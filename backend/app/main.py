from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import os
from .routers import tts, yadisk, stt_ws, gigachat, agent
from .routers import invitations
from .routers import candidates
from .routers import live
from .routers import reports
from .routers import upload
from .routers import auth
from .routers import match
from .routers import scheduler
from .routers import dialog
from .routers import analysis
from .routers import contacts
from .routers import embeddings
from .routers import vacancies
from .routers import stats
from .routers import voip
from .routers import voice_agents
from .routers import metrics
from .routers import tasks as tasks_router
from .routers import audit
from .routers import notify
from .routers import realtime
from .routers import va
from .routers import interview
from .db import engine, Base, SessionLocal
from sqlalchemy import text
from .config import settings
from .models import User
from .security import hash_password
from .services.escalation import escalation_loop

app = FastAPI(title="Sber Interviewer Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok"})


@app.on_event("startup")
async def on_startup() -> None:
    try:
        # Автосоздание таблиц (для MVP). В проде — миграции Alembic.
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # Инициализация учётки админа в settings (если используем таблицу пользователей — можно расширить)
        # Здесь лишь логируем наличие ADMIN_USER для прозрачноcти
        async with SessionLocal() as s:
            # создаём админа, если не существует
            res = await s.execute(text("SELECT 1 FROM users WHERE username=:u"), {"u": settings.admin_user})
            if res.scalar() is None:
                pwd_hash = hash_password(settings.admin_password, settings.auth_secret)
                await s.execute(text("INSERT INTO users(username, password_hash, role) VALUES(:u, :p, 'admin')"), {"u": settings.admin_user, "p": pwd_hash})
                await s.commit()
        # Escalation background task
        if settings.escalation_enabled:
            app.state.escalation_stop = asyncio.Event()
            app.state.escalation_task = asyncio.create_task(escalation_loop(SessionLocal, app.state.escalation_stop))
    except Exception:
        # Не валимся при ошибке миграции на демо
        await asyncio.sleep(0)


@app.websocket("/ws/audio")
async def audio_ws(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            data = await ws.receive_bytes()
            # echo back frame size for smoke-test; replace with VAD/STT pipeline later
            await ws.send_json({"recv": len(data)})
    except WebSocketDisconnect:
        return
    except Exception as e:
        # don't leak internals
        await asyncio.sleep(0)
        return


app.include_router(tts.router)
app.include_router(yadisk.router)
app.include_router(stt_ws.router)
app.include_router(gigachat.router)
app.include_router(agent.router)
app.include_router(invitations.router)
app.include_router(candidates.router)
app.include_router(live.router)
app.include_router(reports.router)
app.include_router(upload.router)
app.include_router(auth.router)
app.include_router(match.router)
app.include_router(scheduler.router)
app.include_router(dialog.router)
app.include_router(analysis.router)
app.include_router(contacts.router)
app.include_router(embeddings.router)
app.include_router(vacancies.router)
app.include_router(stats.router)
app.include_router(voip.router)
app.include_router(voice_agents.router)
app.include_router(notify.router)
app.include_router(metrics.router)
app.include_router(audit.router)
app.include_router(tasks_router.router)
app.include_router(realtime.router)
app.include_router(va.router)
app.include_router(interview.router)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    try:
        ev = getattr(app.state, 'escalation_stop', None)
        if ev:
            ev.set()
        task = getattr(app.state, 'escalation_task', None)
        if task:
            await task
    except Exception:
        await asyncio.sleep(0)


