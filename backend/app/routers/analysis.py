from __future__ import annotations

from typing import Any, Dict, Optional, List
import re

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..db import get_session
from ..models import Candidate, Vacancy


router = APIRouter(prefix="/api/analysis", tags=["analysis"])


class Weights(BaseModel):
    tech: float = 0.4
    comm: float = 0.3
    cases: float = 0.3


class ScoreRequest(BaseModel):
    candidate_id: int
    vacancy_id: Optional[int] = None
    weights: Weights = Field(default_factory=Weights)


class ScoreResponse(BaseModel):
    candidate_id: int
    vacancy_id: Optional[int]
    tech: float
    comm: float
    cases: float
    total: float
    strengths: List[str]
    gaps: List[str]
    quotes: List[Dict[str, Any]] | None = None


def _norm_weights(w: Weights) -> Weights:
    s = max(1e-6, (w.tech + w.comm + w.cases))
    return Weights(tech=w.tech / s, comm=w.comm / s, cases=w.cases / s)


def _score_tech(cand: Candidate, vac: Optional[Vacancy]) -> tuple[float, List[str], List[str]]:
    skills = []
    if isinstance(cand.tags, dict):
        skills = list(map(str, cand.tags.get("skills", []) or []))
    skills_l = [s.lower() for s in skills]
    jd_keywords: List[str] = []
    if vac is not None and isinstance(vac.jd_json, dict):
        jd_keywords = list(map(str, vac.jd_json.get("keywords", []) or []))
    jd_l = [k.lower() for k in jd_keywords]
    if not jd_l:
        # если нет JD, оценим по количеству скиллов
        score = min(1.0, len(skills_l) / 10.0)
        return score, skills[:3], []
    overlap = [s for s in skills_l if s in jd_l]
    score = (len(overlap) / max(1, len(jd_l)))
    strengths = [s for s in skills if s.lower() in set(overlap)][:3]
    gaps = [k for k in jd_keywords if k.lower() not in set(skills_l)][:3]
    return score, strengths, gaps


def _concat_transcript(tags: dict) -> str:
    tx = tags.get("transcript") or []
    if isinstance(tx, list):
        parts: List[str] = []
        for row in tx:
            try:
                parts.append(str(row.get("text") or ""))
            except Exception:
                continue
        return "\n".join(parts)
    return ""


def _score_comm_and_cases(cand: Candidate) -> tuple[float, float]:
    tags = cand.tags or {}
    text = (cand.name or "") + "\n" + _concat_transcript(tags)
    tl = text.lower()
    # communication keywords heuristic
    comm_kw = ["команд", "обсужд", "договор", "обратн", "feedback", "team", "communicat", "mentor", "lead"]
    comm_hits = sum(1 for k in comm_kw if k in tl)
    # cases/impact keywords heuristic
    case_kw = ["случа", "пример", "кейс", "%", "rps", "qps", "latency", "оптимиз", "ускор", "снизили", "повысили"]
    case_hits = sum(1 for k in case_kw if k in tl)
    # normalize
    comm = max(0.0, min(1.0, comm_hits / 5.0))
    cases = max(0.0, min(1.0, case_hits / 5.0))
    return comm, cases


def _compute_speech_metrics(tags: dict) -> Dict[str, Any]:
    tx = tags.get("transcript") or []
    utter = []
    for row in (tx if isinstance(tx, list) else []):
        try:
            if (row.get("role") or "").lower().startswith("c"):
                t0 = row.get("t0"); t1 = row.get("t1")
                if isinstance(t0, (int, float)) and isinstance(t1, (int, float)) and t1 > t0:
                    utter.append((float(t0), float(t1), str(row.get("text") or "")))
        except Exception:
            continue
    utter.sort(key=lambda x: x[0])
    total_s = sum((u[1]-u[0]) for u in utter if u[1] > u[0])
    words = 0
    fillers = 0
    filler_rx = re.compile(r"\b(э+|мм+|ну|как бы|типа)\b", re.IGNORECASE)
    for _, _, text in utter:
        words += len([w for w in re.findall(r"\b\w+\b", text, re.UNICODE)])
        fillers += len(filler_rx.findall(text or ""))
    wpm = (words / (total_s/60.0)) if total_s > 1.0 else 0.0
    # pauses between utterances > 600ms
    pauses = []
    for i in range(1, len(utter)):
        gap = utter[i][0] - utter[i-1][1]
        if gap >= 0.6:
            pauses.append(gap)
    avg_pause_ms = int(1000.0 * (sum(pauses)/len(pauses))) if pauses else 0
    return {"wpm": round(wpm, 1), "avg_pause_ms": avg_pause_ms, "fillers": fillers, "utterances": len(utter)}


