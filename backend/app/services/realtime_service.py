"""
OpenAI Realtime API Service
"""
import os
import json
import asyncio
import logging
import re
from typing import Dict, Any, Optional, List
from datetime import datetime

logger = logging.getLogger(__name__)

# Конфигурация
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-realtime")
REALTIME_WS_URL = "wss://api.openai.com/v1/realtime"


class RealtimeSession:
    """Управление сессией Realtime API"""
    
    def __init__(self, session_id: str, candidate_id: str, vacancy_id: Optional[int] = None):
        self.session_id = session_id
        self.candidate_id = candidate_id
        self.vacancy_id = vacancy_id
        self.created_at = datetime.now()
        self.conversation_items = []
        self.scores = []
        self.total_questions = 5
        self.current_question = 0
        self.ws_connection = None
        self.context = {}
        
    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "candidate_id": self.candidate_id,
            "vacancy_id": self.vacancy_id,
            "created_at": self.created_at.isoformat(),
            "total_questions": self.total_questions,
            "current_question": self.current_question,
            "scores": self.scores,
            "conversation_items": self.conversation_items
        }


# Хранилище сессий (в продакшене использовать Redis)
realtime_sessions: Dict[str, RealtimeSession] = {}


def _compact_list(items: Optional[List[str]], n: int = 10) -> List[str]:
    if not items:
        return []
    return [str(s).strip() for s in items if str(s).strip()][:n]


def _compact_text(text: Optional[str], max_chars: int = 800) -> str:
    if not text:
        return ""
    t = re.sub(r"\s+", " ", str(text))
    return t[:max_chars]


def build_private_context(resume_tags: Optional[Dict[str, Any]], vacancy_json: Optional[Dict[str, Any]], lang: str) -> str:
    """Собирает компактный приватный контекст без сырых текстов JD/CV."""
    resume_tags = resume_tags or {}
    vacancy_json = vacancy_json or {}

    skills = _compact_list(resume_tags.get("skills") or resume_tags.get("tech_stack"))
    exp_years = resume_tags.get("experience_years") or resume_tags.get("exp_years")
    jd_keywords = _compact_list((vacancy_json or {}).get("keywords"))
    role = (vacancy_json or {}).get("title") or (vacancy_json or {}).get("role")
    raw_scenario = (vacancy_json or {}).get("scenario") or []

    # Компетенции сценария
    competences: List[str] = []
    if isinstance(raw_scenario, list):
        for q in raw_scenario:
            if isinstance(q, dict) and q.get("competence"):
                competences.append(str(q["competence"]))
    elif isinstance(raw_scenario, dict):
        for k in ["intro", "experience", "stack", "cases", "communication", "final"]:
            if raw_scenario.get(k):
                competences.append(k)

    lines: List[str] = []
    if role:
        lines.append(f"Role: {role}")
    if exp_years:
        lines.append(f"Experience: ~{exp_years}y")
    if jd_keywords:
        lines.append("JD must-have: " + ", ".join(_compact_list(jd_keywords, 12)))
    if skills:
        lines.append("Candidate skills: " + ", ".join(_compact_list(skills, 12)))
    if competences:
        lines.append("Scenario competences: " + ", ".join(_compact_list(competences, 8)))

    return "\n".join(lines)


