from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from ..db import get_session
from ..models import Candidate, Vacancy


router = APIRouter(prefix="/api/dialog", tags=["dialog"])

# Хранилище сессий (в продакшене использовать Redis)
dialog_sessions: Dict[str, Dict[str, Any]] = {}


def _lang_short(code: Optional[str]) -> str:
    if not code:
        return "ru"
    code = code.lower()
    return "ru" if code.startswith("ru") else "en"


def _choose_topic(answer_l: str) -> str:
    if any(k in answer_l for k in ["fastapi", "django", "async", "postgres", "kafka", "microservice", "микросервис"]):
        return "backend"
    if any(k in answer_l for k in ["react", "frontend", "ui", "ux", "верстк", "css", "html"]):
        return "frontend"
    if any(k in answer_l for k in ["ml", "nlp", "model", "модель", "обуч", "датасет"]):
        return "ml"
    if any(k in answer_l for k in ["team", "команд", "конфликт", "feedback", "обратн", "soft"]):
        return "soft"
    return "general"


def _next_question(topic: str, lang: str, answer: str | None) -> Dict[str, str]:
    # Пытаемся получить вопрос от GigaChat, иначе фолбэк на правила
    try:
        from ..services.openai_service import chat_completion  # type: ignore
        sys_prompt = (
            "You are an interview assistant. Propose the next question to candidate based on the previous answer. "
            "Be concise and on-topic. Return plain text only."
        ) if lang == "en" else (
            "Ты ассистент-интервьюер. Предложи следующий вопрос кандидату по предыдущему ответу. "
            "Коротко и по делу. Верни только текст вопроса."
        )
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": (answer or "")},
        ]
        data = chat_completion(messages)
        text = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        if text:
            comp = {
                "backend": "Тех: бэкенд" if lang != "en" else "Tech: backend",
                "frontend": "Тех: фронтенд" if lang != "en" else "Tech: frontend",
                "ml": "Тех: ML",
                "soft": "Soft skills",
            }.get(topic, "Общее" if lang != "en" else "General")
            return {"competency": comp, "question": text}
    except Exception:
        pass
    # Фолбэк
    if lang == "en":
        mapping = {
            "backend": ("Tech: backend", "Describe a challenging backend task you solved recently. Why was it hard?"),
            "frontend": ("Tech: frontend", "Tell about a complex UI you built. What trade-offs did you make?"),
            "ml": ("Tech: ML", "How did you validate your model and avoid data leakage?"),
            "soft": ("Soft skills", "Tell about a conflict in a team and how you resolved it."),
            "general": ("General", "What accomplishment are you most proud of in the last year?")
        }
    else:
        mapping = {
            "backend": ("Тех: бэкенд", "Опишите сложную задачу на бэкенде, которую вы недавно решали. В чём была сложность?"),
            "frontend": ("Тех: фронтенд", "Расскажите о сложном UI, который вы реализовали. На какие компромиссы пошли?"),
            "ml": ("Тех: ML", "Как вы валидировали модель и избегали утечек данных?"),
            "soft": ("Soft skills", "Расскажите о конфликте в команде и как вы его урегулировали."),
            "general": ("Общее", "Каким достижением за последний год вы особенно гордитесь?")
        }
    comp, q = mapping.get(topic, mapping["general"])
    return {"competency": comp, "question": q}


class NextRequest(BaseModel):
    last_answer: str
    lang: Optional[str] = None  # ru|en|ru-RU|en-US
    stage: Optional[str] = None
    vacancy_id: Optional[int] = None


class NextResponse(BaseModel):
    competency: str
    question: str
    lang: str


@router.post("/next", response_model=NextResponse)
def dialog_next(req: NextRequest) -> Dict[str, Any]:
    lang = _lang_short(req.lang)
    topic = _choose_topic((req.last_answer or "").lower())
    nxt = _next_question(topic, lang, req.last_answer)
    return {"lang": lang, **nxt}


class FollowupRequest(BaseModel):
    question: str
    answer: str
    lang: Optional[str] = None


class FollowupResponse(BaseModel):
    followup: str


@router.post("/followup", response_model=FollowupResponse)
def dialog_followup(req: FollowupRequest) -> Dict[str, str]:
    lang = _lang_short(req.lang)
    a = (req.answer or "").lower()
    if lang == "en":
        if any(k in a for k in ["metric", "latency", "throughput", "cpu", "memory", "qps"]):
            f = "Which metrics did you monitor and what thresholds were acceptable?"
        elif any(k in a for k in ["tradeoff", "compromise", "deadline", "scope"]):
            f = "What trade-offs did you consider and why did you choose this option?"
        else:
            f = "Can you provide a concrete example to illustrate your point?"
    else:
        if any(k in a for k in ["метрик", "латентн", "пропускн", "cpu", "память", "rps", "qps"]):
            f = "Какие метрики вы отслеживали и какие пороги считали допустимыми?"
        elif any(k in a for k in ["компромисс", "срок", "tradeoff", "объём работ"]):
            f = "Какие компромиссы вы рассматривали и почему выбрали этот вариант?"
        else:
            f = "Можете привести конкретный пример, чтобы проиллюстрировать мысль?"
    return {"followup": f}


# Новые эндпоинты для интервью


class StartInterviewRequest(BaseModel):
    candidate_id: str
    vacancy_id: Optional[int] = None
    lang: Optional[str] = "ru-RU"


class StartInterviewResponse(BaseModel):
    session_id: str
    total_questions: int


