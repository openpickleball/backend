"""The Celery app for OPEN background work.

`celery -A config worker` and `celery -A config beat` load this module
through config/__init__.py. Settings come from Django settings, under the
CELERY_ prefix (see the Celery section of config/settings.py). Nothing
here reads settings at import time.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
# Finds a tasks module in each installed app.
app.autodiscover_tasks()


# Named explicitly: queued messages carry the name, so a rename or move
# must not strand them.
@app.task(name="config.ping")
def ping() -> str:
    """Return "pong". Proves a worker picks up and runs tasks."""
    return "pong"
