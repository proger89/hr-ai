from __future__ import annotations

from typing import List

from .openai_service import get_embeddings as openai_get_embeddings


def get_embeddings(texts: List[str], model: str = "Embeddings:latest") -> List[List[float]]:
    """Get embeddings using OpenAI API."""
    if not texts:
        return []
    # Используем OpenAI embeddings
    return openai_get_embeddings(texts)


