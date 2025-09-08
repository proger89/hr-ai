from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from ..services.oauth import get_salutespeech_access_token
import requests
import asyncio
from typing import AsyncIterator
import contextlib
import os
import time
from openai import AsyncOpenAI
import base64
import io

router = APIRouter(prefix="/api/tts", tags=["tts"])

# Простая реализация barge-in: карта request_id -> Event
_CANCEL_EVENTS: dict[str, asyncio.Event] = {}

# OpenAI client
openai_client = None


@router.post("/synthesize")
def synthesize_ssml(ssml: str, voice: str = "Nec_24000", sample_rate: int = 24000):
    try:
        token, _ = get_salutespeech_access_token()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    url = f"https://smartspeech.sber.ru/rest/v1/text:synthesize?format=opus&voice={voice}&sample_rate={sample_rate}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/ssml",
    }
    # Проверка цепочки доверия через собранный бандл (если есть)
    verify_path = "/app/ca/ru_bundle.pem" if os.path.exists("/app/ca/ru_bundle.pem") else True
    resp = requests.post(url, data=ssml.encode("utf-8"), headers=headers, timeout=60, verify=verify_path)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    def gen():
        yield resp.content

    return StreamingResponse(gen(), media_type="audio/ogg")



@router.post("/synthesize/stream")
def synthesize_ssml_stream(ssml: str, voice: str = "Nec_24000", sample_rate: int = 24000, request_id: str = "default"):
    """Потоковая генерация TTS (REST v1, chunked). Поддерживает остановку по /api/tts/stop/{request_id}."""
    try:
        token, _ = get_salutespeech_access_token()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    url = f"https://smartspeech.sber.ru/rest/v1/text:synthesize?format=opus&voice={voice}&sample_rate={sample_rate}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/ssml",
    }
    verify_path = "/app/ca/ru_bundle.pem" if os.path.exists("/app/ca/ru_bundle.pem") else True

    cancel_event = _CANCEL_EVENTS.setdefault(request_id, asyncio.Event())
    # Сбросим флаг перед запуском
    if cancel_event.is_set():
        cancel_event.clear()

    try:
        resp = requests.post(url, data=ssml.encode("utf-8"), headers=headers, timeout=300, verify=verify_path, stream=True)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TTS upstream error: {e}")

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    def chunk_gen():
        # Агрегируем входящие данные и отдаём кусками примерно каждые 300-500 мс
        # с минимальным размером буфера, чтобы избежать слишком мелких аппендов в MSE
        min_bytes = 16 * 1024  # ~16KB
        max_delay_s = 0.4      # целимся в ~400мс
        buf = bytearray()
        last_flush = time.monotonic()

        with contextlib.closing(resp):
            for chunk in resp.iter_content(chunk_size=4096):
                if cancel_event.is_set():
                    break
                if not chunk:
                    # пустой кусок игнорируем
                    if buf:
                        yield bytes(buf)
                    break

                buf.extend(chunk)
                now = time.monotonic()
                if len(buf) >= min_bytes or (now - last_flush) >= max_delay_s:
                    yield bytes(buf)
                    buf.clear()
                    last_flush = now

            # флеш остатка, если есть
            if not cancel_event.is_set() and buf:
                yield bytes(buf)

    return StreamingResponse(chunk_gen(), media_type="audio/ogg")


@router.post("/stop/{request_id}")
def stop_tts(request_id: str):
    ev = _CANCEL_EVENTS.get(request_id)
    if ev is None:
        _CANCEL_EVENTS[request_id] = asyncio.Event()
        ev = _CANCEL_EVENTS[request_id]
    ev.set()
    return {"stopped": True, "request_id": request_id}


@router.post("/openai/synthesize")
async def synthesize_openai(text: str, voice: str = "alloy"):
    """Генерация речи через OpenAI API"""
    global openai_client
    
    try:
        if not openai_client:
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise HTTPException(status_code=500, detail="OpenAI API key not configured")
            openai_client = AsyncOpenAI(api_key=api_key)
        
        # Генерируем речь
        response = await openai_client.audio.speech.create(
            model="tts-1",
            voice=voice,  # alloy, echo, fable, onyx, nova, shimmer
            input=text,
            response_format="mp3"
        )
        
        # Получаем байты аудио
        audio_data = response.read()
        
        # Возвращаем как аудио поток
        return StreamingResponse(
            io.BytesIO(audio_data),
            media_type="audio/mpeg",
            headers={
                "Content-Disposition": "inline; filename=speech.mp3"
            }
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OpenAI TTS error: {str(e)}")

