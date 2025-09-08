from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Candidate, Invitation, Booking


router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("")
async def get_stats(session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    # candidates passed: tags.decision == 'yes'
    # Используем PostgreSQL JSON ->> (astext) для корректного сравнения строки
    # SQLAlchemy 2.x: JSON -> text extraction via as_string() for PG
    passed = (
        await session.execute(
            select(func.count(Candidate.id)).where(Candidate.tags["decision"].as_string() == "yes")
        )
    ).scalar() or 0
    total = (await session.execute(select(func.count(Candidate.id)))).scalar() or 0
    invites = (await session.execute(select(func.count(Invitation.id)))).scalar() or 0
    scheduled = (await session.execute(select(func.count(Booking.id)).where(Booking.status == "booked"))).scalar() or 0
    return {
        "candidates_total": int(total),
        "candidates_passed": int(passed),
        "invites_total": int(invites),
        "slots_booked": int(scheduled),
    }


