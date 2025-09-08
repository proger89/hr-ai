from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import VoipCall, VoipEvent


class VoipService:
    def __init__(self, session: AsyncSession, provider: str = "simulated") -> None:
        self.session = session
        self.provider = provider

    async def create_outbound_call(
        self,
        *,
        phone_to: str,
        phone_from: Optional[str] = None,
        candidate_id: Optional[int] = None,
        vacancy_id: Optional[int] = None,
        slot_id: Optional[int] = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> VoipCall:
        call = VoipCall(
            provider=self.provider,
            external_id=None,
            direction="outbound",
            status="initiated",
            candidate_id=candidate_id,
            vacancy_id=vacancy_id,
            slot_id=slot_id,
            phone_from=phone_from,
            phone_to=phone_to,
            meta=meta or {},
        )
        self.session.add(call)
        await self.session.flush()
        await self._add_event(call.id, "call.created", {"phone_to": phone_to})
        return call

    async def mark_started(self, call_id: int, external_id: Optional[str] = None) -> None:
        await self.session.execute(
            update(VoipCall)
            .where(VoipCall.id == call_id)
            .values(status="in_progress", external_id=external_id, started_at=dt.datetime.utcnow())
        )
        await self._add_event(call_id, "call.started", {"external_id": external_id})

    async def mark_finished(self, call_id: int, ok: bool, **payload: Any) -> None:
        await self.session.execute(
            update(VoipCall)
            .where(VoipCall.id == call_id)
            .values(status="finished" if ok else "failed", ended_at=dt.datetime.utcnow())
        )
        await self._add_event(call_id, "call.finished", {"ok": ok, **payload})

    async def add_dtmf(self, call_id: int, digits: str) -> None:
        await self.session.execute(
            update(VoipCall)
            .where(VoipCall.id == call_id)
            .values(dtmf_digits=(digits))
        )
        await self._add_event(call_id, "dtmf.received", {"digits": digits})

    async def _add_event(self, call_id: int, event_type: str, payload: Optional[dict[str, Any]] = None) -> None:
        event = VoipEvent(call_id=call_id, type=event_type, payload=payload or {})
        self.session.add(event)
        await self.session.flush()

    # Convenience wrappers for simulated provider
    async def start_call(self, candidate_id: Optional[int], phone_to: str) -> int:
        """Start a simulated outbound call and return call_id."""
        call = await self.create_outbound_call(phone_to=phone_to, candidate_id=candidate_id)
        await self.session.commit()
        return call.id

    async def finish_call(self, call_id: int, reason: str = "completed") -> None:
        """Finish a simulated call with a given reason (completed/failed)."""
        ok = reason in {"completed", "ok", "done", "success"}
        await self.mark_finished(call_id, ok=ok, reason=reason)
        await self.session.commit()


