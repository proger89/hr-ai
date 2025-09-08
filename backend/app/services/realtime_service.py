"""
OpenAI Realtime API Service
"""
import os
import json
import asyncio
import logging
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


def create_session_config(
    resume_text: str,
    jd_text: str,
    scenario: List[Dict[str, Any]],
    lang: str = "ru"
) -> Dict[str, Any]:
    """Создание конфигурации для Realtime сессии"""
    
    logger.debug(f"create_session_config called with scenario type: {type(scenario)}, value: {scenario}")
    
    # Формируем инструкции для ИИ
    lang_name = "русский" if lang == "ru" else "английский"
    instructions = f"""# Роль и цель
Ты профессиональный HR‑интервьюер. Успех — корректное, дружелюбное и структурированное интервью.

## Язык
- ГОВОРИ СТРОГО ТОЛЬКО НА {lang_name.upper()} ЯЗЫКЕ.
- НЕ ПЕРЕХОДИ на другие языки ни при каких условиях.
- Если пользователь говорит не на {lang_name}, по‑{lang_name} вежливо скажи, что говоришь только на {lang_name}.

## Контекст вакансии
{jd_text[:2000]}

## Резюме кандидата
{resume_text[:2000]}

## Правила
- НЕ зачитывай и НЕ пересказывай дословно контекст (резюме/вакансию).
- Используй контекст только для подготовки вопросов.
- Вопросы короткие и по делу.
- Один вопрос за раз; жди ответ кандидата.
- Не перебивай, соблюдай паузы; поддерживай перебивание пользователя.
- В конце поблагодари кандидата.
- Не оценивай кандидата вслух.

## Сценарий интервью
"""
    
    if scenario and isinstance(scenario, list):
        for i, q in enumerate(scenario[:5], 1):
            if isinstance(q, dict):
                instructions += f"\n{i}. [{q.get('competence', 'Общий')}] {q.get('question', '')}"
    else:
        instructions += """
1. Расскажите о себе и своем опыте
2. Почему вас заинтересовала эта вакансия?
3. Опишите свой самый сложный проект
4. Какие у вас есть вопросы о компании?
5. Когда вы готовы приступить к работе?
"""
    
    instructions += f"""

## Приветствие
Начни с короткого приветствия на {lang_name} и сразу задай первый вопрос. Не предлагай свободный диалог, проводи именно интервью по сценарию.

ПЕРВЫЙ ВОПРОС:
{"Здравствуйте! Я проведу с вами интервью. Расскажите, пожалуйста, о себе." if lang == "ru" else "Hello! I'll be conducting this interview. Please tell me about yourself."}
"""
    
    # Добавляем язык в конфигурацию
    response_lang = "ru-RU" if lang == "ru" else "en-US"
    
    output_voice = "alloy" if lang == "en" else "verse"
    return {
        "type": "realtime",
        "model": REALTIME_MODEL,
        "voice": output_voice,
        "instructions": instructions,
        "output_modalities": ["audio"],
        # Улучшаем распознавание речи и фиксируем язык распознавания
        "input_audio_transcription": {
            "model": "gpt-4o-transcribe",
            "language": ("ru" if lang == "ru" else "en")
        },
        "audio": {
            "input": {
                "format": "pcm16",
                "sample_rate": 24000,
                "turn_detection": {
                    "type": "semantic_vad",
                    "create_response": True,
                    "threshold": 0.5,
                    "silence_duration_ms": 700
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
