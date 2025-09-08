"""OpenAI API integration service for LLM and embeddings."""
import os
import logging
from typing import List, Dict, Any, Optional
from openai import OpenAI

logger = logging.getLogger(__name__)

# Initialize OpenAI client
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

# Model configurations
CHAT_MODEL = "gpt-4o-mini"  # Возвращаемся к проверенной модели
EMBEDDING_MODEL = "text-embedding-3-small"  # Дешевле чем ada-002, но эффективнее


def chat_completion(messages: List[Dict[str, str]], model: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    """Make a chat completion request to OpenAI.
    
    Args:
        messages: List of message dicts with 'role' and 'content'
        model: Model to use (defaults to CHAT_MODEL)
        **kwargs: Additional parameters for the API
        
    Returns:
        Response dict from OpenAI
    """
    try:
        # gpt-5-mini поддерживает только temperature=1
        used_model = model or CHAT_MODEL
        if 'gpt-5' in used_model:
            # Убираем temperature для gpt-5 моделей
            kwargs.pop('temperature', None)
        elif 'temperature' not in kwargs:
            kwargs['temperature'] = 0.3  # Для других моделей используем 0.3
            
        response = client.chat.completions.create(
            model=used_model,
            messages=messages,
            **kwargs
        )
        return {
            "choices": [
                {
                    "message": {
                        "content": response.choices[0].message.content
                    }
                }
            ]
        }
    except Exception as e:
        logger.error(f"OpenAI chat completion error: {e}")
        raise


def get_embeddings(texts: List[str], model: Optional[str] = None) -> List[List[float]]:
    """Get embeddings for a list of texts.
    
    Args:
        texts: List of texts to embed
        model: Model to use (defaults to EMBEDDING_MODEL)
        
    Returns:
        List of embedding vectors
    """
    try:
        # OpenAI имеет лимит на размер текста, обрезаем если нужно
        truncated_texts = [text[:8191] for text in texts]  # Max 8191 tokens
        
        response = client.embeddings.create(
            model=model or EMBEDDING_MODEL,
            input=truncated_texts
        )
        return [item.embedding for item in response.data]
    except Exception as e:
        logger.error(f"OpenAI embeddings error: {e}")
        raise


def transcribe_audio(audio_file_path: str) -> str:
    """Transcribe audio using OpenAI Whisper.
    
    Args:
        audio_file_path: Path to audio file
        
    Returns:
        Transcribed text
    """
    try:
        with open(audio_file_path, 'rb') as audio_file:
            response = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
        return response.text
    except Exception as e:
        logger.error(f"OpenAI transcription error: {e}")
        raise
