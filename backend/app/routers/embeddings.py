from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..db import get_session
from ..models import Embedding


router = APIRouter(prefix="/api/embeddings", tags=["embeddings"])


class PutEmbedding(BaseModel):
    kind: str  # 'cv' | 'jd'
    ref_id: int
    vector: List[float]


@router.post("")
async def put_embedding(req: PutEmbedding, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    if not req.vector or not isinstance(req.vector, list):
        raise HTTPException(status_code=400, detail="vector is required")
    e = Embedding(kind=req.kind, ref_id=req.ref_id, vec=[float(x) for x in req.vector])
    session.add(e)
    await session.commit()
    await session.refresh(e)
    return {"id": e.id}


class SearchRequest(BaseModel):
    kind: str
    vector: List[float]
    top_k: int = 5


@router.post("/search")
async def search(req: SearchRequest, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    res = await session.execute(select(Embedding).where(Embedding.kind == req.kind))
    items = res.scalars().all()
    q = req.vector

    def dot(a: List[float], b: List[float]) -> float:
        n = min(len(a), len(b))
        s = 0.0
        for i in range(n):
            s += float(a[i]) * float(b[i])
        return s

    scored = [
        {"id": e.id, "ref_id": e.ref_id, "score": dot(q, e.vec or [])}
        for e in items
    ]
    scored.sort(key=lambda x: x["score"], reverse=True)
    return {"items": scored[: max(1, int(req.top_k))]}


