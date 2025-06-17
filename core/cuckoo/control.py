# Copyright (C) 2019-2021 Estonian Information System Authority.
# See the file 'LICENSE' for copying permission.

from datetime import datetime, timezone
import os
import queue
import threading

from cuckoo.common import analyses, task
from cuckoo.common.config import cfg
from cuckoo.common.errors import ErrorTracker
from cuckoo.common.importing import import_analysis, AnalysisImportError
from cuckoo.common.ipc import UnixSocketServer, ReaderWriter
from cuckoo.common.log import CuckooGlobalLogger, AnalysisLogger, TaskLogger
from cuckoo.common.storage import Paths, AnalysisPaths, TaskPaths, delete_file
from cuckoo.common.strictcontainer import Analysis, Task, Identification, Pre, Post
from cuckoo.common.submit import settings_maker, SubmissionError

log = CuckooGlobalLogger(__name__)

_tracking_lock = threading.Lock()


class StateControllerError(Exception):
    pass


def set_location_remote(worktracker):
    exported_ids = os.listdir(Paths.exported())
    if not exported_ids:
        return

    analyses.db_set_remote(exported_ids)

    for analysis_id in exported_ids:
        delete_file(Paths.exported(analysis_id))


def import_importables(worktracker):
    with _tracking_lock:
        importables = os.listdir(Paths.importables())
        if not importables:
            return

        for importable in importables:
            if not importable.endswith(".zip"):
                continue

            path = Paths.importables(importable)
            try:
                analysis = import_analysis(str(path), delete_after_import=True)
                log.debug("Imported analysis", analysis=analysis.id)
            except AnalysisImportError as e:
                log.warning("Import failed", importable=path, error=e)
                continue


def track_untracked(worktracker):
    with _tracking_lock:
        analysis_ids = os.listdir(Paths.untracked())
        if not analysis_ids:
            return

        tracked = analyses.track_analyses(analysis_ids)
        log.info("Tracked new analyses.", amount=len(tracked))

        # Queue the newly tracked analyses for identification.
        for analysis_id in tracked:
            worktracker.ctx.processing_handler.identify(analysis_id)

        # Remove all analysis id files from the untracked dir.
        for analysis_id in analysis_ids:
            delete_file(Paths.untracked(analysis_id))


def handle_identification_done(worktracker):
    analysis = worktracker.analysis

    if analysis.settings.manual:
        analysis.state = analyses.States.WAITING_MANUAL
        analyses.write_changes(analysis)
        return

    ident_path = AnalysisPaths.identjson(worktracker.analysis_id)
    if not os.path.isfile(ident_path):
        worktracker.log.error(
            "Failed to read identification stage file",
            error="File does not exist",
            filepath=ident_path,
        )
        worktracker.errtracker.fatal_error(
            "Failed to read identification stage file. File does not exist"
        )
        analysis.state = analyses.States.FATAL_ERROR
        analyses.write_changes(analysis)
        return

    ident = Identification.from_file(ident_path)
    allow_pre_analysis = False
    if not ident.selected:
        # No target was selected. Check settings if this means we should
        # still perform the pre analysis processing stage.
        cancel = cfg("cuckoo", "state_control", "cancel_unidentified")
        if not ident.identified and not cancel:
            allow_pre_analysis = True
        else:
            newstate = analyses.States.NO_SELECTED
            worktracker.log.debug("Updating analysis state.", newstate=newstate)

            analysis.state = newstate
            analyses.write_changes(analysis)

    if ident.selected or allow_pre_analysis:
        newstate = analyses.States.PENDING_PRE
        worktracker.log.debug("Updating analysis state.", newstate=newstate)
        analysis.state = newstate
        analyses.write_changes(analysis)
        worktracker.ctx.processing_handler.pre_analysis(worktracker.analysis_id)


