from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from sqlalchemy import func
from ..models import VoipCall, VoipEvent, Candidate, Slot, Booking, ContactEvent
from ..services.voip import VoipService


router = APIRouter(prefix="/api/voip", tags=["voip"])


class CallCreate(BaseModel):
    phone_to: str
    phone_from: Optional[str] = None
    candidate_id: Optional[int] = None
    vacancy_id: Optional[int] = None
    slot_id: Optional[int] = None
    meta: Optional[dict[str, Any]] = None


@router.post("/call")
async def create_call(payload: CallCreate, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    svc = VoipService(session)
    phone_to = payload.phone_to
    if (not phone_to) and payload.candidate_id:
        cand = await session.get(Candidate, payload.candidate_id)
        if cand and cand.phone:
            phone_to = cand.phone
    if not phone_to:
        raise HTTPException(status_code=400, detail="phone_to or candidate_id with phone required")
    call = await svc.create_outbound_call(
        phone_to=phone_to,
        phone_from=payload.phone_from,
        candidate_id=payload.candidate_id,
        vacancy_id=payload.vacancy_id,
        slot_id=payload.slot_id,
        meta=payload.meta,
    )
    await session.commit()
    return {"call_id": call.id, "status": call.status}


class WebhookEvent(BaseModel):
    call_id: Optional[int] = None
    external_id: Optional[str] = None
    type: str
    digits: Optional[str] = None
    payload: Optional[dict[str, Any]] = None


@router.post("/webhook")
async def provider_webhook(evt: WebhookEvent, session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    svc = VoipService(session)
    if evt.type == "call.started":
        if evt.call_id is None:
            raise HTTPException(status_code=400, detail="call_id required")
        await svc.mark_started(evt.call_id, evt.external_id)
        # контакт: звонок начался
        call = await session.get(VoipCall, evt.call_id)
        if call and call.candidate_id:
            session.add(ContactEvent(candidate_id=call.candidate_id, type="call_started", meta={"call_id": call.id}))
    elif evt.type == "dtmf.received":
        if evt.call_id is None or not evt.digits:
            raise HTTPException(status_code=400, detail="call_id and digits required")
        await svc.add_dtmf(evt.call_id, evt.digits)
        # Определяем режим IVR: prescreen | slots
        call = await session.get(VoipCall, evt.call_id)
        mode = None
        try:
            mode = (call.meta or {}).get("ivr", {}).get("mode") if call else None
        except Exception:
            mode = None
        if mode == "prescreen":
            data = await _prescreen_answer(session, evt.call_id, evt.digits.strip())
            await svc._add_event(evt.call_id, "prescreen.answer", {"digits": evt.digits.strip(), "done": data.get("finished", False)})
            await session.commit()
            # Гарантируем валидный JSON для фронта
            resp = {"status": "ok"}
            resp.update(data)
            return resp
        else:
            # Режим слотов: интерпретируем цифру как выбор N, 0 — повтор подсказки
            try:
                n = int(evt.digits.strip()[0])
            except Exception:
                n = 0
            if n == 0:
                data = await _compute_ivr(session, evt.call_id)
                await svc._add_event(evt.call_id, "ivr.repeat", {})
                await session.commit()
                return {"status": "ok", "prompt": data.get("prompt"), "options": data.get("options", [])}
            if n > 0 and call and call.vacancy_id:
                q = select(Slot).where(Slot.vacancy_id == call.vacancy_id).order_by(Slot.start_at)
                slots = (await session.execute(q)).scalars().all()
                chosen: Optional[Slot] = slots[n - 1] if len(slots) >= n else None
                if chosen is not None:
                    booked_cnt = (await session.execute(
                        select(func.count(Booking.id)).where(Booking.slot_id == chosen.id, Booking.status == "booked")
                    )).scalar() or 0
                    if int(booked_cnt) < int(chosen.capacity):
                        b = Booking(slot_id=chosen.id, candidate_id=call.candidate_id, status="booked")
                        session.add(b)
                        await session.flush()
                        await svc._add_event(call.id, "slot.chosen", {"slot_id": chosen.id, "booking_id": b.id})
                        if call.candidate_id:
                            session.add(ContactEvent(candidate_id=call.candidate_id, type="slot_chosen", meta={"slot_id": chosen.id, "booking_id": b.id}))
                        await session.commit()
                        return {"status": "ok", "booking_id": b.id, "slot_id": chosen.id}
    elif evt.type in {"call.finished", "call.failed"}:
        if evt.call_id is None:
            raise HTTPException(status_code=400, detail="call_id required")
        await svc.mark_finished(evt.call_id, ok=(evt.type == "call.finished"), **(evt.payload or {}))
        # контакт: звонок завершён/ошибка
        call = await session.get(VoipCall, evt.call_id)
        if call and call.candidate_id:
            session.add(ContactEvent(candidate_id=call.candidate_id, type=("call_finished" if evt.type == "call.finished" else "call_failed"), meta={"call_id": call.id}))
    else:
        raise HTTPException(status_code=400, detail="unknown event type")
    await session.commit()
    return {"status": "ok"}


@router.get("/call/{call_id}")
async def get_call(call_id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    res = await session.execute(select(VoipCall).where(VoipCall.id == call_id))
    call = res.scalar_one_or_none()
    if call is None:
        raise HTTPException(status_code=404, detail="not found")
    events = (await session.execute(select(VoipEvent).where(VoipEvent.call_id == call_id))).scalars().all()
    return {
        "call": {
            "id": call.id,
            "status": call.status,
            "provider": call.provider,
            "external_id": call.external_id,
            "phone_from": call.phone_from,
            "phone_to": call.phone_to,
            "dtmf_digits": call.dtmf_digits,
            "meta": call.meta,
            "started_at": call.started_at,
            "ended_at": call.ended_at,
        },
        "events": [{"id": e.id, "type": e.type, "payload": e.payload, "created_at": e.created_at} for e in events],
    }


@router.get("/calls")
async def list_calls(
    candidate_id: Optional[int] = None,
    vacancy_id: Optional[int] = None,
    limit: int = 20,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    q = select(VoipCall).order_by(VoipCall.created_at.desc())
    if candidate_id:
        q = q.where(VoipCall.candidate_id == candidate_id)
    if vacancy_id:
        q = q.where(VoipCall.vacancy_id == vacancy_id)
    res = await session.execute(q.limit(max(1, min(limit, 100))))
    calls = []
    for c in res.scalars().all():
        calls.append({
            "id": c.id,
            "status": c.status,
            "provider": c.provider,
            "phone_to": c.phone_to,
            "candidate_id": c.candidate_id,
            "vacancy_id": c.vacancy_id,
            "dtmf_digits": c.dtmf_digits,
            "started_at": c.started_at,
            "ended_at": c.ended_at,
            "created_at": c.created_at,
        })
    return {"items": calls}


async def _compute_ivr(session: AsyncSession, call_id: int) -> dict[str, Any]:
    call = await session.get(VoipCall, call_id)
    if not call:
        raise HTTPException(status_code=404, detail="call not found")
    if not call.vacancy_id:
        raise HTTPException(status_code=400, detail="vacancy_id required on call")
    q = select(Slot).where(Slot.vacancy_id == call.vacancy_id).order_by(Slot.start_at)
    slots = (await session.execute(q)).scalars().all()
    options = []
    for idx, s in enumerate(slots[:5], start=1):
        options.append({
            "digit": str(idx),
            "slot_id": s.id,
            "start_at": s.start_at,
            "label": _format_slot_ru(s),
        })
    if not options:
        prompt = "К сожалению, свободных слотов нет. Попробуйте позже."
    else:
        listed = ", ".join([f"нажмите {o['digit']} — {o['label']}" for o in options])
        prompt = f"Здравствуйте. Для записи на интервью {listed}. Для повтора нажмите ноль."
    meta = dict(call.meta or {})
    meta["ivr"] = {"options": options}
    call.meta = meta
    await session.flush()
    return {"prompt": prompt, "options": options}


def _format_slot_ru(s: Slot) -> str:
    try:
        start = s.start_at
        return start.strftime("%d.%m %H:%M") if hasattr(start, 'strftime') else str(start)
    except Exception:
        return str(getattr(s, 'start_at', ''))


@router.get("/ivr/next")
async def ivr_next(call_id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    data = await _compute_ivr(session, call_id)
    await session.commit()
    return {"call_id": call_id, **data, "expect": "dtmf"}


class PrescreenStart(BaseModel):
    call_id: int
    questions: Optional[list[str]] = None  # если не переданы — используем дефолтные 3


@router.post("/prescreen/start")
async def prescreen_start(payload: PrescreenStart, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    call = await session.get(VoipCall, payload.call_id)
    if not call:
        raise HTTPException(status_code=404, detail="call not found")
    qs = payload.questions or [
        "Готовы ли вы к работе в офисе 3 дня в неделю? 1 — да, 2 — нет",
        "Ваш уровень английского: 1 — А2 и ниже, 2 — B1, 3 — B2 и выше",
        "Ожидаемый уровень компенсации: 1 — до 150, 2 — 150–250, 3 — 250+",
    ]
    meta = dict(call.meta or {})
    meta["ivr"] = {"mode": "prescreen", "q": qs, "i": 0, "answers": []}
    call.meta = meta
    await session.flush()
    # contact log: prescreen started
    if call.candidate_id:
        session.add(ContactEvent(candidate_id=call.candidate_id, type="prescreen_started", meta={"call_id": call.id}))
    await session.commit()
    return {"status": "ok", "prompt": qs[0], "expect": "dtmf"}


async def _prescreen_answer(session: AsyncSession, call_id: int, digits: str) -> dict[str, Any]:
    call = await session.get(VoipCall, call_id)
    if not call:
        raise HTTPException(status_code=404, detail="call not found")
    meta = dict(call.meta or {})
    st = meta.get("ivr", {})
    qs = st.get("q", [])
    idx = int(st.get("i", 0))
    ans = list(st.get("answers", []))
    if not qs or idx >= len(qs):
        return {"finished": True}
    ans.append(digits)
    idx += 1
    finished = idx >= len(qs)
    st.update({"i": idx, "answers": ans})
    meta["ivr"] = st
    call.meta = meta
    await session.flush()
    if finished:
        # contact log: prescreen finished
        if call.candidate_id:
            session.add(ContactEvent(candidate_id=call.candidate_id, type="prescreen_finished", meta={"answers": ans}))
        return {"finished": True, "summary": ans}
    return {"finished": False, "prompt": qs[idx], "expect": "dtmf"}
