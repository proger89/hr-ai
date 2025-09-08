from __future__ import annotations

import os
import io
import tempfile
import re
import time
from typing import Dict, Any, Optional
import requests
from pydantic import BaseModel
from datetime import datetime, timezone

from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..db import get_session
from ..models import Vacancy, Candidate, InviteToken
from ..services.storage import storage
from ..services.llm import generate_scenario_with_llm
from ..celery_app import celery_app
from ..config import settings
from ..security import sign_jwt_like
import secrets

try:
    from pypdf import PdfReader  # type: ignore
except Exception:  # noqa: BLE001
    PdfReader = None  # type: ignore
try:
    import docx2txt  # type: ignore
except Exception:  # noqa: BLE001
    docx2txt = None  # type: ignore


router = APIRouter(prefix="/api/upload", tags=["upload"])


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _plain_text(path: str) -> str:
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext == '.pdf' and PdfReader is not None:
            out = []
            reader = PdfReader(path)
            for page in reader.pages:
                try:
                    out.append(page.extract_text() or '')
                except Exception:
                    pass
            return "\n".join(out)
        if ext in ('.docx', '.doc') and docx2txt is not None:
            try:
                return docx2txt.process(path) or ''
            except Exception:
                return ''
        # Fallback: read as utf-8 text
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except Exception:
        return ''


def _infer_keywords(text: str) -> list[str]:
    """Динамическое извлечение ключевых токенов из произвольного текста (RU/EN).
    Без статических словарей: частотный отбор с фильтрацией стоп-слов.
    Возвращает топ-20 токенов по убыванию частоты.
    """
    try:
        tl = (text or "").lower()
        # Базовые стоп-слова RU/EN (минимальный набор)
        stop = set(
            """
            и в во не на с со из за по от для при как что это той тойто то этой эти этот эта также или да но а же уже либо либоже к у о об обо над под между без более менее чем где когда который которая которые который что бы чтобы было были быть есть нет да нету тут там тогда потом также самый самая самые всего всего-то всегото всего‑то всего—то очень ещё еще либо‑либо либо-то the a an of in on at by with to from for as is are was were be been being this that these those and or nor but so into onto about across over under above below near far out up down off than within without per not only also just vs versus etc etc.
            """
            .split()
        )
        # токены: рус/латин/цифры/+,#,/ и . внутри
        tokens = re.findall(r"[a-zа-я0-9][a-zа-я0-9+#/.\-]{1,}", tl, flags=re.IGNORECASE)
        freq: dict[str, int] = {}
        for t in tokens:
            if len(t) < 3:
                continue
            if t.isdigit():
                continue
            if t in stop:
                continue
            freq[t] = freq.get(t, 0) + 1
        # топ-20
        return [k for k, _ in sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:20]]
    except Exception:
        return []


def _extract_sections(text: str) -> dict:
    """Very lightweight extraction of typical JD sections in RU/EN.
    Returns { responsibilities: str, requirements: str, nice_to_have: str }
    """
    try:
        # Normalize
        t = (text or "").replace("\r", "")
        lines = [ln.strip() for ln in t.split("\n")]
        blocks: dict[str, list[str]] = {"resp": [], "req": [], "nice": []}
        current = None
        for ln in lines:
            low = ln.lower()
            if any(k in low for k in ["обязанности", "responsibilities", "what you will do", "you will"]):
                current = "resp"; continue
            if any(k in low for k in ["требования", "requirements", "what we expect", "must have"]):
                current = "req"; continue
            if any(k in low for k in ["будет плюсом", "nice to have", "additional", "желательно"]):
                current = "nice"; continue
            if current:
                blocks[current].append(ln)
        return {
            "responsibilities": "\n".join(blocks["resp"]).strip(),
            "requirements": "\n".join(blocks["req"]).strip(),
            "nice_to_have": "\n".join(blocks["nice"]).strip(),
        }
    except Exception:
        return {"responsibilities": "", "requirements": "", "nice_to_have": ""}


