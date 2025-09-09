# app/routers/va.py
import os, httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import SessionLocal
from ..models import Vacancy

router = APIRouter(prefix="/api/va", tags=["voice-agents"])

class TokenRequest(BaseModel):
    vacancy_id: int
    interview_id: str
    lang: str = "ru"   # "ru" | "en"

async def fetch_scenario_from_db(vacancy_id: int) -> dict:
    """
    Верните dict сценария из БД:
    {
      "intro": "...",
      "experience": "...",
      "stack": "...",
      "cases": "...",
      "communication": "...",
      "final": "..."
    }
    """
    async with SessionLocal() as db:
        vacancy = await db.get(Vacancy, vacancy_id)
        if not vacancy or not vacancy.jd_json:
            return {}
        
        # Извлекаем сценарий из jd_json
        scenario = vacancy.jd_json.get("scenario", {})
        if isinstance(scenario, dict):
            return scenario
        
        # Если сценарий в виде списка, преобразуем в dict
        if isinstance(scenario, list):
            result = {}
            for item in scenario:
                if isinstance(item, dict) and "competence" in item and "question" in item:
                    comp = item["competence"]
                    result[comp] = item["question"]
            return result
        
        return {}

def build_tools_schema():
    return [
        {
            "type": "function",
            "name": "question_asked",
          "description": "Mark progress for a primary question exactly once per question.",
            "parameters": {
                "type": "object",
            "properties": { "is_primary": { "type": "boolean" } },
                "required": ["is_primary"]
            }
        },
        {
            "type": "function",
            "name": "extract_facts",
          "description": "Extract short bullet facts from user's last answer for scoring.",
            "parameters": {
                "type": "object",
                "properties": {
                    "section": { "type": "string", "enum": ["experience","stack","cases","communication"] },
              "facts":   { "type": "array", "items": { "type": "string" } }
                },
                "required": ["section","facts"]
            }
        },
        {
            "type": "function",
            "name": "end_interview",
          "description": "Request to finish; server will verify thresholds and finalize.",
            "parameters": { "type": "object", "properties": {} }
        }
    ]

def make_instructions(lang: str) -> str:
    if lang.lower().startswith("ru"):
        return (
          "Говори ТОЛЬКО по‑русски. Ты HR‑интервьюер и ведёшь СТРОГО сценарное интервью (state.scenario).\n"
          "Жёсткие правила:\n"
          "• ОДИН вопрос за раз. В КОНЦЕ каждого вопроса ставь символ '⟂' (U+27C2). После '⟂' НЕ добавляй слова.\n"
          "• Следующий вопрос можно задавать ТОЛЬКО после полноценного ответа (содержательно и не односложно).\n"
          "• Реплика ассистента ≤ 10 секунд и ≤ 20 слов. Никакого коучинга/советов/психологии/карьерных рекомендаций.\n"
          "• Вопросы формулируй ИСКЛЮЧИТЕЛЬНО из state.scenario в порядке: intro → experience → stack → cases → communication → final (если есть). Не импровизируй.\n"
          "• Если пользователь уводит разговор в сторону — вежливо верни к сценарию и повтори текущий вопрос.\n"
          "• На первичных вопросах CALL tool question_asked({is_primary:true}); на follow‑up — false.\n"
          "• После каждой реплики пользователя CALL extract_facts(section,facts[]). Факты вслух не зачитывай.\n"
          "• Не завершай интервью до покрытия всех первичных вопросов и подтверждения кандидата."
        )
    else:
        return (
          "Speak ONLY English. You are an HR interviewer. STRICTLY follow state.scenario.\n"
          "Rules:\n"
          "• EXACTLY one question at a time. END every question with '⟂'. Do NOT add words after '⟂'.\n"
          "• Ask the next question ONLY after a substantive answer.\n"
          "• Utterances ≤ 10 sec / ≤ 20 words. No coaching/therapy/career advice.\n"
          "• Form questions EXCLUSIVELY from state.scenario in order: intro → experience → stack → cases → communication → final (if present). NO improvisation.\n"
          "• If the user derails/asks for advice — politely redirect to the scenario and repeat the current question.\n"
          "• Use question_asked({is_primary:true}) for primary, false for follow‑ups; extract_facts after each user reply; finish only after all primary questions are covered and user confirms."
        )

@router.post("/token")
async def mint_client_secret(req: TokenRequest):
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
    if not OPENAI_API_KEY:
        raise HTTPException(500, "OPENAI_API_KEY not set")
    
    try:
        scenario = await fetch_scenario_from_db(req.vacancy_id)
    except Exception as e:
        raise HTTPException(500, f"Scenario not available: {e}")

    primary_keys = [k for k in scenario.keys() if k != "final"]
    total_primary = max(1, len(primary_keys))

    session_cfg = {
        "type": "realtime",
        "model": os.environ.get("REALTIME_MODEL", "gpt-realtime"),
        "modalities": ["audio","text"],
        "voice": os.environ.get("REALTIME_VOICE", "marin"),
        # Делаем VAD ленивее, чтобы вдох/шорох не считался фразой
        "turn_detection": { "type": "server_vad", "silence_duration_ms": 750 },
        "input_audio_format":  { "type": "pcm16", "sample_rate": 24000 },
        "output_audio_format": { "type": "pcm16", "sample_rate": 24000 },
        "temperature": 0.0,
        "instructions": make_instructions(req.lang),
        "tools": build_tools_schema(),
        "tool_choice": "auto",
        "metadata": {
            "vacancy_id": req.vacancy_id,
            "interview_id": req.interview_id,
            "lang": req.lang,
            "total_primary": total_primary
        },
        "state": { "scenario": scenario }
    }
    
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            headers = {
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
                "OpenAI-Beta": "realtime=v1",
            }

            # Попытка №1: client_secrets с прошивкой сессии
            r = await client.post(
                "https://api.openai.com/v1/realtime/client_secrets",
                headers=headers,
                json={"session": session_cfg},
            )

            if r.is_success:
                return r.json()

            # Попытка №2: fallback на старый эндпоинт sessions
            r2 = await client.post(
                "https://api.openai.com/v1/realtime/sessions",
                headers=headers,
                json={
                    "model": session_cfg.get("model"),
                    "voice": session_cfg.get("voice"),
                },
            )
            r2.raise_for_status()
            res2 = r2.json()
            return {
                "client_secret": {
                    "value": (res2.get("client_secret") or {}).get("value"),
                    "expires_at": (res2.get("client_secret") or {}).get("expires_at"),
                },
                "session": session_cfg,
            }
    except httpx.HTTPError as e:
        raise HTTPException(502, f"OpenAI client_secrets failed: {e}")
