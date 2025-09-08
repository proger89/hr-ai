from __future__ import annotations

from celery import Celery
from .config import settings


celery_app = Celery(
    "sber_interviewer",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    imports=(
        "app.routers.upload",
        "app.tasks",
    ),
)


@celery_app.task(name="ping")
def ping() -> str:
    return "pong"


# Explicitly import task modules so Celery registers them
try:
    from .routers.upload import task_generate_scenario  # noqa: F401
    from .tasks import generate_report_task  # noqa: F401
except Exception:
    # In worker startup, missing imports should not crash the process; tasks may be discovered later
    pass

