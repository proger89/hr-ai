# app/routers/interview.py
import logging
from datetime import datetime
from typing import Dict, Any, List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import SessionLocal
from ..models import Candidate, Vacancy
from ..services.scoring_service import scorer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/interviews", tags=["interviews"])

# ---- NEW: метрики одного пользовательского «turn» ----
class TurnMetrics(BaseModel):
    turn_index: int
    speech_ms: int
    segments: list[dict] = []   # [{ "s": float_ms, "e": float_ms }]
    words: int = 0
    rms_mean: float | None = None
    rms_var: float | None = None

# Временное хранилище для интервью (в проде использовать Redis)
interview_sessions: Dict[str, Dict[str, Any]] = {}

class MarkRequest(BaseModel):
    is_primary: bool

class FactsRequest(BaseModel):
    section: str
    facts: List[str]

@router.post("/{interview_id}/mark")
async def mark_question(interview_id: str, body: MarkRequest):
    """Отмечаем заданный вопрос и обновляем прогресс"""
    session = interview_sessions.get(interview_id, {})
    
    if body.is_primary:
        session["current_primary"] = session.get("current_primary", 0) + 1
        session["answered_primary"] = session.get("answered_primary", 0) + 1
    
    current = session.get("current_primary", 0)
    total = session.get("total_primary", 5)
    
    # Обновляем сессию
    interview_sessions[interview_id] = session
    
    return {
        "current": current,
        "total": total,
        "progress_percent": round(100 * current / max(1, total))
    }

@router.post("/{interview_id}/facts") 
async def push_facts(interview_id: str, body: FactsRequest):
    """Сохраняем извлечённые факты из ответов кандидата"""
    session = interview_sessions.get(interview_id, {})
    
    if "facts" not in session:
        session["facts"] = {}
    
    if body.section not in session["facts"]:
        session["facts"][body.section] = []
    
    session["facts"][body.section].extend(body.facts)
    
    # Обновляем сессию
    interview_sessions[interview_id] = session
    
    return {"ok": True, "total_facts": sum(len(f) for f in session["facts"].values())}

@router.post("/{interview_id}/finalize")
async def finalize_interview(interview_id: str):
    """Финализация интервью: скоринг, запись в БД, редирект"""
    session = interview_sessions.get(interview_id)
    if not session:
        raise HTTPException(404, "Interview session not found")
    
    # Проверяем пороги
    answered_primary = session.get("answered_primary", 0)
    min_primary_required = session.get("min_primary_required", 3)
    
    if answered_primary < min_primary_required:
        return {
            "status": "too_early",
            "message": f"Need {min_primary_required - answered_primary} more primary questions"
        }
    
    db = SessionLocal()
    try:
        # Получаем кандидата по interview_id
        # Предполагаем что interview_id это на самом деле candidate_id для упрощения
        candidate_id = int(interview_id) if interview_id.isdigit() else None
        if not candidate_id:
            raise HTTPException(400, "Invalid interview ID")
        
        candidate = await db.get(Candidate, candidate_id)
        if not candidate:
            raise HTTPException(404, "Candidate not found")
        
        # Собираем все факты для скоринга
        all_facts = session.get("facts", {})
        vacancy_id = session.get("vacancy_id")
        
        # Получаем данные вакансии для скоринга
        vacancy = await db.get(Vacancy, vacancy_id) if vacancy_id else None
        vacancy_requirements = {}
        
        if vacancy and vacancy.jd_json:
            vacancy_requirements = {
                "title": vacancy.jd_json.get("title", ""),
                "experience_years": vacancy.jd_json.get("experience_years", 0),
                "required_skills": vacancy.jd_json.get("required_skills", []),
                "nice_to_have": vacancy.jd_json.get("nice_to_have", [])
            }
        
        # Генерируем скоринг через GPT
        try:
            scoring_result = await scorer.score_interview(
                candidate_facts=all_facts,
                vacancy_requirements=vacancy_requirements,
                lang=session.get("lang", "ru"),
                speech_metrics=session.get("turns", [])
            )
            
            overall_score = scoring_result.get("overall_score", 50)
            recommendation = scoring_result.get("recommendation", "maybe")
            
            # Генерируем детальный отчёт
            detailed_report = await scorer.generate_detailed_report(
                scoring_result=scoring_result,
                candidate_name=candidate.name or "Кандидат",
                vacancy_title=vacancy_requirements.get("title", "Позиция"),
                lang=session.get("lang", "ru")
            )
            
        except Exception as e:
            logger.error(f"Failed to generate scoring: {e}")
            # Fallback скоринг
            overall_score = 60
            recommendation = "maybe"
            scoring_result = {
                "scores": {"experience": 60, "stack": 60, "cases": 60, "communication": 60},
                "strengths": ["Мотивированный"],
                "weaknesses": ["Недостаточно опыта"]
            }
            detailed_report = "Автоматическая оценка недоступна"
        
        # Определяем решение
        if recommendation == "hire" or (recommendation == "maybe" and overall_score >= 70):
            decision = "hired"
        elif recommendation == "maybe":
            decision = "maybe"
        else:
            decision = "rejected"
        
        # Сохраняем результаты в tags кандидата
        if not candidate.tags:
            candidate.tags = {}
        
        candidate.tags.update({
            "interview_completed": True,
            "interview_score": overall_score,
            "interview_decision": decision,
            "interview_recommendation": recommendation,
            "interview_date": datetime.now().isoformat(),
            "interview_facts": all_facts,
            "interview_scoring": scoring_result,
            "interview_report": detailed_report,
            "interview_duration_min": session.get("duration_min", 0)
        })
        
        await db.commit()
        
        # Формируем URL для редиректа
        redirect_url = f"/complete.html?score={overall_score}&decision={decision}&id={candidate_id}"
        
        # Очищаем сессию
        interview_sessions.pop(interview_id, None)
        
        return {
            "status": "completed",
            "decision": decision,
            "score": overall_score,
            "redirect_url": redirect_url
        }
        
    except Exception as e:
        logger.error(f"Error finalizing interview: {e}")
        raise HTTPException(500, f"Failed to finalize interview: {str(e)}")
    finally:
        await db.close()

@router.post("/{interview_id}/init")
async def init_interview(interview_id: str, vacancy_id: int = None, lang: str = "ru"):
    """Инициализация сессии интервью"""
    interview_sessions[interview_id] = {
        "interview_id": interview_id,
        "vacancy_id": vacancy_id,
        "lang": lang,
        "current_primary": 0,
        "total_primary": 5,
        "answered_primary": 0,
        "min_primary_required": 3,
        "facts": {},
        "turns": [],
        "created_at": datetime.now().isoformat()
    }
    
    return {"ok": True, "interview_id": interview_id}

@router.post("/{interview_id}/turn")
async def ingest_turn_metrics(interview_id: str, body: TurnMetrics):
    """Принимаем метрики одного пользовательского «turn» (между вопросами ассистента)."""
    session = interview_sessions.get(interview_id)
    if not session:
        raise HTTPException(404, "Interview session not found")
    turns = session.setdefault("turns", [])
    turns.append(body.model_dump())
    interview_sessions[interview_id] = session
    return {"ok": True, "turns": len(turns)}