def handle_pre_done(worktracker):
    analysis = worktracker.analysis

    pre_path = AnalysisPaths.prejson(worktracker.analysis_id)
    if not os.path.isfile(pre_path):
        worktracker.log.error(
            "Failed to pre processing stage file",
            error="Pre processing stage file does not exist.",
            filepath=pre_path,
        )
        worktracker.errtracker.fatal_error("Pre processing stage file does not exist.")
        analysis.state = analyses.States.FATAL_ERROR
        analyses.write_changes(analysis)
        return

    pre = Pre.from_file(pre_path)

    # Overwrite the analysis.settings.platforms list with one that was
    # changed during the pre stage. Pre can add things such as launch commands
    # to platforms.
    if pre.platforms:
        analyses.overwrite_platforms(analysis, pre.platforms)

    # Update analysis settings of the final target using information identified
    # during the identification phase.
    analyses.determine_final_platforms(analysis, pre)

    # Set the final target in analysis.json
    analysis.target = pre.target

    # Update analysis score, tags, and detected families
    worktracker.analysis.update_from_report(pre)

    worktracker.log.debug("Creating tasks for analysis.")
    # It is possible that no tasks are created if the identified machine tags
    # or platforms are not available. If autotag is enabled
    # this can cause submitted analyses to then become non-runnable if the
    # submitted settings are enriched with machine tags that no machine has.
    # This is not a bug. This is the intended operation of auto tagging. It
    # should stop analyses before creating tasks that are not useful to run.
    try:
        tasks, resource_errs = task.create_all(analysis, worktracker.ctx.nodes)
    except task.TaskCreationError as e:
        worktracker.log.error("Failed to create tasks for analysis", error=e)
        worktracker.errtracker.fatal_error(
            f"Failed to create tasks for analysis. {e}. Reasons: {', '.join(e.reasons)}"
        )
        for err in e.reasons:
            worktracker.errtracker.add_error(err)
            worktracker.log.warning("Task creation failed.", error=err)

        analysis.state = analyses.States.FATAL_ERROR
        analyses.write_changes(analysis)
        return

    for err in resource_errs:
        worktracker.errtracker.add_error(err)
        worktracker.log.warning("Task creation failed.", error=err)

    analysis.state = analyses.States.TASKS_PENDING

    # Write to disk here because the target will be read by the task runner.
    # the task can start as soon as it is queued.
    analyses.write_changes(analysis)

    worktracker.ctx.scheduler.queue_many(*tasks)


def handle_manual_done(worktracker, settings_dict):
    s_helper = settings_maker.new_settings(nodeinfos=worktracker.ctx.nodes.nodeinfos)
    try:
        s_helper.from_dict(settings_dict)
        # We overwrite all settings, but want to retain the 'manual' setting
        # to be able to recognize it was used after this step.
        s_helper.set_manual(True)
        settings = s_helper.make_settings()
    except SubmissionError as e:
        worktracker.log.error(
            "Failed to update settings for manual state analysis",
            error=e,
            analysis_id=worktracker.analysis_id,
        )
        return

    worktracker.analysis.settings = settings

    # Update the analysis to the new pending pre state and queue it for
    # pre analysis processing.
    worktracker.analysis.state = analyses.States.PENDING_PRE

    # Write the new settings to the analysis file
    analyses.write_changes(worktracker.analysis)

    worktracker.ctx.processing_handler.pre_analysis(worktracker.analysis_id)


def handle_post_done(worktracker):
    report_path = TaskPaths.report(worktracker.task_id)
    if not os.path.isfile(report_path):
        worktracker.log.error(
            "Failed to read post processing report",
            error="File does not exist",
            filepath=report_path,
        )
        worktracker.errtracker.fatal_error("Post processing report file does not exist")
        worktracker.task.state = task.States.FATAL_ERROR
        task.write_changes(worktracker.task)
        return

    post = Post.from_file(report_path)
    worktracker.task.score = post.score
    worktracker.task.state = task.States.REPORTED

    # Update the score and state of this task in the analysis json.
    worktracker.analysis.update_task(
        worktracker.task.id,
        score=post.score,
        state=worktracker.task.state,
        stopped_on=datetime.now(timezone.utc),
    )

    # Update analysis score, tags, and detected families
    worktracker.analysis.update_from_report(post)

    worktracker.log.info("Setting task to reported.")
    task.write_changes(worktracker.task)
    update_final_analysis_state(worktracker)
    analyses.write_changes(worktracker.analysis)