@router.post("/score", response_model=ScoreResponse)
async def score(req: ScoreRequest, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    cand = await session.get(Candidate, req.candidate_id)
    if not cand:
        raise HTTPException(status_code=404, detail="candidate not found")
    vac = await session.get(Vacancy, req.vacancy_id) if req.vacancy_id else None

    tech, strengths, gaps = _score_tech(cand, vac)
    comm, cases = _score_comm_and_cases(cand)
    w = _norm_weights(req.weights)
    total = round(tech * w.tech + comm * w.comm + cases * w.cases, 4)

    # Extract quotes from transcript (simple heuristic: pick lines with %/latency/optimized)
    quotes: List[Dict[str, Any]] = []
    try:
        tx = (cand.tags or {}).get("transcript") or []
        if isinstance(tx, list):
            for row in tx:
                t = (row or {}).get("text") or ""
                if not isinstance(t, str):
                    continue
                tl = t.lower()
                if any(k in tl for k in ["%", "latency", "ускор", "оптимиз", "повысил", "снизил"]):
                    q = {
                        "t0": (row or {}).get("t0"),
                        "t1": (row or {}).get("t1"),
                        "text": t,
                        "url": (row or {}).get("audio_url"),
                    }
                    quotes.append(q)
            quotes = quotes[:5]
    except Exception:
        quotes = []

    # persist into candidate tags
    tags = dict(cand.tags or {})
    tags["scores"] = {"by_comp": {"tech": tech, "comm": comm, "cases": cases}, "total": total}
    summary = dict(tags.get("summary" , {}))
    summary["match_pct"] = total
    summary.setdefault("strengths", strengths)
    summary.setdefault("gaps", gaps)
    tags["summary"] = summary
    # speech metrics
    try:
        tags["speech_metrics"] = _compute_speech_metrics(tags)
    except Exception:
        pass
    # persist JD keywords for UI highlighting
    try:
        jd_keywords: List[str] = []
        if vac is not None and isinstance(vac.jd_json, dict):
            jd_keywords = list(map(str, vac.jd_json.get("keywords", []) or []))
        if jd_keywords:
            tags["jd_keywords"] = jd_keywords
    except Exception:
        pass
    if quotes:
        tags["quotes"] = quotes
    cand.tags = tags
    await session.commit()

    return {
        "candidate_id": cand.id,
        "vacancy_id": req.vacancy_id,
        "tech": round(tech, 4),
        "comm": round(comm, 4),
        "cases": round(cases, 4),
        "total": total,
        "strengths": strengths,
        "gaps": gaps,
        "quotes": quotes or [],
    }


class RankRequest(BaseModel):
    vacancy_id: Optional[int] = None
    candidate_ids: List[int]
    weights: Weights = Field(default_factory=Weights)


@router.post("/rank")
async def rank(req: RankRequest, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    if not req.candidate_ids:
        return {"items": []}
    vac = await session.get(Vacancy, req.vacancy_id) if req.vacancy_id else None
    w = _norm_weights(req.weights)
    items: List[Dict[str, Any]] = []
    for cid in req.candidate_ids:
        cand = await session.get(Candidate, cid)
        if not cand:
            continue
        tech, _, _ = _score_tech(cand, vac)
        comm, cases = _score_comm_and_cases(cand)
        total = round(tech * w.tech + comm * w.comm + cases * w.cases, 4)
        items.append({"candidate_id": cid, "tech": tech, "comm": comm, "cases": cases, "total": total})
    items.sort(key=lambda x: x["total"], reverse=True)
    return {"items": items}


