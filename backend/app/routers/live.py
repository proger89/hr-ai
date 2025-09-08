from __future__ import annotations

from fastapi import APIRouter, Depends
from typing import Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update
from ..db import get_session
from ..models import LiveSession, Candidate, Vacancy


router = APIRouter(prefix="/api/live", tags=["live"])


@router.get("")
async def list_live(session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    result = await session.execute(select(LiveSession))
    sessions: List[Dict[str, Any]] = []
    for s in result.scalars().all():
        # enrich
        cand = await session.get(Candidate, s.candidate_id) if s.candidate_id else None
        vac = await session.get(Vacancy, s.vacancy_id) if s.vacancy_id else None
        sessions.append({
            "id": s.session_id,
            "candidate_name": cand.name if cand else None,
            "vacancy": vac.title if vac else None,
            "ping_ms": s.ping_ms,
            "net": s.net,
            "lang": s.lang,
            "competency": s.competency,
            "partial": s.partial,
        })
    return {"sessions": sessions}


@router.post("/heartbeat")
async def heartbeat(session_id: str, lang: str | None = None, competency: str | None = None, partial: str | None = None, ping_ms: int | None = None, net: str | None = None, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    # upsert by session_id
    existing = await session.execute(select(LiveSession).where(LiveSession.session_id == session_id))
    row = existing.scalar_one_or_none()
    if row is None:
        row = LiveSession(session_id=session_id, lang=lang or "ru-RU", competency=competency, partial=partial, ping_ms=ping_ms, net=net)
        session.add(row)
    else:
        if lang is not None:
            row.lang = lang
        if competency is not None:
            row.competency = competency
        if partial is not None:
            row.partial = partial
        if ping_ms is not None:
            row.ping_ms = ping_ms
        if net is not None:
            row.net = net
    await session.commit()
    return {"ok": True}


