"""Local development settings."""

from .base import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ["*"]

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

INTERNAL_IPS = ["127.0.0.1"]

# Deposits and withdrawals run against Stripe test keys in development; the
# LIVE_MODE guard in config.checks keeps real settlement off regardless.
LIVE_MODE = False