def _generate_scenario(base_text: str, lang: str | None, keywords: list[str]) -> dict:
    """Generate an interview scenario without external LLM.
    Uses heuristics and keywords to assemble structured prompts.
    """
    l = (lang or "ru").lower()
    secs = _extract_sections(base_text)
    kw = ", ".join(sorted(set(keywords))) or ("python" if l == "ru" else "python")
    if l.startswith("ru"):
        intro = "Расскажите кратко о себе и опыте в последних проектах."
        experience = f"Опишите 2–3 ключевых проекта, вашу роль, стек ({kw}) и результаты."
        stack = "Поясните выбор технологий, архитектуру, масштаб и компромиссы."
        cases = "Разберите сложный инцидент/кейc: постановка, гипотезы, диагностика, решение, метрики."
        communication = "Как вы взаимодействуете с командой, заказчиками и смежными командами?"
        final = "Какие ожидания от роли и что важно для вас? Есть вопросы к нам?"
        # Tailor by sections if present
        if secs.get("requirements"):
            stack += " Какие пункты из требований особенно сильны для вас?"
        if secs.get("responsibilities"):
            experience += " Какие из обязанностей вам наиболее близки?"
    else:
        intro = "Give a brief overview of your background and recent projects."
        experience = f"Describe 2–3 key projects, your role, tech stack ({kw}), and outcomes."
        stack = "Explain technology choices, architecture, scale and trade-offs."
        cases = "Walk through a complex incident/case: problem, hypotheses, diagnostics, solution, metrics."
        communication = "How do you collaborate with teammates, stakeholders and adjacent teams?"
        final = "What are your expectations from the role? Any questions for us?"
        if secs.get("requirements"):
            stack += " Which requirement items are your strongest?"
        if secs.get("responsibilities"):
            experience += " Which responsibilities fit you best?"
    return {
        "intro": intro,
        "experience": experience,
        "stack": stack,
        "cases": cases,
        "communication": communication,
        "final": final,
    }


@celery_app.task(bind=True, name="vacancy.generate_and_save")
def task_generate_and_save(self, vacancy_id: int, jd_text: str, lang: str | None) -> dict:
    """Background: generate scenario with LLM (fallback heuristics) and persist to DB with progress."""
    import asyncio
    from .upload import _infer_keywords, _generate_scenario  # self-import safe for Celery context
    from ..db import SessionLocal
    from ..models import Vacancy

    try:
        self.update_state(state="PROGRESS", meta={"progress": 5, "stage": "start"})
    except Exception:
        pass

    scenario = generate_scenario_with_llm(jd_text, lang)
    try:
        self.update_state(state="PROGRESS", meta={"progress": 70 if scenario else 30, "stage": "llm_done"})
    except Exception:
        pass
    if not scenario:
        scenario = _generate_scenario(jd_text, lang, _infer_keywords(jd_text))
    try:
        self.update_state(state="PROGRESS", meta={"progress": 85, "stage": "saving"})
    except Exception:
        pass

    async def _save() -> dict:
        # Use a per-call async engine/session to avoid event-loop reuse issues in Celery
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
        from ..config import settings as _settings
        _engine = create_async_engine(_settings.database_url or "postgresql+asyncpg://sber:sber@db:5432/sber", echo=False, future=True)
        _Session = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
        async with _Session() as s:
            v = await s.get(Vacancy, int(vacancy_id))
            if not v:
                return {"ok": False, "error": "vacancy not found"}
            jd = (v.jd_json or {}).copy()
            jd.setdefault("keywords", _infer_keywords(jd_text))
            jd["scenario"] = scenario
            v.jd_json = jd
            try:
                await s.commit()
            except Exception:
                # Fallback to explicit UPDATE in case ORM change tracking misses JSON mutation
                from sqlalchemy import update
                try:
                    await s.execute(update(Vacancy).where(Vacancy.id == int(vacancy_id)).values(jd_json=jd))
                    await s.commit()
                except Exception:
                    return {"ok": False, "error": "commit_failed"}
        # Verify using a fresh session and SELECT
        async with _Session() as s2:
            v2 = await s2.get(Vacancy, int(vacancy_id))
            if not v2:
                return {"ok": False, "error": "vacancy not found (verify)"}
            saved = False
            try:
                data = ((v2.jd_json or {}).get("scenario")) or {}
                if isinstance(data, dict):
                    for _k, _v in data.items():
                        if isinstance(_v, str) and _v.strip():
                            saved = True
                            break
            except Exception:
                saved = False
            try:
                await _engine.dispose()
            except Exception:
                pass
            return {"ok": bool(saved), "saved": bool(saved)}

    res = asyncio.run(_save())
    # If save returned ok=False, reflect FAILURE in result to prevent frontend from marking done
    if not res.get("ok"):
        try:
            self.update_state(state="FAILURE", meta={"progress": 90, "stage": "save_failed"})
        except Exception:
            pass
    try:
        self.update_state(state="PROGRESS", meta={"progress": 100, "stage": "done"})
    except Exception:
        pass
    return res


