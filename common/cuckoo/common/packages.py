# Copyright (C) 2019-2021 Estonian Information System Authority.
# See the file 'LICENSE' for copying permission.

import os

from importlib import import_module
from pkgutil import iter_modules


class NotACuckooPackageError(Exception):
    pass


def is_cuckoo_package(cuckoo_package):
    if not hasattr(cuckoo_package, "__path__"):
        return False

    path = os.path.join(cuckoo_package.__path__[0], "data", ".cuckoopackage")
    return os.path.isfile(path)


def find_cuckoo_packages():
    """Returns a list of tuples containing the full package name,
    a subpackage name, and imported module of all
     packages part of the cuckoo namespace"""
    import cuckoo

    found = [("cuckoo", "", cuckoo)]

    module_iter = iter_modules(cuckoo.__path__)
    for _, name, is_package in module_iter:
        if not is_package:
            continue

        fullname = f"cuckoo.{name}"
        imported_module = import_module(fullname)
        if is_cuckoo_package(imported_module):
            found.append((fullname, name, imported_module))

    return found


def get_package_versions():
    pkg_versions = {}
    for fullname, _, pkg in find_cuckoo_packages():
        pkg_versions[fullname] = pkg.__version__

    return pkg_versions


def get_module(name):
    return import_module(name)


def get_package_version(name):
    pkg = get_module(name)
    return pkg.__version__


def get_data_dir(cuckoo_package):
    if not is_cuckoo_package(cuckoo_package):
        raise NotACuckooPackageError(f"{cuckoo_package} is not a Cuckoo package")

    return os.path.join(cuckoo_package.__path__[0], "data")


def get_conftemplate_dir(cuckoo_package):
    return os.path.join(get_data_dir(cuckoo_package), "conftemplates")


def get_cwdfiles_dir(cuckoo_package):
    cwddata = os.path.join(get_data_dir(cuckoo_package), "cwd")
    if os.path.isdir(cwddata):
        return cwddata

    return ""


def has_conftemplates(cuckoo_package):
    return os.path.isdir(get_conftemplate_dir(cuckoo_package))


def get_conftemplates(cuckoo_package):
    if not has_conftemplates(cuckoo_package):
        return {}

    path = get_conftemplate_dir(cuckoo_package)
    templates = {}
    for filename in os.listdir(path):
        if filename.endswith(".yaml.jinja2"):
            typeloaderkey = filename.replace(".jinja2", "")
            templates[typeloaderkey] = os.path.join(path, filename)

    return templates


def get_conf_typeloaders(cuckoo_package):
    if not is_cuckoo_package(cuckoo_package):
        raise NotACuckooPackageError(f"{cuckoo_package} is not a Cuckoo package")

    pkgname = f"{cuckoo_package.__name__}.config"
    try:
        config = import_module(pkgname)
    except ModuleNotFoundError:
        return None, None

    if not hasattr(config, "typeloaders"):
        return None, None

    exclude_autoload = []
    if hasattr(config, "exclude_autoload"):
        exclude_autoload = config.exclude_autoload

    return config.typeloaders, exclude_autoload


def get_conf_migrations(cuckoo_package):
    if not is_cuckoo_package(cuckoo_package):
        raise NotACuckooPackageError(f"{cuckoo_package} is not a Cuckoo package")

    pkgname = f"{cuckoo_package.__name__}.confmigrations"
    try:
        confmigrations = import_module(pkgname)
    except ModuleNotFoundError:
        return None

    return confmigrations.migrations


def enumerate_plugins(package_path, namespace, class_, attributes={}):
    """Import plugins of type `class` located at `dirpath` into the
    `namespace` that starts with `module_prefix`. If `dirpath` represents a
    filepath then it is converted into its containing directory. The
    `attributes` dictionary allows one to set extra fields for all imported
    plugins. Using `as_dict` a dictionary based on the module name is
    returned."""

    try:
        dirpath = import_module(package_path).__file__
    except ImportError as e:
        raise ImportError(f"Failed to import package: {package_path}. Error: {e}")
    if os.path.isfile(dirpath):
        dirpath = os.path.dirname(dirpath)

    for fname in os.listdir(dirpath):
        if fname.endswith(".py") and not fname.startswith("__init__"):
            module_name, _ = os.path.splitext(fname)
            module_path = f"{package_path}.{module_name}"
            try:
                import_module(module_path)
            except ImportError as e:
                raise ImportError(f"Failed to import: {module_path}. Error: {e}")

    subclasses = class_.__subclasses__()[:]

    plugins = []
    while subclasses:
        subclass = subclasses.pop(0)

        # Include subclasses of this subclass (there are some subclasses, e.g.,
        # Libvirt machineries such as KVM. KVM<-Libvirt<-Machinery
        subclasses.extend(subclass.__subclasses__())

        # Check whether this subclass belongs to the module namespace that
        # we are currently importing. It should be noted that parent and child
        # namespaces should fail the following if-statement.
        if package_path != ".".join(subclass.__module__.split(".")[:-1]):
            continue

        namespace[subclass.__name__] = subclass
        for key, value in attributes.items():
            setattr(subclass, key, value)

        plugins.append(subclass)

    return sorted(plugins, key=lambda x: x.__name__.lower())
