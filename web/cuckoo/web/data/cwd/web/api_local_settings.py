# Copyright (C) 2020 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

from cuckoo.common.storage import Paths

from django.utils.crypto import get_random_string

# Language code for this installation. All choices can be found here:
# http://www.i18nguy.com/unicode/language-identifiers.html
LANGUAGE_CODE = "en-us"

# Override default secret key stored in $CWD/web/.apisecret
# Make this unique, and don't share it with anybody.
# SECURITY WARNING: keep the secret key used in production secret!
secret_path = Path(Paths.web(".apisecret"))
if not secret_path.exists():
    secret_path.write_text(
        get_random_string(
            50, "abcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*(-_=+)"
        )
    )

SECRET_KEY = secret_path.read_text()

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': Paths.web("api.db"),
    }
}

ADMINS = (
    # ("Your Name", "your_email@example.com"),
)
MANAGERS = ADMINS

# Allow verbose debug error message in case of application fault.
# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = False
DEBUG404 = False

# A list of strings representing the host/domain names that this Django site
# can serve. Values in this list can be fully qualified names
# (e.g. 'www.example.com'). When DEBUG is True or when running tests, host
# validation is disabled; any host will be accepted. Thus it's usually only
# necessary to set it in production.
ALLOWED_HOSTS = ["*"]