@celery_app.task(bind=True, name="cv.match_candidate")
def task_match_candidate(self, candidate_id: int) -> dict:
    """Подбор соответствия кандидата вакансиям и вычисление match_pct.
    Алгоритм:
      1) Берём текст из резюме (cv_path) через _plain_text
      2) По ключевым словам сравниваем с keywords каждой вакансии
      3) Обновляем Candidate.tags: summary.match_pct, status='ready'
    Реализация лёгкая эвристическая (LLM можно подключить позже).
    """
    import asyncio
    import logging
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select
    from .upload import _infer_keywords, _plain_text  # type: ignore
    from ..config import settings as _settings
    from ..models import Candidate as _Cand, Vacancy as _Vac
    from ..services.openai_service import chat_completion, get_embeddings
    
    logger = logging.getLogger(__name__)

    async def _run() -> dict:
        _engine = create_async_engine(_settings.database_url, echo=False, future=True)
        _Session = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
        async with _Session() as s:
            c = await s.get(_Cand, int(candidate_id))
            if not c:
                return {"ok": False, "error": "candidate not found"}
            tags = dict(c.tags or {})
            cv_path = tags.get("cv_path") or ""
            text = _plain_text(str(cv_path)) if cv_path else ""
            logger.info(f"[CV {candidate_id}] Text extracted: {len(text)} chars, path: {cv_path}")
            
            res = await s.execute(select(_Vac))
            vacs = list(res.scalars().all())
            logger.info(f"[CV {candidate_id}] Found {len(vacs)} vacancies to match")
            
            # Упрощенный подход - доверяем OpenAI для оценки соответствия
            best = 0.0
            best_vac = None
            
            for idx, v in enumerate(vacs):
                try:
                    # Небольшая задержка между запросами
                    if idx > 0:
                        import time
                        time.sleep(0.5)
                    
                    # Получаем полный текст вакансии
                    jd_text = _plain_text(v.jd_raw or "") or ""
                    if not jd_text:
                        jd_text = v.title or ""
                    
                    # Добавим сценарий интервью если есть
                    jd_scenario = ""
                    try:
                        scenario = (v.jd_json or {}).get("scenario", {})
                        if scenario:
                            parts = []
                            for key in ["intro", "experience", "stack", "cases", "communication", "final"]:
                                if scenario.get(key):
                                    parts.append(f"{key}: {scenario[key]}")
                            if parts:
                                jd_scenario = "\n\nСценарий интервью:\n" + "\n".join(parts)
                    except:
                        pass
                    
                    # Логируем первые 500 символов резюме для отладки
                    logger.info(f"[CV {candidate_id}] Resume preview: {text[:500]}...")
                    
                    # Простой промпт без строгих указаний
                    prompt = f"""Проанализируй вакансию и резюме кандидата. Оцени насколько резюме соответствует требованиям вакансии.

ВАКАНСИЯ:
{jd_text}{jd_scenario}

РЕЗЮМЕ КАНДИДАТА:
{text}

Укажи соответствие резюме к вакансии от 1 до 100% (только число):"""
                    
                    try:
                        logger.info(f"[CV {candidate_id}] Calling OpenAI for vac {v.id} ({v.title})")
                        messages = [
                            {"role": "system", "content": "Ты эксперт по подбору персонала."},
                            {"role": "user", "content": prompt}
                        ]
                        
                        # Retry логика
                        def _retry(fn, *a, _tries=3, _delay=1.0, **kw):
                            import time as _t
                            for i in range(_tries):
                                try:
                                    return fn(*a, **kw)
                                except Exception as e:
                                    if i == _tries-1:
                                        raise
                                    logger.warning(f"Retry {i+1}/{_tries} after error: {e}")
                                    _t.sleep(_delay*(2**i))
                        
                        data = _retry(chat_completion, messages)
                        raw = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                        logger.info(f"[CV {candidate_id}] OpenAI raw response: '{raw}'")
                        
                        # Парсим число из ответа
                        import re
                        numbers = re.findall(r'\d+(?:\.\d+)?', raw)
                        if numbers:
                            score_pct = float(numbers[0])
                            score = score_pct / 100.0  # Конвертируем в 0-1
                            score = max(0.0, min(1.0, score))
                        else:
                            score = 0.0
                            
                        logger.info(f"[CV {candidate_id}] OpenAI score for vac {v.id}: {score * 100}%")
                        
                    except Exception as e:
                        logger.error(f"[CV {candidate_id}] OpenAI failed for vac {v.id}: {e}")
                        # Простой fallback - базовое сравнение ключевых слов
                        cv_kw = set(_infer_keywords(text))
                        vac_kw = set(_infer_keywords(jd_text))
                        if cv_kw and vac_kw:
                            score = len(cv_kw & vac_kw) / max(len(cv_kw), len(vac_kw))
                        else:
                            score = 0.0
                        logger.info(f"[CV {candidate_id}] Fallback score: {score * 100}%")
                    
                    if score > best:
                        best = score
                        best_vac = v
                        
                except Exception as e:
                    logger.error(f"[CV {candidate_id}] Error processing vacancy {v.id}: {e}")
            # Обновить теги
            summ = dict(tags.get("summary", {}) or {})
            summ["match_pct"] = round(float(best), 4)
            tags["summary"] = summ
            tags["status"] = "ready"
            if best_vac is not None:
                if not tags.get("vacancy_id"):
                    tags["vacancy_id"] = best_vac.id
                    tags["vacancy_title"] = best_vac.title
                elif tags.get("vacancy_id") == best_vac.id:
                    # Обновляем название вакансии, если id совпадает
                    tags["vacancy_title"] = best_vac.title
            c.tags = tags
            await s.commit()
            logger.info(f"[CV {candidate_id}] Final match: {round(best*100, 2)}% for vac {best_vac.id if best_vac else 'None'}")
        try:
            await _engine.dispose()
        except Exception:
            pass
        return {"ok": True, "match": best}

    try:
        return asyncio.get_event_loop().run_until_complete(_run())
    except RuntimeError:
        import asyncio as _a
        loop = _a.new_event_loop()
        _a.set_event_loop(loop)
        return loop.run_until_complete(_run())


