# Load the Celery app whenever Django starts, so tasks bind to it.
from config.celery import app as celery_app

__all__ = ("celery_app",)
