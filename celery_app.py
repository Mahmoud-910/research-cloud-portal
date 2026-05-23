"""
celery_app.py — إعداد الـ Celery instance.
منفصل عن الـ Flask app عشان الـ worker يقدر يشتغل لوحده.
"""

import os
from celery import Celery
from dotenv import load_dotenv

load_dotenv()

def make_celery(app=None):
    celery = Celery(
        "research_portal",
        broker=os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0"),
        backend=os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0"),
    )

    celery.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_track_started=True,
        result_expires=3600,
    )

    if app:
        class ContextTask(celery.Task):
            def __call__(self, *args, **kwargs):
                with app.app_context():
                    return self.run(*args, **kwargs)
        celery.Task = ContextTask

    return celery


celery = make_celery()
