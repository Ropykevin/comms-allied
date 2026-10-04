"""Celery entry point.

    celery -A celery_worker.celery worker --loglevel=info
    celery -A celery_worker.celery beat --loglevel=info
"""
import os

os.environ.setdefault("TASK_BACKEND", "celery")

from app import create_app  # noqa: E402

flask_app = create_app(os.environ.get("APP_ENV", "production"))
celery = flask_app.extensions["celery"]
