from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..models import InviteToken, Candidate, ContactEvent
from ..config import settings
from ..services.voip import VoipService


async def _send_reminder_email(candidate: Candidate, link: str) -> None:
    try:
        import aiohttp  # type: ignore
    except Exception:
        return
    email = (candidate.email or '').strip()
    if not email:
        return
    payload = {"to": email, "subject": "Напоминание: интервью", "text": f"Ссылка: {link}"}
    async with aiohttp.ClientSession() as cli:
        try:
            await cli.post("http://backend:8000/api/notify/email", json=payload, timeout=8)
        except Exception:
            return


async def _send_reminder_sms(candidate: Candidate, link: str) -> None:
    try:
        import aiohttp  # type: ignore
    except Exception:
        return
    phone = (candidate.phone or '').strip()
    if not phone:
        return
    payload = {"to": phone, "text": f"Интервью: {link}"}
    async with aiohttp.ClientSession() as cli:
        try:
            await cli.post("http://backend:8000/api/notify/sms", json=payload, timeout=8)
        except Exception:
            return


async def _create_autocall(candidate: Candidate, session: AsyncSession) -> Optional[int]:
    if not candidate.phone:
        return None
    vs = VoipService(session)
    call = await vs.create_outbound_call(phone_to=candidate.phone, candidate_id=candidate.id)
    await session.commit()
    return call.id


async def escalation_loop(session_factory, stop_event: asyncio.Event) -> None:
    if not settings.escalation_enabled:
        return
    interval = max(60, settings.escalation_check_interval_sec)
    remind_td = timedelta(hours=settings.escalation_reminder_hours)
    autocall_td = timedelta(hours=settings.escalation_autocall_hours)
    while not stop_event.is_set():
        try:
            async with session_factory() as session:  # type: AsyncSession
                now = datetime.now(timezone.utc)
                q = await session.execute(select(InviteToken))
                tokens = q.scalars().all()
                for t in tokens:
                    created = t.created_at  # type: ignore[attr-defined]
                    if not created:
                        continue
                    if t.used_at is None:
                        age = now - created
                        cand = await session.get(Candidate, t.candidate_id) if t.candidate_id else None
                        link = f"/v/{t.vacancy_id}" if t.vacancy_id else "/"
                        if age >= autocall_td:
                            if cand:
                                call_id = await _create_autocall(cand, session)
                                if call_id:
                                    session.add(ContactEvent(candidate_id=cand.id, type="autocall_started", meta={"call_id": call_id}))
                                    await session.commit()
                        elif age >= remind_td:
                            if cand:
                                await _send_reminder_email(cand, link)
                                await _send_reminder_sms(cand, link)
                                session.add(ContactEvent(candidate_id=cand.id, type="reminder_sent", meta={"link": link}))
                                await session.commit()
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