def update_final_analysis_state(worktracker):
    if not task.has_unfinished_tasks(worktracker.analysis_id):
        worktracker.analysis.state = analyses.States.FINISHED
        # Delete stored analysis files on all nodes that worked on this
        # analysis.
        worktracker.ctx.nodes.delete_completed_analysis(worktracker.analysis_id)


def set_next_state(worktracker, worktype):
    if worktype == "identification":
        analyses.merge_processing_errors(worktracker.analysis)
        handle_identification_done(worktracker)

    elif worktype == "pre":
        analyses.merge_processing_errors(worktracker.analysis)
        handle_pre_done(worktracker)

    elif worktype == "post":
        task.merge_processing_errors(worktracker.task)
        handle_post_done(worktracker)

    else:
        raise ValueError(
            f"Unknown work type {worktype} for analysis: {worktracker.analysis_id}"
        )


def set_failed(worktracker, worktype):
    if worktype == "identification":
        worktracker.log.error("Analysis identification stage failed")
        analyses.merge_processing_errors(worktracker.analysis)
        worktracker.analysis.state = analyses.States.FATAL_ERROR
        analyses.write_changes(worktracker.analysis)

    elif worktype == "pre":
        worktracker.log.error("Analysis pre stage failed")
        analyses.merge_processing_errors(worktracker.analysis)
        worktracker.analysis.state = analyses.States.FATAL_ERROR
        analyses.write_changes(worktracker.analysis)

    elif worktype == "post":
        worktracker.log.error("Task post stage failed")
        task.merge_processing_errors(worktracker.task)
        worktracker.task.state = task.States.FATAL_ERROR
        worktracker.analysis.update_task(
            worktracker.task.id,
            state=worktracker.task.state,
            stopped_on=datetime.now(timezone.utc),
        )
        task.write_changes(worktracker.task)
        update_final_analysis_state(worktracker)
        analyses.write_changes(worktracker.analysis)

    else:
        raise ValueError(
            f"Unknown work type '{worktype}' for analysis: {worktracker.analysis_id}"
        )


def handle_task_done(worktracker):
    task.merge_run_errors(worktracker.task)

    worktracker.log.debug("Queueing task for post analysis processing.")
    worktracker.task.state = task.States.PENDING_POST
    task.write_changes(worktracker.task)
    worktracker.ctx.processing_handler.post_analysis(
        worktracker.analysis_id, worktracker.task_id
    )


def set_task_failed(worktracker):
    worktracker.log.info("Setting task to state failed")
    task.merge_run_errors(worktracker.task)
    worktracker.task.state = task.States.FATAL_ERROR
    worktracker.analysis.update_task(
        worktracker.task.id, state=worktracker.task.state, stopped_on=datetime.now(timezone.utc)
    )
    analyses.write_changes(worktracker.analysis)
    task.write_changes(worktracker.task)
    update_final_analysis_state(worktracker)
    analyses.write_changes(worktracker.analysis)


def set_task_running(worktracker, machine, node):
    worktracker.log.info("Setting task to state running")
    worktracker.task.state = task.States.RUNNING
    worktracker.task.node = node.name
    worktracker.analysis.update_task(
        worktracker.task.id,
        state=worktracker.task.state,
        platform=machine.platform,
        os_version=machine.os_version,
        started_on=datetime.now(timezone.utc),
    )
    task.write_changes(worktracker.task)
    analyses.write_changes(worktracker.analysis)


def set_task_pending(worktracker):
    worktracker.log.info("Setting task to pending")
    worktracker.task.state = task.States.PENDING
    worktracker.analysis.update_task(
        worktracker.task.id, state=worktracker.task.state, clear_started_stopped=True
    )
    task.write_changes(worktracker.task)
    analyses.write_changes(worktracker.analysis)


