from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..db import get_session
from ..models import ContactEvent


router = APIRouter(prefix="/api/contacts", tags=["contacts"])


class EventCreate(BaseModel):
    candidate_id: int
    type: str
    meta: Optional[Dict[str, Any]] = None


@router.post("")
async def create_event(ev: EventCreate, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    e = ContactEvent(candidate_id=ev.candidate_id, type=ev.type, meta=ev.meta)
    session.add(e)
    await session.commit()
    await session.refresh(e)
    return {"id": e.id}


@router.get("/{candidate_id}")
async def list_events(candidate_id: int, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    res = await session.execute(select(ContactEvent).where(ContactEvent.candidate_id == candidate_id).order_by(ContactEvent.created_at.desc()))
    items: List[Dict[str, Any]] = []
    for e in res.scalars().all():
        items.append({
            "id": e.id,
            "type": e.type,
            "meta": e.meta,
            "created_at": e.created_at,
        })
    return {"items": items}


