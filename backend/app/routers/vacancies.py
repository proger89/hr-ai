from __future__ import annotations

from typing import Any, Dict, Optional
from pydantic import BaseModel
from fastapi import HTTPException
from fastapi.responses import FileResponse

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..celery_app import celery_app
from .upload import _plain_text
from ..models import Vacancy
from ..services.openai_service import chat_completion


router = APIRouter(prefix="/api/vacancies", tags=["vacancies"])


@router.get("")
async def list_vacancies(session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    res = await session.execute(select(Vacancy).order_by(Vacancy.created_at.desc()))
    items = []
    for v in res.scalars().all():
        jd = v.jd_json or {}
        scen = jd.get("scenario") or {}
        has = False
        if isinstance(scen, dict):
            for _k, _v in scen.items():
                if isinstance(_v, str) and _v.strip():
                    has = True
                    break
        items.append({
            "id": v.id,
            "title": v.title,
            "lang": v.lang,
            "keywords": jd.get("keywords", []),
            "task_id": jd.get("task_id"),
            "has_scenario": has,
            "created_at": v.created_at,
        })
    return {"items": items}


class WeightsPayload(BaseModel):
    tech: float
    comm: float
    cases: float


@router.get("/{vacancy_id}/weights")
async def get_weights(vacancy_id: int, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    v = await session.get(Vacancy, vacancy_id)
    if not v:
        raise HTTPException(status_code=404, detail="vacancy not found")
    return {"tech": v.weights_tech, "comm": v.weights_comm, "cases": v.weights_cases}


@router.post("/{vacancy_id}/weights")
async def set_weights(vacancy_id: int, payload: WeightsPayload, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    v = await session.get(Vacancy, vacancy_id)
    if not v:
        raise HTTPException(status_code=404, detail="vacancy not found")
    s = max(1e-6, payload.tech + payload.comm + payload.cases)
    v.weights_tech = float(payload.tech / s)
    v.weights_comm = float(payload.comm / s)
    v.weights_cases = float(payload.cases / s)
    await session.commit()
    return {"ok": True, "weights": {"tech": v.weights_tech, "comm": v.weights_comm, "cases": v.weights_cases}}


class VacancyUpdate(BaseModel):
    title: Optional[str] = None
    lang: Optional[str] = None


@router.get("/{vacancy_id}")
async def get_vacancy(vacancy_id: int, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    v = await session.get(Vacancy, vacancy_id)
    if not v:
        raise HTTPException(status_code=404, detail="vacancy not found")
    jd = v.jd_json or {}
    scen = jd.get("scenario") or {}
    has = False
    if isinstance(scen, dict):
        for _k, _v in scen.items():
            if isinstance(_v, str) and _v.strip():
                has = True
                break
    return {
        "id": v.id,
        "title": v.title,
        "lang": v.lang,
        "keywords": jd.get("keywords", []),
        "scenario": scen,
        "has_scenario": has,
        "scenario_versions": jd.get("scenario_versions", []),
        "jd_path": v.jd_raw,
        "weights": {"tech": v.weights_tech, "comm": v.weights_comm, "cases": v.weights_cases},
        "created_at": v.created_at,
    }


@router.patch("/{vacancy_id}")
async def update_vacancy(vacancy_id: int, payload: VacancyUpdate, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    v = await session.get(Vacancy, vacancy_id)
    if not v:
        raise HTTPException(status_code=404, detail="vacancy not found")
    changed = False
    if payload.title is not None and payload.title.strip() and payload.title != v.title:
        v.title = payload.title.strip()
        changed = True
    if payload.lang is not None and payload.lang.strip() and payload.lang != v.lang:
        v.lang = payload.lang.strip()
        changed = True
    if changed:
        await session.commit()
    return {"ok": True, "id": v.id, "title": v.title, "lang": v.lang}


class ScenarioPayload(BaseModel):
    intro: Optional[str] = None
    experience: Optional[str] = None
    stack: Optional[str] = None
    cases: Optional[str] = None
    communication: Optional[str] = None
    final: Optional[str] = None
    save_version: bool = False
    regen: bool = False


@router.get("/{vacancy_id}/scenario")
async def get_scenario(vacancy_id: int, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    v = await session.get(Vacancy, vacancy_id)
    if not v:
        raise HTTPException(status_code=404, detail="vacancy not found")
    jd = v.jd_json or {}
    return {
        "scenario": jd.get("scenario", {}),
        "versions": jd.get("scenario_versions", []),
    }


@router.post("/{vacancy_id}/scenario")
async def set_scenario(vacancy_id: int, payload: ScenarioPayload, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    from datetime import datetime

    v = await session.get(Vacancy, vacancy_id)
    if not v:
        raise HTTPException(status_code=404, detail="vacancy not found")
    jd = v.jd_json or {}
    scenario = jd.get("scenario", {})
    # Async regeneration (non-blocking)
    if payload.regen:
        jd_text = _plain_text(v.jd_raw or "")
        try:
            ar = celery_app.send_task("vacancy.generate_and_save", args=[vacancy_id, jd_text, v.lang])
            return {"ok": True, "task_id": ar.id}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"regen enqueue failed: {e}")

    # Update provided fields only
    for k in ["intro", "experience", "stack", "cases", "communication", "final"]:
        val = getattr(payload, k)
        if val is not None:
            scenario[k] = val
    jd["scenario"] = scenario
    if payload.save_version:
        versions = list(jd.get("scenario_versions", []))
        versions.insert(0, {"date": datetime.utcnow().isoformat() + "Z", "data": scenario})
        jd["scenario_versions"] = versions[:50]
    v.jd_json = jd
    await session.commit()
    return {"ok": True, "scenario": scenario}


@router.delete("/{vacancy_id}")
async def delete_vacancy(vacancy_id: int, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    v = await session.get(Vacancy, vacancy_id)
    if not v:
        raise HTTPException(status_code=404, detail="vacancy not found")
    await session.delete(v)
    await session.commit()
    return {"ok": True}


@router.get("/{vacancy_id}/jd/preview")
async def jd_preview(vacancy_id: int, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    v = await session.get(Vacancy, vacancy_id)
    if not v:
        raise HTTPException(status_code=404, detail="vacancy not found")
    text = _plain_text(v.jd_raw or "")
    name = (v.jd_raw or "").split("/")[-1]
    return {"name": name, "text": text[:20000]}


@router.get("/{vacancy_id}/jd/download")
async def jd_download(vacancy_id: int, session: AsyncSession = Depends(get_session)) -> FileResponse:
    v = await session.get(Vacancy, vacancy_id)
    if not v:
        raise HTTPException(status_code=404, detail="vacancy not found")
    path = v.jd_raw or ""
    if not path or not isinstance(path, str) or not path.startswith("/"):
        raise HTTPException(status_code=404, detail="file not available")
    filename = path.split("/")[-1]
    return FileResponse(path, filename=filename, media_type="application/octet-stream")


@router.get("/{vacancy_id}/keywords")
async def get_dynamic_keywords(vacancy_id: int, session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    """Динамически извлекает ключевые слова из вакансии через OpenAI."""
    v = await session.get(Vacancy, vacancy_id)
    if not v:
        raise HTTPException(status_code=404, detail="vacancy not found")
    
    # Получаем текст вакансии
    jd_text = _plain_text(v.jd_raw or "") or v.title or ""
    
    # Добавим сценарий если есть
    try:
        scenario = (v.jd_json or {}).get("scenario", {})
        if scenario:
            parts = []
            for key in ["intro", "experience", "stack", "cases", "communication", "final"]:
                if scenario.get(key):
                    parts.append(f"{key}: {scenario[key]}")
            if parts:
                jd_text += "\n\nСценарий интервью:\n" + "\n".join(parts)
    except:
        pass
    
    if not jd_text:
        return {"keywords": []}
    
    # Запрашиваем ключевые слова у OpenAI
    prompt = f"""Извлеки из текста вакансии наиболее важные ключевые слова и технологии.
Верни ТОЛЬКО список через запятую, без нумерации и дополнительного текста.
Включи:
- Языки программирования
- Фреймворки и библиотеки
- Инструменты и технологии
- Ключевые навыки
- Важные термины из предметной области

Текст вакансии:
{jd_text[:4000]}

Ключевые слова (через запятую):"""
    
    try:
        messages = [
            {"role": "system", "content": "Ты эксперт по анализу вакансий. Извлекай только самые релевантные ключевые слова."},
            {"role": "user", "content": prompt}
        ]
        
        response = chat_completion(messages)
        keywords_text = response.get("choices", [{}])[0].get("message", {}).get("content", "")
        
        # Парсим ключевые слова
        keywords = [kw.strip() for kw in keywords_text.split(",") if kw.strip()]
        
        # Ограничим количество для производительности
        keywords = keywords[:30]
        
        return {"keywords": keywords}
        
    except Exception as e:
        # Fallback на существующие keywords
        return {"keywords": v.jd_json.get("keywords", []) if v.jd_json else []}

