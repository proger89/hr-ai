from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse
import asyncio
import json
import time
from typing import Any, Dict
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST


router = APIRouter(prefix="/api/metrics", tags=["metrics"])

_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=1000)

# Prometheus metrics
METRIC_EVENTS = Counter('sber_events_total', 'Count of telemetry events', ['name'])
HIST_TTFB = Histogram('sber_tts_ttfb_ms', 'TTFB of TTS in ms', buckets=(50, 100, 200, 400, 800, 1500, 3000))


@router.post("")
async def push_metric(payload: Dict[str, Any]):
    item = {"ts": int(time.time()*1000), **(payload or {})}
    try:
        _queue.put_nowait(item)
    except asyncio.QueueFull:
        try:
            _ = _queue.get_nowait()
        except Exception:
            pass
        await _queue.put(item)
    # Update Prometheus
    try:
        name = str(payload.get('name') or 'unknown')
        METRIC_EVENTS.labels(name=name).inc()
        if name == 'tts_ttfb_ms':
            ms = float(payload.get('ms') or 0)
            HIST_TTFB.observe(ms)
    except Exception:
        pass
    return {"ok": True}


@router.get("/stream")
async def stream_metrics(request: Request):
    async def gen():
        yield b"event: open\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                item = await asyncio.wait_for(_queue.get(), timeout=10.0)
                data = json.dumps(item, ensure_ascii=False).encode("utf-8")
                yield b"data: " + data + b"\n\n"
            except asyncio.TimeoutError:
                # heartbeat
                yield b": keepalive\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/prom")
async def prom_export():
    data = generate_latest()
    return StreamingResponse(iter([data]), media_type=CONTENT_TYPE_LATEST)


