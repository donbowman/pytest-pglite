"""Django settings for the PGlite example.

Importing this module starts a PGlite server (once per process) and points
Django's PostgreSQL backend at its Unix socket. psycopg treats a directory in
HOST as a Unix socket directory.
"""

from __future__ import annotations

import os

from pglite_env import ensure_server

ensure_server()

SECRET_KEY = "pytest-pglite-example"
DEBUG = False
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "shop",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("PGLITE_DB", "template1"),
        "USER": "postgres",
        "PASSWORD": "password",
        "HOST": os.environ["PGLITE_HOST"],
        "PORT": os.environ.get("PGLITE_PORT", "5432"),
        "OPTIONS": {"sslmode": "disable"},
    }
}

USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
