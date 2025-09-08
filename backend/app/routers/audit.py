from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import AuditLog
from ..security import verify_jwt_like
from ..config import settings


router = APIRouter(prefix="/api/audit", tags=["audit"])


class AuditCreate(BaseModel):
    action: str
    meta: Optional[Dict[str, Any]] = None


def _user_from_auth(header: Optional[str]) -> Optional[str]:
    if not header:
        return None
    try:
        if header.lower().startswith('bearer '):
            token = header.split(' ', 1)[1]
        else:
            token = header
        # our login token is HMAC v1, not JWT; we only accept it here for simplicity
        # try to extract username from payload
        parts = token.split('.')
        if len(parts) == 3 and parts[0] == 'v1':
            payload = parts[1]
            kv = dict(item.split('=', 1) for item in payload.split(';'))
            return kv.get('u')
    except Exception:
        return None
    return None


@router.post("")
async def create_audit(body: AuditCreate, authorization: Optional[str] = Header(default=None), session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    user = _user_from_auth(authorization)
    log = AuditLog(user=user, action=body.action, meta=body.meta)
    session.add(log)
    await session.commit()
    await session.refresh(log)
    return {"id": log.id}


@router.get("")
async def list_audit(limit: int = 100, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    limit = max(1, min(500, limit))
    res = await session.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit))
    items: List[Dict[str, Any]] = []
    for x in res.scalars().all():
        items.append({
            "id": x.id,
            "user": x.user,
            "action": x.action,
            "meta": x.meta,
            "created_at": x.created_at,
        })
    return {"items": items}