class NextQuestionRequest(BaseModel):
    session_id: str


class NextQuestionResponse(BaseModel):
    question: str
    question_number: int
    finished: bool = False


class AnswerRequest(BaseModel):
    session_id: str
    answer: str


class AnswerResponse(BaseModel):
    ok: bool


class FinishInterviewRequest(BaseModel):
    session_id: str


class FinishInterviewResponse(BaseModel):
    overall_score: int
    passed: bool
    summary: Dict[str, Any]


@router.post("/start", response_model=StartInterviewResponse)
async def start_interview(
    req: StartInterviewRequest, 
    session: AsyncSession = Depends(get_session)
) -> Dict[str, Any]:
    """Начало интервью"""
    try:
        # Получаем данные кандидата
        candidate = await session.get(Candidate, req.candidate_id)
        if not candidate:
            raise HTTPException(status_code=404, detail="Кандидат не найден")
        
        # Получаем вакансию
        vacancy_id = req.vacancy_id or candidate.tags.get("vacancy_id")
        vacancy = None
        if vacancy_id:
            vacancy = await session.get(Vacancy, vacancy_id)
        
        # Создаем сессию
        session_id = str(uuid.uuid4())
        
        # Подготавливаем контекст для интервью
        resume_text = candidate.tags.get("cv_text", "")
        jd_text = ""
        scenario = []
        
        if vacancy:
            jd_text = vacancy.jd_json.get("text", "") if vacancy.jd_json else ""
            scenario = vacancy.jd_json.get("scenario", []) if vacancy.jd_json else []
        
        # Инициализируем сессию
        dialog_sessions[session_id] = {
            "candidate_id": req.candidate_id,
            "vacancy_id": vacancy_id,
            "lang": _lang_short(req.lang),
            "started_at": datetime.now().isoformat(),
            "questions": [],
            "answers": [],
            "current_question": 0,
            "total_questions": min(len(scenario), 5) if scenario else 5,
            "resume_text": resume_text,
            "jd_text": jd_text,
            "scenario": scenario,
            "scores": []
        }
        
        return {
            "session_id": session_id,
            "total_questions": dialog_sessions[session_id]["total_questions"]
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/next", response_model=NextQuestionResponse)
async def get_next_question(
    req: NextQuestionRequest,
    session: AsyncSession = Depends(get_session)
) -> Dict[str, Any]:
    """Получение следующего вопроса"""
    if req.session_id not in dialog_sessions:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    
    sess = dialog_sessions[req.session_id]
    current = sess["current_question"]
    total = sess["total_questions"]
    
    if current >= total:
        return {
            "question": "",
            "question_number": current,
            "finished": True
        }
    
    # Генерируем вопрос
    lang = sess["lang"]
    scenario = sess["scenario"]
    
    if scenario and current < len(scenario):
        # Используем вопрос из сценария
        q_data = scenario[current]
        question = q_data.get("question", "")
        if q_data.get("competence"):
            question = f"[{q_data['competence']}] {question}"
    else:
        # Генерируем общий вопрос
        previous_answer = sess["answers"][-1] if sess["answers"] else None
        topic = _choose_topic(previous_answer or "")
        q_data = _next_question(topic, lang, previous_answer)
        question = q_data.get("question", "")
    
    sess["questions"].append(question)
    sess["current_question"] += 1
    
    return {
        "question": question,
        "question_number": sess["current_question"],
        "finished": False
    }


@router.post("/answer", response_model=AnswerResponse)
async def submit_answer(
    req: AnswerRequest,
    session: AsyncSession = Depends(get_session)
) -> Dict[str, Any]:
    """Сохранение ответа кандидата"""
    if req.session_id not in dialog_sessions:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    
    sess = dialog_sessions[req.session_id]
    sess["answers"].append(req.answer)
    
    # Оцениваем ответ (простая эвристика)
    score = min(100, len(req.answer.split()) * 5)  # Примитивная оценка по длине
    sess["scores"].append(score)
    
    return {"ok": True}


@router.post("/finish", response_model=FinishInterviewResponse)
async def finish_interview(
    req: FinishInterviewRequest,
    session: AsyncSession = Depends(get_session)
) -> Dict[str, Any]:
    """Завершение интервью и сохранение результатов"""
    if req.session_id not in dialog_sessions:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    
    sess = dialog_sessions[req.session_id]
    
    # Вычисляем общую оценку
    scores = sess["scores"]
    overall_score = sum(scores) // len(scores) if scores else 0
    passed = overall_score >= 70
    
    # Сохраняем результаты в БД
    candidate = await session.get(Candidate, sess["candidate_id"])
    if candidate:
        if not candidate.tags:
            candidate.tags = {}
        
        candidate.tags["interview_completed"] = True
        candidate.tags["interview_score"] = overall_score
        candidate.tags["interview_passed"] = passed
        candidate.tags["interview_date"] = datetime.now().isoformat()
        candidate.tags["interview_summary"] = {
            "questions": sess["questions"],
            "answers": sess["answers"],
            "scores": scores,
            "duration": sess.get("duration", "00:00")
        }
        
        await session.commit()
    
    # Удаляем сессию
    summary = {
        "questions_asked": len(sess["questions"]),
        "avg_answer_length": sum(len(a) for a in sess["answers"]) // len(sess["answers"]) if sess["answers"] else 0
    }
    
    del dialog_sessions[req.session_id]
    
    return {
        "overall_score": overall_score,
        "passed": passed,
        "summary": summary
    }


