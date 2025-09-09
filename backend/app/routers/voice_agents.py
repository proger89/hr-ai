from __future__ import annotations

import os
import json
from typing import Any, Dict

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..db import SessionLocal
from ..models import Vacancy


router = APIRouter(prefix="/api/voice", tags=["voice"])

OPENAI_KEY = os.environ.get("OPENAI_API_KEY")
REALTIME_MODEL = os.environ.get("OPENAI_REALTIME_MODEL", os.environ.get("REALTIME_MODEL", "gpt-realtime"))
DEFAULT_VOICE = os.environ.get("OPENAI_VOICE", os.environ.get("REALTIME_VOICE", "verse"))


class MintResponse(BaseModel):
    client_secret: Dict[str, Any]
    session: Dict[str, Any]


@router.get("/mint", response_model=MintResponse)
async def mint_client_secret(
    vacancy_id: int = Query(...),
    interview_id: int = Query(...),
    lang: str = Query("ru"),
):
    if not OPENAI_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY missing")

    async with SessionLocal() as db:
        vac = await db.get(Vacancy, vacancy_id)

    if not vac or not getattr(vac, "jd_json", None):
        raise HTTPException(status_code=404, detail="Vacancy or scenario not found")

    jd = vac.jd_json if isinstance(vac.jd_json, dict) else json.loads(vac.jd_json)
    sc = jd.get("scenario", {}) if isinstance(jd, dict) else {}

    instructions = f"""
Ты — HR-интервьюер. Говори ТОЛЬКО на языке: {'Русский' if lang=='ru' else 'English'}.
Строго следуй сценарию по шагам: intro → experience → stack → cases → communication → final.
ЗАДАВАЙ ровно ОДИН вопрос за раз. После произнесения вопроса — молчи и ЖДИ ответа пользователя.
Если ответа нет ~10 секунд, мягко переспрашивай тем же вопросом.
По каждому ПРОИЗНЕСЕННОМУ вопросу вызывай tool `question_asked` с полями section и text.
После того как пользователь ответил достаточно, переходи к следующему шагу.
Никаких коучинговых монологов и психологических советов. Коротко, по делу.
Старайся не перебивать себя и пользователя.

Сценарий:
intro: {sc.get('intro', '')}
experience: {sc.get('experience', '')}
stack: {sc.get('stack', '')}
cases: {sc.get('cases', '')}
communication: {sc.get('communication', '')}
final: {sc.get('final', '')}
"""

    session_cfg: Dict[str, Any] = {
        "model": REALTIME_MODEL,
        "voice": DEFAULT_VOICE,
        "instructions": instructions.strip(),
        "modalities": ["audio", "text"],
        "turn_detection": {
            "type": "semantic_vad",
            "eagerness": "low",
            "create_response": True,
            "interrupt_response": True,
        },
        "input_audio_transcription": {
            "model": "gpt-4o-mini-transcribe",
        },
        "tools": [
            {
                "type": "function",
                "name": "question_asked",
                "description": "Notify UI to increment progress when a question is spoken.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "section": {
                            "type": "string",
                            "enum": [
                                "intro",
                                "experience",
                                "stack",
                                "cases",
                                "communication",
                                "final",
                            ],
                        },
                        "text": {"type": "string"},
                    },
                    "required": ["section", "text"],
                },
            },
            {
                "type": "function",
                "name": "finish_interview",
                "description": "Finish interview and show result to candidate.",
                "parameters": {"type": "object", "properties": {}},
            },
        ],
        "metadata": {
            "vacancy_id": vacancy_id,
            "interview_id": interview_id,
            "lang": lang,
        },
    }

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://api.openai.com/v1/realtime/client_secrets",
            headers={
                "Authorization": f"Bearer {OPENAI_KEY}",
                "Content-Type": "application/json",
                "OpenAI-Beta": "realtime=v1",
            },
            json={"session": session_cfg},
        )
        if r.status_code >= 300:
            raise HTTPException(status_code=500, detail=f"Mint failed: {r.text}")
        data = r.json()

    return {"client_secret": data.get("client_secret", {}), "session": session_cfg}


