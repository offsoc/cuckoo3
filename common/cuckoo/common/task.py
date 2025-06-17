# Copyright (C) 2019-2021 Estonian Information System Authority.
# See the file 'LICENSE' for copying permission.

import copy
import os

from . import db
from .log import CuckooGlobalLogger
from .storage import TaskPaths, make_task_id
from .strictcontainer import Task, Errors

log = CuckooGlobalLogger(__name__)


class TaskError(Exception):
    pass


class TaskCreationError(TaskError):
    def __init__(self, msg, reasons=[]):
        self.reasons = reasons
        super().__init__(msg)


class MissingResourceError(TaskCreationError):
    pass


class NoTasksCreatedError(TaskCreationError):
    pass


class NotAllTasksCreatedError(TaskCreationError):
    pass


class HumanStates:
    PENDING = "Pending"
    RUNNING = "Running"
    RUN_COMPLETED = "Run completed"
    PENDING_POST = "Pending post"
    REPORTED = "Reported"
    FATAL_ERROR = "Fatal error"


class States:
    PENDING = "pending"
    RUNNING = "running"
    RUN_COMPLETED = "run_completed"
    PENDING_POST = "pending_post"
    REPORTED = "reported"
    FATAL_ERROR = "fatal_error"

    _HUMAN = {
        PENDING: HumanStates.PENDING,
        RUNNING: HumanStates.RUNNING,
        RUN_COMPLETED: HumanStates.RUN_COMPLETED,
        PENDING_POST: HumanStates.PENDING_POST,
        REPORTED: HumanStates.REPORTED,
        FATAL_ERROR: HumanStates.FATAL_ERROR,
    }

    @classmethod
    def to_human(cls, state):
        try:
            return cls._HUMAN[state]
        except KeyError:
            raise TaskError(f"No human readable version for state {state!r} exists")


def _make_task_dirs(task_id):
    task_path = TaskPaths.path(task_id)
    try:
        os.mkdir(task_path)
    except FileExistsError as e:
        raise TaskCreationError(
            f"Task directory '{task_path}' creation failed. Already exists: {e}"
        )

    for dirpath in (
        TaskPaths.logfile(task_id),
        TaskPaths.procmem_dump(task_id),
        TaskPaths.screenshot(task_id),
        TaskPaths.dropped_file(task_id),
    ):
        os.mkdir(dirpath)


def _create_task(nodes_tracker, analysis, task_number, platform_obj):
    route = platform_obj.settings.route or analysis.settings.route
    has_platform, has_route, _ = nodes_tracker.nodeinfos.find_support(
        platform_obj, route
    )

    if not has_platform:
        raise MissingResourceError(f"No node has machine with: {platform_obj}")

    if has_platform and route and not has_route:
        raise MissingResourceError(
            f"No nodes have the combination of platform: "
            f"{platform_obj} and route {route}"
        )

    task_id = make_task_id(analysis.id, task_number)
    log.debug(
        "Creating task.",
        task_id=task_id,
        platform=platform_obj.platform,
        os_version=platform_obj.os_version,
    )

    _make_task_dirs(task_id)
    task_values = {
        "kind": analysis.kind,
        "number": task_number,
        "id": task_id,
        "state": States.PENDING,
        "analysis_id": analysis.id,
        "platform": platform_obj.platform,
        "os_version": platform_obj.os_version,
        "machine_tags": list(platform_obj.tags),
        "command": platform_obj.settings.command or analysis.settings.command,
        "browser": platform_obj.settings.browser or analysis.settings.browser,
        "route": route,
    }

    task = Task(**task_values)
    task.to_file(TaskPaths.taskjson(task_id))
    analysis.tasks.append(
        {
            "id": task_id,
            "platform": platform_obj.platform,
            "os_version": platform_obj.os_version,
            "state": States.PENDING,
            "score": 0,
        }
    )

    return task_values


def create_all(analysis, nodes_tracker):
    tasks = []
    tasknum = 1
    resource_errors = []
    for platform in analysis.settings.platforms:
        try:
            tasks.append(
                _create_task(
                    nodes_tracker, analysis, task_number=tasknum, platform_obj=platform
                )
            )
            tasknum += 1
        except MissingResourceError as e:
            resource_errors.append(str(e))

    if not tasks:
        raise NoTasksCreatedError("No tasks were created", reasons=resource_errors)

    # Set the default state for each task dict
    for task_dict in tasks:
        task_dict["created_on"] = analysis.created_on
        task_dict["priority"] = analysis.settings.priority

    # Copy the list of task dicts to allow the changing of data in preparation
    # of db insertion. Normally would be handled by ORM, but is not since
    # we are using a bulk insert here.
    task_rows = copy.deepcopy(tasks)
    for row in task_rows:
        row["machine_tags"] = ",".join(row["machine_tags"])

    ses = db.dbms.session()
    try:
        ses.bulk_insert_mappings(db.Task, task_rows)
        ses.commit()
    finally:
        ses.close()

    return tasks, resource_errors


def set_db_state(task_id, state):
    ses = db.dbms.session()
    try:
        ses.query(db.Task).filter_by(id=task_id).update({"state": state})
        ses.commit()
    finally:
        ses.close()


def merge_errors(task, errors_container):
    if task.errors:
        task.errors.merge_errors(errors_container)
    else:
        task.errors = errors_container


def merge_run_errors(task):
    errpath = TaskPaths.runerr_json(task.id)
    if not os.path.exists(errpath):
        return

    merge_errors(task, Errors.from_file(errpath))

    os.remove(errpath)


def merge_processing_errors(task):
    errpath = TaskPaths.processingerr_json(task.id)
    if not os.path.exists(errpath):
        return

    merge_errors(task, Errors.from_file(errpath))

    os.remove(errpath)


def exists(task_id):
    return os.path.isfile(TaskPaths.taskjson(task_id))


def has_unfinished_tasks(analysis_id):
    ses = db.dbms.session()
    try:
        count = (
            ses.query(db.Task)
            .filter(
                db.Task.analysis_id == analysis_id,
                db.Task.state.in_(
                    [States.PENDING, States.RUNNING, States.PENDING_POST]
                ),
            )
            .count()
        )
        return count > 0
    finally:
        ses.close()


def update_db_row(task_id, **kwargs):
    ses = db.dbms.session()
    try:
        ses.query(db.Task).filter_by(id=task_id).update(kwargs)
        ses.commit()
    finally:
        ses.close()


def count_created(start=None, end=None):
    ses = db.dbms.session()
    try:
        q = ses.query(db.Task)
        if start and end:
            q = q.filter(db.Task.created_on >= start, db.Task.created_on <= end)
        return q.count()
    finally:
        ses.close()


def write_changes(task):
    if not task.was_updated:
        return

    db_fields = {}
    for field in ("state", "score"):
        if field in task.updated_fields:
            db_fields[field] = task[field]

    task.to_file_safe(TaskPaths.taskjson(task.id))
    if db_fields:
        update_db_row(task.id, **db_fields)