_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}")
_RE_PHONE = re.compile(r"(?:\+?7|8)[\s\-()]?\d{3}[\s\-()]?\d{3}[\s\-()]?\d{2}[\s\-()]?\d{2}")

_RU_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}

def _parse_contacts(text: str) -> Dict[str, Optional[str]]:
    email = None
    phone = None
    m = _RE_EMAIL.search(text)
    if m:
        email = m.group(0)
    p = _RE_PHONE.search(text)
    if p:
        phone = p.group(0)
    return {"email": email, "phone": phone}


def _norm_month(token: str) -> Optional[int]:
    t = token.strip().lower()
    for k, v in _RU_MONTHS.items():
        if t.startswith(k):
            return v
    try:
        n = int(t)
        if 1 <= n <= 12:
            return n
    except Exception:
        pass
    return None


def _is_present(tok: str) -> bool:
    t = tok.strip().lower()
    return t in {"нв", "н.в.", "наст", "настоящее", "по настоящее время", "present"}


def _parse_date_span(s: str) -> Optional[tuple[datetime, Optional[datetime]]]:
    # Supported examples:
    # 03.2019 — 07.2021
    # 2018 — 2020
    # Июнь 2019 — Август 2021
    # 04/2020 - н.в.
    s_l = s.lower().replace("—", "-").replace("–", "-")
    # dd.mm.yyyy or mm.yyyy or mm/yyyy
    # Try patterns in order: month name, mm.yyyy, yyyy
    parts = [p.strip() for p in s_l.split("-")]
    if len(parts) != 2:
        return None
    a, b = parts

    def parse_side(t: str) -> Optional[datetime]:
        t = t.strip()
        if not t:
            return None
        if _is_present(t):
            return None
        # Month name + year
        tokens = re.split(r"[\s./]", t)
        tokens = [x for x in tokens if x]
        if len(tokens) == 2:
            m = _norm_month(tokens[0])
            if m is not None:
                try:
                    y = int(tokens[1])
                    return datetime(y, m, 1)
                except Exception:
                    pass
        # mm.yyyy or mm/yyyy
        if re.match(r"^\d{1,2}[./]\d{4}$", t):
            sep = "/" if "/" in t else "."
            mm, yy = t.split(sep)
            try:
                return datetime(int(yy), int(mm), 1)
            except Exception:
                return None
        # yyyy
        if re.match(r"^\d{4}$", t):
            try:
                return datetime(int(t), 1, 1)
            except Exception:
                return None
        return None

    start = parse_side(a)
    end = None if _is_present(b) else parse_side(b)
    if not start and not end:
        return None
    return (start or datetime(1970, 1, 1), end)


