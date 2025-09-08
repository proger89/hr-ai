from __future__ import annotations

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Dict, Any, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..db import get_session
from ..models import Candidate
from sqlalchemy import update
from .upload import _plain_text  # reuse PDF/DOCX/TXT extraction


router = APIRouter(prefix="/api/candidates", tags=["candidates"])


@router.get("")
async def list_candidates(vacancy_id: Optional[int] = Query(default=None), session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    q = select(Candidate).order_by(Candidate.created_at.desc())
    res = await session.execute(q)
    items: List[Dict[str, Any]] = []
    for c in res.scalars().all():
        tags = c.tags or {}
        v_id = (tags.get("vacancy_id") if isinstance(tags, dict) else None)
        if vacancy_id is not None and v_id != vacancy_id:
            continue
        items.append({
            "id": c.id,
            "name": c.name,
            "created_at": c.created_at,
            "email": c.email,
            "phone": c.phone,
            "pml_url": tags.get("pml_url"),
            "vacancy_id": v_id,
            "vacancy_title": tags.get("vacancy_title"),
            "match_pct": (tags.get("summary", {}) or {}).get("match_pct"),
            "status": tags.get("status") or "ready",
            "interview_completed": tags.get("interview_completed", False),
            "interview_passed": tags.get("interview_passed", False),
            "interview_score": tags.get("interview_score"),
        })
    return {"items": items}


@router.get("/")
async def list_candidates_slash(session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    # alias для совместимости со слэшем
    return await list_candidates(session)


@router.get("/{candidate_id}/resume_text")
async def resume_text(candidate_id: int, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    cand = await session.get(Candidate, candidate_id)
    if not cand:
        raise HTTPException(status_code=404, detail="candidate not found")
    tags = cand.tags or {}
    path = tags.get("cv_path")
    if not path:
        raise HTTPException(status_code=404, detail="cv not found")
    try:
        text = _plain_text(path)
    except Exception:
        text = ""
    return {"id": cand.id, "name": cand.name, "vacancy_id": tags.get("vacancy_id"), "vacancy_title": tags.get("vacancy_title"), "text": text or ""}


@router.get("/{candidate_id}")
async def get_candidate(candidate_id: int, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    cand = await session.get(Candidate, candidate_id)
    if not cand:
        raise HTTPException(status_code=404, detail="candidate not found")
    tags = cand.tags or {}
    profile = {
        "summary": {
            "name": cand.name,
            "position": tags.get("position"),
            "match_pct": (tags.get("summary", {}) or {}).get("match_pct", 0.0),
            "decision": tags.get("decision", "pending"),
            "strengths": tags.get("strengths", []),
            "gaps": tags.get("gaps", []),
            "flags": tags.get("flags", []),
        },
        "scores": tags.get("scores", {"total": 0.0, "by_comp": {}}),
        "quotes": tags.get("quotes", []),
        "transcript": tags.get("transcript", []),
        "resume_url": tags.get("cv_path"),
        "jd": tags.get("jd"),
        "flags": tags.get("flags", []),
    }
    return profile


@router.post("/{candidate_id}/status")
async def set_status(candidate_id: int, body: Dict[str, Any], session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    cand = await session.get(Candidate, candidate_id)
    if not cand:
        raise HTTPException(status_code=404, detail="candidate not found")
    tags = cand.tags or {}
    tags["status"] = body.get("status", "new")
    cand.tags = tags
    await session.commit()
    return {"ok": True}


@router.delete("/{candidate_id}")
async def delete_candidate(candidate_id: int, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    cand = await session.get(Candidate, candidate_id)
    if not cand:
        raise HTTPException(status_code=404, detail="candidate not found")
    await session.delete(cand)
    await session.commit()
    return {"ok": True}


@router.get("/{candidate_id}/resume")
async def get_candidate_resume(candidate_id: str, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    """Получить резюме кандидата для интервью"""
    # Преобразуем ID если нужно
    try:
        cand_id = int(candidate_id)
    except:
        cand_id = candidate_id
        
    candidate = await session.get(Candidate, cand_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Кандидат не найден")
    
    return {
        "candidate_id": str(cand_id),
        "name": candidate.name,
        "vacancy_id": candidate.tags.get("vacancy_id") if candidate.tags else None,
        "cv_text": candidate.tags.get("cv_text", "") if candidate.tags else "",
        "keywords": candidate.tags.get("keywords", []) if candidate.tags else [],
        "pml": candidate.tags.get("pml", "") if candidate.tags else ""
    }

