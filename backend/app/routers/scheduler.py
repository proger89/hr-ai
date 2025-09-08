from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Slot, Booking, Vacancy, Candidate


router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


class SlotCreate(BaseModel):
    vacancy_id: int
    start_at: datetime
    end_at: datetime
    capacity: int = 1


@router.post("/slot")
async def create_slot(payload: SlotCreate, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    v = await session.get(Vacancy, payload.vacancy_id)
    if not v:
        raise HTTPException(status_code=404, detail="vacancy not found")
    s = Slot(
        vacancy_id=payload.vacancy_id,
        start_at=payload.start_at,
        end_at=payload.end_at,
        capacity=max(1, payload.capacity),
    )
    session.add(s)
    await session.commit()
    await session.refresh(s)
    return {"slot_id": s.id}


@router.get("/slots")
async def list_slots(vacancy_id: int, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    result = await session.execute(select(Slot).where(Slot.vacancy_id == vacancy_id).order_by(Slot.start_at))
    slots = [
        {
            "id": s.id,
            "start_at": s.start_at,
            "end_at": s.end_at,
            "capacity": s.capacity,
        }
        for s in result.scalars().all()
    ]
    return {"items": slots}


@router.get("/slot/{slot_id}/ics")
async def slot_ics(slot_id: int, session: AsyncSession = Depends(get_session)):
    s = await session.get(Slot, slot_id)
    if not s:
        raise HTTPException(status_code=404, detail="slot not found")
    start = s.start_at.strftime("%Y%m%dT%H%M%SZ") if hasattr(s.start_at, 'strftime') else str(s.start_at)
    end = s.end_at.strftime("%Y%m%dT%H%M%SZ") if hasattr(s.end_at, 'strftime') else str(s.end_at)
    uid = f"slot-{s.id}@sber-interviewer"
    ics = (
        "BEGIN:VCALENDAR\r\n" "VERSION:2.0\r\n" "PRODID:-//Sber Interviewer//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTART:{start}\r\n"
        f"DTEND:{end}\r\n"
        f"SUMMARY:Interview slot #{s.id}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    from fastapi.responses import Response
    return Response(content=ics, media_type="text/calendar")


class BookRequest(BaseModel):
    slot_id: int
    candidate_id: Optional[int] = None


@router.post("/book")
async def book(payload: BookRequest, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    s = await session.get(Slot, payload.slot_id)
    if not s:
        raise HTTPException(status_code=404, detail="slot not found")

    # capacity check
    result = await session.execute(select(func.count(Booking.id)).where(Booking.slot_id == s.id, Booking.status == "booked"))
    booked_count = int(result.scalar() or 0)
    if booked_count >= s.capacity:
        raise HTTPException(status_code=409, detail="slot is full")

    # optional candidate check
    if payload.candidate_id:
        cand = await session.get(Candidate, payload.candidate_id)
        if not cand:
            raise HTTPException(status_code=404, detail="candidate not found")

    code = "S-" + secrets.token_urlsafe(6)
    b = Booking(slot_id=s.id, candidate_id=payload.candidate_id, status="booked", code=code)
    session.add(b)
    await session.commit()
    await session.refresh(b)
    return {"booking_id": b.id, "code": b.code}


@router.get("/slot/{slot_id}/bookings")
async def list_bookings(slot_id: int, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    result = await session.execute(select(Booking).where(Booking.slot_id == slot_id).order_by(Booking.created_at))
    items = [
        {
            "id": b.id,
            "candidate_id": b.candidate_id,
            "status": b.status,
            "code": b.code,
            "created_at": b.created_at,
        }
        for b in result.scalars().all()
    ]
    return {"items": items}


