from __future__ import annotations

from typing import List, Dict, Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import math

from ..services.embeddings import get_embeddings


router = APIRouter(prefix="/api/agent", tags=["agent"])


class ScoreRequest(BaseModel):
    answer: str
    rubric: List[str]


class ScoreResponse(BaseModel):
    score: float
    similarities: List[float]


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@router.post("/score", response_model=ScoreResponse)
def score(req: ScoreRequest) -> Dict[str, Any]:
    try:
        texts: List[str] = [req.answer] + req.rubric
        vectors = get_embeddings(texts)
        ans_vec, rubric_vecs = vectors[0], vectors[1:]
        sims = [_cosine(ans_vec, v) for v in rubric_vecs]
        score = max(sims) if sims else 0.0
        return {"score": float(score), "similarities": [float(s) for s in sims]}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


