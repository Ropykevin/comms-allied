from celery import Celery, Task


def celery_init_app(app):
    class FlaskTask(Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                try:
                    return self.run(*args, **kwargs)
                finally:
                    from app.extensions import db
                    db.session.remove()

    celery = Celery(app.name, task_cls=FlaskTask)
    celery.conf.update(
        broker_url=app.config["REDIS_URL"],
        result_backend=app.config["REDIS_URL"],
        task_ignore_result=True,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        # Long campaigns must not be redelivered to a second worker while still running.
        broker_transport_options={"visibility_timeout": 12 * 3600},
        timezone="UTC",
        beat_schedule={
            "dispatch-due-campaigns": {"task": "allied.dispatch_due_campaigns", "schedule": 60.0},
            "purge-old-data": {"task": "allied.purge_old_data", "schedule": 6 * 3600.0},
        },
    )
    celery.set_default()
    app.extensions["celery"] = celery

    from app.tasks import JOBS

    for name, fn in JOBS.items():
        celery.task(name=f"allied.{name}")(fn)
    return celery