def create_session_config(
    resume_text: str,
    jd_text: str,
    scenario: List[Dict[str, Any]],
    lang: str = "ru"
) -> Dict[str, Any]:
    """Создание конфигурации для Realtime сессии"""
    
    logger.debug(f"create_session_config called with scenario type: {type(scenario)}, value: {scenario}")

    # Формируем инструкции для ИИ без сырых текстов (используем приватный контекст)
    lang_name = "русском" if lang == "ru" else "английском"
    instructions = f"""
# Роль
Ты профессиональный интервьюер. Веди собеседование строго на {lang_name}. Никаких других языков.

# Приватный контекст (НЕ ОЗВУЧИВАТЬ, НЕ ЦИТИРОВАТЬ)
<private>
{{PRIVATE_CONTEXT}}
</private>

# Правила
- Жесткий запрет: не читать и не пересказывать приватный контекст; используй его только для подбора вопросов.
- Сразу после краткого приветствия задай первый вопрос. Не пересказывай JD/резюме.
- Один вопрос за раз. Жди ответа кандидата.
- КАЖДЫЙ РАЗ, когда задаёшь НОВЫЙ вопрос, НЕМЕДЛЕННО вызови tool `question_asked` с `index = номер_вопроса` (нумерация с 1).
- После каждого ответа вызови tool `evaluate_answer` (score 0–100 + короткое обоснование).
- Всего вопросов: {{TOTAL_Q}}. После последнего вызови `end_interview`.

"""
    
    if scenario and isinstance(scenario, list):
        instructions += "\n# Сценарий вопросов\n"
        for i, q in enumerate(scenario[:12], 1):
            if isinstance(q, dict) and q.get("question"):
                instructions += f"{i}. [{q.get('competence', 'Общий')}] {q.get('question', '')}\n"
    else:
        instructions += (
            "\n# Сценарий вопросов\n"
            "1. Расскажите о себе и своем опыте\n"
            "2. Почему вас заинтересовала эта вакансия?\n"
            "3. Опишите свой самый сложный проект\n"
            "4. Какие у вас есть вопросы о компании?\n"
            "5. Когда вы готовы приступить к работе?\n"
        )
    
    instructions += (
        "\n# Старт\n"
        + ("Поздоровайся кратко и сразу задай первый вопрос." if lang == "ru" else "Greet briefly and immediately ask the first question.")
        + "\n"
    )
    
    # Добавляем язык в конфигурацию
    output_voice = "alloy" if lang == "en" else "verse"
    return {
        "voice": output_voice,
        "instructions": instructions,
        "modalities": ["text", "audio"],
        # Улучшаем распознавание речи и фиксируем язык распознавания
        "input_audio_transcription": {
            "model": "gpt-4o-mini-transcribe",
            "language": ("ru" if lang == "ru" else "en")
        },
        "audio": {
            "input": {
                "format": "pcm16",
                "sample_rate": 24000,
                "turn_detection": {
                    "type": "semantic_vad",
                    "create_response": True,
                    "threshold": 0.6,
                    "silence_duration_ms": 1000
                }
            },
            "output": {
                "format": "pcm16",
                "sample_rate": 24000,
                "voice": "alloy",
                "speed": 1.0
            }
        },
        "tools": [
            {
                "type": "function",
                "name": "evaluate_answer",
                "description": "Оценить ответ кандидата по шкале от 0 до 100",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "score": {
                            "type": "integer",
                            "description": "Оценка от 0 до 100",
                            "minimum": 0,
                            "maximum": 100
                        },
                        "reasoning": {
                            "type": "string",
                            "description": "Обоснование оценки"
                        }
                    },
                    "required": ["score", "reasoning"]
                }
            },
            {
                "type": "function",
                "name": "question_asked",
                "description": "Зафиксировать, что задан очередной вопрос (для прогресса)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "Номер вопроса, начиная с 1"}
                    },
                    "required": ["index"]
                }
            },
            {
                "type": "function", 
                "name": "end_interview",
                "description": "Завершить интервью",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "overall_score": {
                            "type": "integer",
                            "description": "Общая оценка кандидата от 0 до 100",
                            "minimum": 0,
                            "maximum": 100
                        },
                        "strengths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Сильные стороны кандидата"
                        },
                        "weaknesses": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Слабые стороны кандидата"
                        },
                        "recommendation": {
                            "type": "string",
                            "enum": ["hire", "maybe", "reject"],
                            "description": "Рекомендация по найму"
                        }
                    },
                    "required": ["overall_score", "recommendation"]
                }
            }
        ]
    }


def get_ws_headers() -> Dict[str, str]:
    """Получить заголовки для WebSocket соединения"""
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY is not set!")
        raise ValueError("OPENAI_API_KEY environment variable is not set")
    
    # Логируем первые символы ключа для отладки
    logger.debug(f"Using API key starting with: {OPENAI_API_KEY[:10]}...")
    
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1"
    }


async def handle_function_call(
    session: RealtimeSession,
    function_name: str,
    arguments: Dict[str, Any]
) -> Dict[str, Any]:
    """Обработка вызова функции от модели"""
    
    if function_name == "evaluate_answer":
        # Сохраняем оценку
        score = arguments.get("score", 50)
        reasoning = arguments.get("reasoning", "")
        session.scores.append({
            "question": session.current_question,
            "score": score,
            "reasoning": reasoning
        })
        
        return {
            "status": "success",
            "message": "Оценка сохранена"
        }
        
    elif function_name == "end_interview":
        # Завершаем интервью
        overall_score = arguments.get("overall_score", 50)
        strengths = arguments.get("strengths", [])
        weaknesses = arguments.get("weaknesses", [])
        recommendation = arguments.get("recommendation", "maybe")
        
        session.context["interview_completed"] = True
        session.context["overall_score"] = overall_score
        session.context["strengths"] = strengths
        session.context["weaknesses"] = weaknesses
        session.context["recommendation"] = recommendation
        session.context["passed"] = recommendation in ["hire", "maybe"]
        
        return {
            "status": "success",
            "message": "Интервью завершено",
            "overall_score": overall_score,
            "passed": session.context["passed"]
        }
    
    return {
        "status": "error",
        "message": f"Неизвестная функция: {function_name}"
    }
