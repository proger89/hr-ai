from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Vacancy, Candidate


router = APIRouter(prefix="/api/match", tags=["match"])


def _infer_keywords(text: str) -> list[str]:
    text_l = (text or "").lower()
    keywords = [
        "python",
        "fastapi",
        "asyncio",
        "postgres",
        "sql",
        "docker",
        "kubernetes",
        "ml",
        "pandas",
        "numpy",
        "nlp",
        "golang",
        "java",
        "react",
        "kafka",
    ]
    return [k for k in keywords if k in text_l]


def _vacancy_keywords(v: Vacancy) -> list[str]:
    if isinstance(v.jd_json, dict):
        k = v.jd_json.get("keywords")
        if isinstance(k, list):
            return [str(x).lower() for x in k]
    return _infer_keywords(f"{v.title} {v.jd_raw}")


def _candidate_keywords(c: Candidate) -> list[str]:
    if isinstance(c.tags, dict):
        k = c.tags.get("skills")
        if isinstance(k, list):
            return [str(x).lower() for x in k]
    return _infer_keywords(f"{c.name} {c.source}")


def _score(vk: list[str], ck: list[str]) -> Dict[str, Any]:
    vs = set(vk)
    cs = set(ck)
    overlap = sorted(vs & cs)
    missing = sorted(vs - cs)
    extra = sorted(cs - vs)
    base = len(vs) or 1
    score = round(len(overlap) / base, 4)
    return {
        "score": score,
        "overlap": overlap,
        "missing": missing,
        "extra": extra,
    }


class ScoreResponse(BaseModel):
    vacancy_id: int
    candidate_id: int
    score: float
    overlap: List[str]
    missing: List[str]
    extra: List[str]
    keywords_vacancy: List[str]
    keywords_candidate: List[str]


@router.get("/score", response_model=ScoreResponse)
async def score(
    vacancy_id: int,
    candidate_id: int,
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    v = await session.get(Vacancy, vacancy_id)
    if not v:
        raise HTTPException(status_code=404, detail="vacancy not found")
    c = await session.get(Candidate, candidate_id)
    if not c:
        raise HTTPException(status_code=404, detail="candidate not found")
    vk = sorted(set(_vacancy_keywords(v)))
    ck = sorted(set(_candidate_keywords(c)))
    met = _score(vk, ck)
    return {
        "vacancy_id": v.id,
        "candidate_id": c.id,
        "keywords_vacancy": vk,
        "keywords_candidate": ck,
        **met,
    }


class ShortlistRequest(BaseModel):
    vacancy_id: int
    candidate_ids: Optional[List[int]] = None
    top_k: int = 5


class ShortlistItem(BaseModel):
    candidate_id: int
    score: float


class ShortlistResponse(BaseModel):
    vacancy_id: int
    items: List[ShortlistItem]


@router.post("/shortlist", response_model=ShortlistResponse)
async def shortlist(
    req: ShortlistRequest,
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    v = await session.get(Vacancy, req.vacancy_id)
    if not v:
        raise HTTPException(status_code=404, detail="vacancy not found")
    vk = sorted(set(_vacancy_keywords(v)))

    if req.candidate_ids:
        result = await session.execute(select(Candidate).where(Candidate.id.in_(req.candidate_ids)))
    else:
        result = await session.execute(select(Candidate))
    candidates = list(result.scalars())

    items: list[dict[str, Any]] = []
    for c in candidates:
        ck = sorted(set(_candidate_keywords(c)))
        sc = _score(vk, ck)["score"]
        items.append({"candidate_id": c.id, "score": sc})

    items.sort(key=lambda x: x["score"], reverse=True)
    top = items[: max(1, req.top_k)]
    return {"vacancy_id": v.id, "items": top}


# Detailed scoring for multiple candidates with TOP-K
class ScoreAndShortlistRequest(BaseModel):
    vacancy_id: int
    candidate_ids: Optional[List[int]] = None
    top_k: int = 5


@router.post("/score_and_shortlist", response_model=List[ScoreResponse])
async def score_and_shortlist(
    req: ScoreAndShortlistRequest,
    session: AsyncSession = Depends(get_session),
) -> List[dict[str, Any]]:
    v = await session.get(Vacancy, req.vacancy_id)
    if not v:
        raise HTTPException(status_code=404, detail="vacancy not found")
    vk = sorted(set(_vacancy_keywords(v)))

    if req.candidate_ids:
        result = await session.execute(select(Candidate).where(Candidate.id.in_(req.candidate_ids)))
    else:
        result = await session.execute(select(Candidate))
    candidates = list(result.scalars())

    scored: list[dict[str, Any]] = []
    for c in candidates:
        ck = sorted(set(_candidate_keywords(c)))
        met = _score(vk, ck)
        scored.append(
            {
                "vacancy_id": v.id,
                "candidate_id": c.id,
                "keywords_vacancy": vk,
                "keywords_candidate": ck,
                **met,
            }
        )

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[: max(1, req.top_k)]