def _extract_experience(text: str) -> list[Dict[str, Any]]:
    # Heuristic: scan lines, detect date spans and capture surrounding context as role/company
    lines = [ln.strip() for ln in text.splitlines()]
    spans: list[Dict[str, Any]] = []
    date_pat = re.compile(r"(?i)(?:\d{1,2}[./]\d{4}|[А-Яа-яA-Za-z]+\s+\d{4}|\d{4})\s*[-—–]\s*(?:\d{1,2}[./]\d{4}|[А-Яа-яA-Za-z]+\s+\d{4}|\d{4}|н\.в\.|наст\.|по настоящее время|present)")
    for i, ln in enumerate(lines):
        if not ln:
            continue
        if date_pat.search(ln):
            span = _parse_date_span(ln)
            if not span:
                continue
            start_dt, end_dt = span
            # Context lines for role/company
            role = None
            company = None
            # previous non-empty line often contains role/company
            for j in range(i - 1, max(-1, i - 4), -1):
                if 0 <= j < len(lines) and lines[j]:
                    if not role:
                        role = lines[j]
                    elif not company and lines[j] != role:
                        company = lines[j]
                        break
            # Next line fallback
            if not role and i + 1 < len(lines) and lines[i + 1]:
                role = lines[i + 1]
            spans.append({
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat() if end_dt else None,
                "role": role,
                "company": company,
                "raw": ln,
            })
    return spans


def _months_between(start: datetime, end: datetime) -> int:
    y = end.year - start.year
    m = end.month - start.month
    return y * 12 + m + (1 if end.day >= start.day else 0)


def _analyze_experience(items: list[Dict[str, Any]]) -> Dict[str, Any]:
    now = datetime.now()
    total_months = 0
    short_tenures = 0
    flags: list[str] = []
    for it in items:
        try:
            s = datetime.fromisoformat(it["start"]) if it.get("start") else None
            e = datetime.fromisoformat(it["end"]) if it.get("end") else None
            e = e or now
            if s and e and e < s:
                flags.append("date_inconsistency")
                continue
            if s and e:
                months = _months_between(s, e)
                it["months"] = months
                total_months += max(0, months)
                if months < 6:
                    short_tenures += 1
        except Exception:
            continue
    if short_tenures >= 3:
        flags.append("frequent_job_changes")
    return {"total_months": total_months, "flags": list(sorted(set(flags)))}