class _WorkTracker:
    def __init__(self, cuckooctx, func, analysis_locker, **kwargs):
        self.ctx = cuckooctx
        self._func = func
        self._locker = analysis_locker
        self.analysis_id = kwargs.pop("analysis_id", None)
        self.task_id = kwargs.pop("task_id", None)

        self._func_kwargs = kwargs

        self.analysis = None
        self.task = None
        self.log = None

        if self.analysis_id:
            analysis_locker.inform_work(self.analysis_id)

        self.errtracker = ErrorTracker()

    def __enter__(self):
        if self.analysis_id:
            return self._locker.get_analysis_lock(self.analysis_id).acquire()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.analysis_id:
            l = self._locker.get_analysis_lock(self.analysis_id)
            self._locker.inform_work_done(self.analysis_id)
            l.release()

    def init(self):
        self._make_logger()
        self._load_strictdicts()

    def _make_logger(self):
        if self.task_id:
            self.log = TaskLogger(__name__, self.task_id)
        elif self.analysis_id:
            self.log = AnalysisLogger(__name__, self.analysis_id)
        else:
            self.log = log

    def _load_strictdicts(self):
        if self.analysis_id:
            if not analyses.exists(self.analysis_id):
                raise StateControllerError(
                    f"Analysis {self.analysis_id} does not exist."
                )

            self.analysis = Analysis.from_file(
                AnalysisPaths.analysisjson(self.analysis_id)
            )
        if self.task_id:
            if not task.exists(self.task_id):
                raise StateControllerError(f"Task {self.task_id} does not exist.")
            self.task = Task.from_file(TaskPaths.taskjson(self.task_id))

    def run_work(self):
        self._func(self, **self._func_kwargs)

    def close(self):
        if self.log:
            self.log.close()
        if self.errtracker.has_errors():
            errors_container = self.errtracker.to_container()
            if self.task_id:
                task.merge_errors(self.task, errors_container)
            elif self.analysis_id:
                analyses.merge_errors(self.analysis, errors_container)

        if self.analysis and self.analysis.was_updated:
            analyses.write_changes(self.analysis)
        elif self.task and self.task.was_updated:
            task.write_changes(self.task)


class StateControllerWorker(threading.Thread):
    def __init__(self, work_queue):
        super().__init__()

        self.work_queue = work_queue
        self.do_run = True

    def run(self):
        while self.do_run:
            try:
                worktracker = self.work_queue.get(timeout=1)
            except queue.Empty:
                continue

            with worktracker:
                try:
                    worktracker.init()
                    worktracker.run_work()
                except Exception as e:
                    worktracker.log.exception(
                        "Failed to run handler function",
                        function=worktracker._func,
                        args=worktracker._func_kwargs,
                        error=e,
                    )
                finally:
                    worktracker.close()

    def stop(self):
        self.do_run = False


class _AnalysisLock:
    def __init__(self):
        self.lock = threading.Lock()
        self.waiters = 0

    def decrement_waiters(self):
        self.waiters -= 1

    def increment_waiters(self):
        self.waiters += 1


class _AnalysisLocker:
    def __init__(self):
        self._trackerlock = threading.Lock()
        self._locks = {}

    def inform_work(self, analysis_id):
        with self._trackerlock:
            a_lock = self._locks.setdefault(analysis_id, _AnalysisLock())
            a_lock.increment_waiters()

    def inform_work_done(self, analysis_id):
        with self._trackerlock:
            a_lock = self._locks[analysis_id]
            a_lock.decrement_waiters()
            if a_lock.waiters < 1:
                self._locks.pop(analysis_id)

    def get_analysis_lock(self, analysis_id):
        return self._locks[analysis_id].lock


