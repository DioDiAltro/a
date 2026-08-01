"""Belt to the braces in `pyproject.toml`.

The real guard is `--ds=config.settings.test` in pytest's `addopts`: a command
line flag outranks the `DJANGO_SETTINGS_MODULE` environment variable, which
outranks the ini option. That ordering matters because the README tells you to
export the dev settings to run the server, and running `pytest` in that same
shell would otherwise point the suite at your development database — where it
would happily create and delete rows.

This file drops any inherited `DATABASE_URL` as well. The test settings pin an
in-memory SQLite database and ignore it, so this changes nothing today; it is
here so that a future settings module which *does* read `DATABASE_URL` cannot
silently inherit a production connection string from somebody's shell.
"""

import os

os.environ.pop("DATABASE_URL", None)