def _extract_text_from_bytes(data: bytes, ext: str) -> str:
    try:
        ext_l = (ext or '').lower()
        if ext_l == '.pdf' and PdfReader is not None:
            try:
                out = []
                reader = PdfReader(io.BytesIO(data))
                for page in reader.pages:
                    try:
                        out.append(page.extract_text() or '')
                    except Exception:
                        pass
                return "\n".join(out)
            except Exception:
                pass
        if ext_l in ('.docx', '.doc') and docx2txt is not None:
            try:
                with tempfile.NamedTemporaryFile(delete=True, suffix=ext_l) as tmp:
                    tmp.write(data)
                    tmp.flush()
                    return docx2txt.process(tmp.name) or ''
            except Exception:
                pass
        # fallback: try decode as text
        try:
            return data.decode('utf-8', errors='ignore')
        except Exception:
            return ''
    except Exception:
        return ''


def _strip_html(html: str) -> str:
    try:
        html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
        html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
    except Exception:
        return html


class CvLinkRequest(BaseModel):
    url: str
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None


@router.post("/jd")
async def upload_jd(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    lang: Optional[str] = Form("ru"),
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    try:
        rid = str(int(time.time()))
        ext = os.path.splitext(file.filename or "jd")[1]
        data = await file.read()
        storage_path, _ = storage.save_bytes(data, "jd", f"{rid}{ext}")
        # Извлечение текста и ключевых слов
        text = _extract_text_from_bytes(data, ext)
        base_text = (file.filename or "") + "\n" + text
        kws = _infer_keywords(base_text)
        vac = Vacancy(
            title=title or (file.filename or "Vacancy"),
            jd_raw=storage_path,
            jd_json={"keywords": kws, "scenario": {}, "scenario_versions": []},
            lang=lang or "ru",
        )
        session.add(vac)
        await session.commit()
        await session.refresh(vac)
        # Seed heuristic scenario immediately to avoid empty UI while LLM runs
        try:
            base_scn = _generate_scenario(base_text, lang, kws)
            v = await session.get(Vacancy, vac.id)
            if v:
                jd0 = v.jd_json or {}
                if not (jd0.get("scenario") or {}):
                    jd0["scenario"] = base_scn
                    v.jd_json = jd0
                    await session.commit()
        except Exception:
            pass
        # fire-and-forget background generation
        task_id = None
        try:
            ar = celery_app.send_task("vacancy.generate_and_save", args=[vac.id, base_text, lang])
            task_id = ar.id
        except Exception:
            pass
        # persist task_id to jd_json for server-driven status
        try:
            jd = vac.jd_json or {}
            if task_id:
                jd["task_id"] = task_id
                vac.jd_json = jd
                await session.commit()
        except Exception:
            pass
        return {"vacancy_id": vac.id, "path": storage_path, "keywords": kws, "task": "enqueued", "task_id": task_id}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/jd_text")
async def upload_jd_text(
    content: str = Form(...),
    title: Optional[str] = Form(None),
    lang: Optional[str] = Form("ru"),
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    try:
        rid = str(int(time.time()))
        data = content.encode("utf-8", errors="ignore")
        storage_path, _ = storage.save_bytes(data, "jd", f"{rid}.txt")
        kws = _infer_keywords(content)
        vac = Vacancy(
            title=title or "Vacancy",
            jd_raw=storage_path,
            jd_json={"keywords": kws, "scenario": {}, "scenario_versions": []},
            lang=lang or "ru",
        )
        session.add(vac)
        await session.commit()
        await session.refresh(vac)
        # Seed heuristic scenario immediately
        try:
            base_scn = _generate_scenario(content, lang, kws)
            v = await session.get(Vacancy, vac.id)
            if v:
                jd0 = v.jd_json or {}
                if not (jd0.get("scenario") or {}):
                    jd0["scenario"] = base_scn
                    v.jd_json = jd0
                    await session.commit()
        except Exception:
            pass
        task_id = None
        try:
            ar = celery_app.send_task("vacancy.generate_and_save", args=[vac.id, content, lang])
            task_id = ar.id
        except Exception:
            pass
        try:
            jd = vac.jd_json or {}
            if task_id:
                jd["task_id"] = task_id
                vac.jd_json = jd
                await session.commit()
        except Exception:
            pass
        return {"vacancy_id": vac.id, "path": storage_path, "keywords": kws, "task": "enqueued", "task_id": task_id}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cv")
async def upload_cv(
    file: UploadFile = File(...),
    name: Optional[str] = Form(None),
    email: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    vacancy_id: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    try:
        # Используем более точный timestamp с микросекундами для уникальности
        rid = str(int(time.time() * 1000000))
        ext = os.path.splitext(file.filename or "cv")[1]
        data = await file.read()
        storage_path, _ = storage.save_bytes(data, "cv", f"{rid}{ext}")
        # Имя кандидата и скиллы
        inferred_name = name or re.sub(r"[_-]+", " ", os.path.splitext(file.filename or "Candidate")[0])
        text = _extract_text_from_bytes(data, ext)
        base_text = (file.filename or "") + "\n" + text
        skills = _infer_keywords(base_text)
        contacts = _parse_contacts(base_text)
        experience = _extract_experience(text)
        exp_info = _analyze_experience(experience)
        total_years = round(exp_info.get("total_months", 0) / 12.0, 2)
        auto_flags = exp_info.get("flags", [])
        # Получаем название вакансии, если передан vacancy_id
        vacancy_title = None
        if vacancy_id and vacancy_id.isdigit():
            vac_result = await session.execute(select(Vacancy).where(Vacancy.id == int(vacancy_id)))
            vac = vac_result.scalar_one_or_none()
            if vac:
                vacancy_title = vac.title
        
        cand = Candidate(
            name=inferred_name,
            email=email,
            phone=phone,
            source="upload",
            tags={
                "skills": skills,
                "cv_path": storage_path,
                "contacts": contacts,
                "experience": experience,
                "total_exp_years": total_years,
                "flags": auto_flags,
                "status": "processing",
                "vacancy_id": (int(vacancy_id) if (vacancy_id and vacancy_id.isdigit()) else None),
                "vacancy_title": vacancy_title,
            },
        )
        session.add(cand)
        await session.commit()
        await session.refresh(cand)

        # Issue PML token and URL (use selected vacancy if передан)
        try:
            jti = secrets.token_urlsafe(8)
            exp = int(time.time()) + 7 * 24 * 3600
            vid = (vacancy_id or "CV").strip() or "CV"
            claims = {"jti": jti, "vid": vid, "cid": str(cand.id), "mode": "pml", "exp": exp}
            token = sign_jwt_like(claims, settings.auth_secret)
            it = InviteToken(jti=jti, candidate_id=int(cand.id), vacancy_id=None, mode="pml", exp=datetime.fromtimestamp(exp, tz=timezone.utc))
            session.add(it)
            # also persist into candidate tags
            tags = cand.tags or {}
            pml_url = f"/i/{vid}/start?t={token}&cid={cand.id}"
            tags["pml_url"] = pml_url
            cand.tags = tags
            await session.commit()
        except Exception:
            pml_url = None

        # enqueue matching task (Celery) — вычислить соответствие JD → обновить tags.match_pct и status
        try:
            from .upload import task_match_candidate  # type: ignore
        except Exception:
            task_match_candidate = None  # type: ignore
        try:
            if task_match_candidate is not None:
                ar = celery_app.send_task("cv.match_candidate", args=[cand.id])
        except Exception:
            pass
        return {"candidate_id": cand.id, "path": storage_path, "skills": skills, "pml_url": pml_url}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/cv/file/{candidate_id}")
async def download_cv_file(candidate_id: int, session: AsyncSession = Depends(get_session)) -> FileResponse:
    cand = await session.get(Candidate, candidate_id)
    if not cand:
        raise HTTPException(status_code=404, detail="candidate not found")
    path = ((cand.tags or {}).get("cv_path")) or ""
    if not path or not isinstance(path, str):
        raise HTTPException(status_code=404, detail="file not available")
    # Если локальное хранилище — отдаем файл; если S3 — редирект мог бы быть добавлен позже
    if not path.startswith("/"):
        # локальные пути в конфиге storage.local_root уже абсолютные
        pass
    filename = os.path.basename(path)
    try:
        return FileResponse(path, filename=filename, media_type="application/octet-stream")
    except Exception:
        raise HTTPException(status_code=404, detail="file not found")


@router.post("/cv_link")
async def upload_cv_link(
    body: CvLinkRequest,
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    """Импорт резюме по открытой ссылке HH.ru (или другой страницы): попытка скачать и распарсить текст.
    Этичный режим: без обхода защиты, только если контент доступен без авторизации.
    """
    try:
        url = (body.url or '').strip()
        if not url:
            raise HTTPException(status_code=400, detail="url required")
        # Пробуем получить контент страницы
        try:
            resp = requests.get(url, timeout=10, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; hr-import-bot/1.0)'
            })
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"fetch error: {e}")
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail="unavailable")
        html = resp.text or ''
        text = _strip_html(html)
        if not text or len(text) < 100:
            # Слишком мало текста — предложить загрузить PDF
            raise HTTPException(status_code=422, detail="insufficient public data; upload PDF")

        # Эвристики: имя из заголовка/URL
        inferred_name = body.name or (re.sub(r"[?#].*$", "", url).split('/')[-1].replace('-', ' ').replace('_', ' ')[:80] or 'Candidate')
        skills = _infer_keywords(text)
        contacts = _parse_contacts(text)
        experience = _extract_experience(text)
        exp_info = _analyze_experience(experience)
        total_years = round(exp_info.get("total_months", 0) / 12.0, 2)
        auto_flags = exp_info.get("flags", [])

        # Сохраняем сырой HTML как .html в сторадже для трассировки
        rid = str(int(time.time()))
        storage_path, _ = storage.save_bytes(html.encode('utf-8', errors='ignore'), "cv", f"{rid}.html")

        cand = Candidate(
            name=inferred_name,
            email=body.email or contacts.get('email'),
            phone=body.phone or contacts.get('phone'),
            source="hh_link",
            tags={
                "skills": skills,
                "cv_path": storage_path,
                "contacts": contacts,
                "experience": experience,
                "total_exp_years": total_years,
                "flags": auto_flags,
                "source_url": url,
                "status": "processing",
            },
        )
        session.add(cand)
        await session.commit()
        await session.refresh(cand)

        # Issue PML token and URL (VID=CV)
        try:
            jti = secrets.token_urlsafe(8)
            exp = int(time.time()) + 7 * 24 * 3600
            claims = {"jti": jti, "vid": "CV", "cid": str(cand.id), "mode": "pml", "exp": exp}
            token = sign_jwt_like(claims, settings.auth_secret)
            it = InviteToken(jti=jti, candidate_id=int(cand.id), vacancy_id=None, mode="pml", exp=datetime.fromtimestamp(exp, tz=timezone.utc))
            session.add(it)
            tags = cand.tags or {}
            pml_url = f"/i/CV/start?t={token}&cid={cand.id}"
            tags["pml_url"] = pml_url
            cand.tags = tags
            await session.commit()
        except Exception:
            pml_url = None

        try:
            from .upload import task_match_candidate  # type: ignore
        except Exception:
            task_match_candidate = None  # type: ignore
        try:
            if task_match_candidate is not None:
                ar = celery_app.send_task("cv.match_candidate", args=[cand.id])
        except Exception:
            pass
        return {"candidate_id": cand.id, "path": storage_path, "skills": skills, "pml_url": pml_url}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))