class StateController(UnixSocketServer):
    NUM_STATE_CONTROLLER_WORKERS = 6

    def __init__(self, controller_sock_path, cuckooctx):
        super().__init__(controller_sock_path)
        self.ctx = cuckooctx

        self._locker = _AnalysisLocker()
        self.workers = []
        self.work_queue = queue.Queue()
        self.subject_handler = {
            "tracknew": self.track_new_analyses,
            "setremote": self.set_remote,
            "workdone": self.work_done,
            "workfail": self.work_failed,
            "taskrundone": self.task_done,
            "taskrunfailed": self.task_failed,
            "manualsetsettings": self.manual_set_settings,
        }

    def queue_call(self, func, kwargsdict={}):
        if not isinstance(kwargsdict, dict):
            raise TypeError(f"Kwargs dict must be a dict. Got: {type(kwargsdict)}")

        self.work_queue.put(_WorkTracker(self.ctx, func, self._locker, **kwargsdict))

    def work_done(self, **kwargs):
        self.queue_call(
            set_next_state,
            {
                "worktype": kwargs["worktype"],
                "analysis_id": kwargs["analysis_id"],
                "task_id": kwargs.get("task_id"),
            },
        )

    def work_failed(self, **kwargs):
        self.queue_call(
            set_failed,
            {
                "worktype": kwargs["worktype"],
                "analysis_id": kwargs["analysis_id"],
                "task_id": kwargs.get("task_id"),
            },
        )

    def task_done(self, **kwargs):
        self.queue_call(
            handle_task_done,
            {"analysis_id": kwargs["analysis_id"], "task_id": kwargs["task_id"]},
        )

    def task_running(self, **kwargs):
        self.queue_call(
            set_task_running,
            {
                "task_id": kwargs["task_id"],
                "analysis_id": kwargs["analysis_id"],
                "machine": kwargs["machine"],
                "node": kwargs["node"],
            },
        )

    def task_pending(self, **kwargs):
        self.queue_call(
            set_task_pending,
            {"task_id": kwargs["task_id"], "analysis_id": kwargs["analysis_id"]},
        )

    def task_failed(self, **kwargs):
        self.queue_call(
            set_task_failed,
            {"analysis_id": kwargs["analysis_id"], "task_id": kwargs["task_id"]},
        )

    def manual_set_settings(self, **kwargs):
        self.queue_call(
            handle_manual_done,
            {
                "analysis_id": kwargs["analysis_id"],
                "settings_dict": kwargs["settings_dict"],
            },
        )

    def track_new_analyses(self, **kwargs):
        self.queue_call(track_untracked)

    def set_remote(self, **kwargs):
        self.queue_call(set_location_remote)

    def handle_connection(self, sock, addr):
        self.track(sock, ReaderWriter(sock))

    def handle_message(self, sock, msg):
        subject = msg.get("subject")
        if not subject:
            return

        handler = self.subject_handler.get(subject)
        if not handler:
            return

        try:
            handler(**msg)
        except KeyError as e:
            log.warning("Incomplete message received.", msg=repr(msg), error=e)
        except TypeError as e:
            log.warning("Incorrect message received.", msg=repr(msg), error=e)
        except StateControllerError as e:
            log.error("Error while handling message", error=e)
        except Exception as e:
            log.exception(
                "Fatal error while handling message.", error=e, message=repr(msg)
            )
            raise

    def stop(self):
        if not self.do_run and not self.workers:
            return

        super().stop()
        for worker in self.workers:
            worker.stop()

        self.cleanup()

    def start(self):
        for _ in range(self.NUM_STATE_CONTROLLER_WORKERS):
            worker = StateControllerWorker(self.work_queue)
            self.workers.append(worker)
            worker.start()

        self.create_socket()
        self.start_accepting(timeout=1)


class ImportController(StateController):
    NUM_STATE_CONTROLLER_WORKERS = 1

    def __init__(self, controller_sock_path, cuckooctx):
        super().__init__(controller_sock_path, cuckooctx)
        self.workers = []
        self.work_queue = queue.Queue()
        self.subject_handler = {"trackimportables": self.import_importables}

    def import_importables(self, **kwargs):
        self.queue_call(import_importables)
