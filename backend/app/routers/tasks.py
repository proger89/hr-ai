from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter
from celery.result import AsyncResult

from ..celery_app import celery_app


router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("/{task_id}")
async def task_status(task_id: str) -> Dict[str, Any]:
    """Return Celery task status and optional result.
    JSON: { state, ready, ok, progress, result }
    ok reflects business result if the task returned {"ok": bool}.
    """
    ar = AsyncResult(task_id, app=celery_app)
    state = ar.state
    ready = ar.ready()
    cel_ok = ar.successful() if ready else False
    progress = 100 if cel_ok else 0

    # meta/progress while running
    info = ar.info if isinstance(ar.info, dict) else {}
    if isinstance(info, dict):
        try:
            progress = int(info.get("progress", progress))
        except Exception:
            pass

    # Always try to fetch result once ready (even on failure) without raising
    result: Any | None = None
    if ready:
        try:
            result = ar.get(propagate=False)
        except Exception:
            result = None

    # Derive business ok from result if present
    biz_ok = cel_ok
    if isinstance(result, dict) and ("ok" in result):
        try:
            biz_ok = bool(result.get("ok"))
        except Exception:
            biz_ok = False

    return {"state": state, "ready": ready, "ok": biz_ok, "progress": progress, "result": result}


