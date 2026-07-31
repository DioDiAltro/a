"""Test settings: fast hashing, eager tasks, in-memory channel layer."""

from .base import *  # noqa: F403

DEBUG = False
LIVE_MODE = False

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# SQLite keeps the suite runnable without a container. The zero-sum invariant is
# enforced in `wallet.ledger.post` rather than by a database trigger precisely so
# that it holds on both backends and can be unit-tested here.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "ATOMIC_REQUESTS": False,
    }
}

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
