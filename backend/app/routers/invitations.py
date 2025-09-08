from __future__ import annotations

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import secrets
import time
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..db import get_session
from ..models import InviteToken
from ..config import settings
from ..security import sign_jwt_like, verify_jwt_like


router = APIRouter(prefix="/api/invitations", tags=["invitations"])


class InvitationBulkRequest(BaseModel):
    vacancy_id: str
    candidate_ids: List[str]
    modes: List[str] = Field(default_factory=list, description="pml, uvl, scheduler")
    slots: Optional[List[str]] = None


class Invitation(BaseModel):
    candidate_id: str
    pml_url: Optional[str] = None
    uvl: Optional[str] = None
    code: Optional[str] = None
    slots: List[str] = Field(default_factory=list)


def _short_code(n: int = 6) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


async def _issue_token(session: AsyncSession, *, vacancy_id: Optional[str], cand_id: str, mode: str, ttl_days: int = 7) -> str:
    jti = secrets.token_urlsafe(8)
    exp = int(time.time()) + ttl_days * 24 * 3600
    claims = {"jti": jti, "vid": vacancy_id or "", "cid": cand_id, "mode": mode, "exp": exp}
    token = sign_jwt_like(claims, settings.auth_secret)
    # persist jti for replay protection
    # Try to parse numeric vacancy id; store NULL if not provided or not numeric
    vid_digits = None
    try:
        _dig = ''.join(filter(str.isdigit, vacancy_id or ''))
        vid_digits = int(_dig) if _dig else None
    except Exception:
        vid_digits = None
    it = InviteToken(jti=jti, candidate_id=int(cand_id), vacancy_id=vid_digits, mode=mode, exp=datetime.fromtimestamp(exp, tz=timezone.utc))
    session.add(it)
    await session.commit()
    return token


async def _make_pml(session: AsyncSession, vacancy_id: Optional[str], cand_id: str) -> str:
    tok = await _issue_token(session, vacancy_id=vacancy_id, cand_id=cand_id, mode="pml")
    path_vid = (vacancy_id or 'CV').strip('/') or 'CV'
    return f"/i/{path_vid}/start?t={tok}&cid={cand_id}"


def _make_uvl(vacancy_id: str) -> str:
    return f"/v/{vacancy_id}"


@router.post("/bulk")
async def bulk_invites(req: InvitationBulkRequest, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    if not req.candidate_ids:
        raise HTTPException(status_code=400, detail="candidate_ids is empty")

    modes = {m.lower() for m in (req.modes or [])}
    include_pml = (not modes) or ("pml" in modes)
    include_uvl = (not modes) or ("uvl" in modes)
    include_scheduler = ("scheduler" in modes)

    invitations: List[Invitation] = []
    for cid in req.candidate_ids:
        inv = Invitation(candidate_id=cid)
        if include_pml:
            inv.pml_url = await _make_pml(session, req.vacancy_id, cid)
        if include_uvl:
            inv.uvl = _make_uvl(req.vacancy_id)
            inv.code = f"{req.vacancy_id}-{_short_code(4)}"
        if include_scheduler:
            inv.slots = (req.slots or ["tomorrow 11:00", "tomorrow 14:00", "+2d 10:00"])[:5]
        invitations.append(inv)

    return {"invitations": [i.model_dump() for i in invitations]}


class VerifyRequest(BaseModel):
    token: str


@router.post("/verify")
async def verify_invite(req: VerifyRequest, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    claims, ok = verify_jwt_like(req.token, settings.auth_secret)
    if not ok:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    jti = claims.get("jti")
    if not jti:
        raise HTTPException(status_code=400, detail="bad token")
    q = await session.execute(select(InviteToken).where(InviteToken.jti == jti))
    row = q.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="token not found")
    if row.used_at is not None:
        raise HTTPException(status_code=409, detail="token already used")
    # Mark as used
    row.used_at = datetime.now(timezone.utc)
    await session.commit()
    return {"ok": True, "claims": claims}


