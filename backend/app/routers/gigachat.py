from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import List, Literal, Dict, Any

from ..services.openai_service import chat_completion, get_embeddings


router = APIRouter(prefix="/api/gigachat", tags=["gigachat"])


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    model: str = Field(default="GigaChat:latest")


@router.post("/chat")
def chat(req: ChatRequest) -> Dict[str, Any]:
    try:
        messages = [m.model_dump() for m in req.messages]
        data = chat_completion(messages)
        return data
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


class EmbeddingsRequest(BaseModel):
    input: List[str]
    model: str = Field(default="Embeddings:latest")


@router.post("/embeddings")
def embeddings(req: EmbeddingsRequest) -> Dict[str, Any]:
    try:
        embeddings_data = get_embeddings(req.input)
        # Форматируем ответ в стиле OpenAI
        return {
            "data": [
                {"embedding": emb, "index": i}
                for i, emb in enumerate(embeddings_data)
            ],
            "model": "text-embedding-3-small"
        }
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


