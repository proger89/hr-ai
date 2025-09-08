"""
WebSocket роутер для проксирования OpenAI Realtime API
"""
import json
import uuid
import asyncio
import logging
from datetime import datetime
from typing import Dict, Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
import websockets

from ..db import SessionLocal
from ..models import Candidate, Vacancy
from ..services.realtime_service import (
    RealtimeSession,
    realtime_sessions,
    create_session_config,
    get_ws_headers,
    handle_function_call,
    REALTIME_WS_URL,
    REALTIME_MODEL
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/realtime", tags=["realtime"])


async def save_interview_results(
    session: RealtimeSession,
    db_session: AsyncSession
):
    """Сохранение результатов интервью в БД"""
    try:
        candidate = await db_session.get(Candidate, int(session.candidate_id))
        if candidate:
            if not candidate.tags:
                candidate.tags = {}
            
            # Сохраняем результаты
            candidate.tags["interview_completed"] = True
            candidate.tags["interview_score"] = session.context.get("overall_score", 0)
            candidate.tags["interview_passed"] = session.context.get("passed", False)
            candidate.tags["interview_date"] = datetime.now().isoformat()
            candidate.tags["interview_recommendation"] = session.context.get("recommendation", "maybe")
            candidate.tags["interview_summary"] = {
                "scores": session.scores,
                "strengths": session.context.get("strengths", []),
                "weaknesses": session.context.get("weaknesses", []),
                "conversation_items": len(session.conversation_items),
                "duration": (datetime.now() - session.created_at).total_seconds()
            }
            
            await db_session.commit()
            logger.info(f"Saved interview results for candidate {session.candidate_id}")
            
    except Exception as e:
        logger.error(f"Error saving interview results: {e}")


@router.websocket("/ws/{candidate_id}")
async def realtime_websocket(
    websocket: WebSocket,
    candidate_id: str
):
    """WebSocket endpoint для Realtime интервью"""
    await websocket.accept()
    
    openai_ws = None
    realtime_session = None
    db = None
    
    try:
        # Создаем сессию БД напрямую
        db = SessionLocal()
        
        # Получаем данные кандидата
        cand_id = int(candidate_id) if candidate_id.isdigit() else candidate_id
        candidate = await db.get(Candidate, cand_id)
        if not candidate:
            await websocket.send_json({
                "type": "error",
                "error": {"message": "Кандидат не найден"}
            })
            await websocket.close()
            return
        
        # Получаем вакансию
        vacancy_id = candidate.tags.get("vacancy_id") if candidate.tags else None
        vacancy = None
        if vacancy_id:
            vacancy = await db.get(Vacancy, vacancy_id)
        
        # Подготавливаем контекст
        resume_text = candidate.tags.get("cv_text", "") if candidate.tags else ""
        jd_text = ""
        scenario = []
        
        if vacancy:
            jd_text = vacancy.jd_json.get("text", "") if vacancy.jd_json else ""
            raw_scenario = vacancy.jd_json.get("scenario", {}) if vacancy and vacancy.jd_json else {}
            # Нормализуем сценарий: поддержка как dict секций, так и уже list
            scenario = []
            if isinstance(raw_scenario, list):
                scenario = [q for q in raw_scenario if isinstance(q, dict)]
            elif isinstance(raw_scenario, dict):
                for key in ["intro", "experience", "stack", "cases", "communication", "final"]:
                    val = raw_scenario.get(key)
                    if isinstance(val, str) and val.strip():
                        scenario.append({"competence": key, "question": val.strip()})
        
        # Определяем язык из query или из профиля
        query_params = dict(websocket.query_params)
        client_lang = (query_params.get("lang") or "").lower()
        # Нормализуем к 'ru'|'en'
        def normalize_lang(value: str) -> str:
            if not value:
                return "en"
            if value.startswith("ru"):
                return "ru"
            if value.startswith("en"):
                return "en"
            return "en"
        normalized_lang = normalize_lang(client_lang)

        # Сохраняем в candidate.lang для последующих сессий
        try:
            prev_lang = getattr(candidate, 'lang', None)
            if prev_lang != normalized_lang:
                setattr(candidate, 'lang', normalized_lang)
                await db.commit()
        except Exception:
            pass

        # Создаем сессию
        session_id = str(uuid.uuid4())
        realtime_session = RealtimeSession(session_id, str(cand_id), vacancy_id)
        realtime_sessions[session_id] = realtime_session
        # Проставим количество вопросов по сценарию, если он задан
        if scenario and isinstance(scenario, list):
            try:
                realtime_session.total_questions = max(1, min(5, len(scenario)))
            except Exception:
                pass
        
        # Подключаемся к OpenAI Realtime API
        ws_url = f"{REALTIME_WS_URL}?model={REALTIME_MODEL}"
        logger.info(f"Connecting to OpenAI Realtime API: {ws_url}")
        
        headers = get_ws_headers()
        logger.debug(f"WebSocket headers: {list(headers.keys())}")
        
        openai_ws = await websockets.connect(
            ws_url,
            extra_headers=headers,
            open_timeout=30  # Увеличиваем таймаут до 30 секунд
        )
        logger.info("Successfully connected to OpenAI Realtime API")

        # Уведомляем браузер о создании сессии только после подключения к OpenAI
        await websocket.send_json({
            "type": "session.created",
            "session": {
                "id": session_id,
                "candidate_id": str(cand_id),
                "vacancy_id": vacancy_id,
                "total_questions": realtime_session.total_questions
            }
        })
        
        # Настраиваем сессию в OpenAI
        candidate_lang = normalized_lang or getattr(candidate, 'lang', 'ru')
        logger.info(f"Candidate language: {candidate_lang}")
        
        session_config = create_session_config(
            resume_text="",  # не даём сырые тексты
            jd_text="",
            scenario=scenario,
            lang=candidate_lang
        )
        # Подставляем приватный контекст вместо сырого JD/CV
        try:
            from ..services.realtime_service import build_private_context
            private_context = build_private_context(candidate.tags or {}, vacancy.jd_json if vacancy else {}, candidate_lang)
            # Общее количество вопросов для плейсхолдера
            total_q = realtime_session.total_questions
            instr = session_config.get("instructions", "")
            instr = instr.replace("{PRIVATE_CONTEXT}", private_context)
            instr = instr.replace("{TOTAL_Q}", str(total_q))
            session_config["instructions"] = instr
        except Exception as _e:
            logger.warning(f"PRIVATE_CONTEXT inject failed: {_e}")
        
        await openai_ws.send(json.dumps({
            "type": "session.update",
            "session": session_config
        }))
        
        # Отправляем начальное сообщение и создаём стартовый ответ
        system_text_ru = "ВАЖНО: Весь разговор должен вестись ТОЛЬКО на русском языке. НЕ используй другие языки."
        system_text_en = "IMPORTANT: You MUST speak ONLY in English. Do not switch languages."
        await openai_ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "system",
                "content": [{
                    "type": "input_text",
                    "text": system_text_ru if candidate_lang == "ru" else system_text_en
                }]
            }
        }))

        # Запускаем интервью: формируем первый вопрос и просим модель поздороваться и задать его
        # Формируем первый вопрос: либо из сценария вакансии, либо дефолт
        q_text: Optional[str] = None
        if scenario and isinstance(scenario, list) and len(scenario) > 0:
            q0 = scenario[0]
            if isinstance(q0, dict):
                q_text = (q0.get("question") or "").strip() or None
        ru_instr = (
            f"Поздоровайся кратко по‑русски и сразу задай первый вопрос: {q_text}."
            if q_text else
            "Поздоровайся кратко по‑русски и сразу задай первый вопрос: Расскажите, пожалуйста, о себе."
        )
        en_instr = (
            f"Greet briefly in English and immediately ask the first question: {q_text}."
            if q_text else
            "Greet briefly in English and immediately ask the first question: Please tell me about yourself."
        )
        await openai_ws.send(json.dumps({
            "type": "response.create",
            "response": {
                "modalities": ["audio", "text"],
                "instructions": (ru_instr if candidate_lang == "ru" else en_instr)
            }
        }))
        
        # Запускаем параллельную обработку сообщений
        await asyncio.gather(
            proxy_client_to_openai(websocket, openai_ws, realtime_session),
            proxy_openai_to_client(openai_ws, websocket, realtime_session),
            return_exceptions=True
        )
        
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for candidate {candidate_id}")
    except Exception as e:
        import traceback
        logger.error(f"Error in realtime websocket: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        try:
            await websocket.send_json({
                "type": "error",
                "error": {"message": str(e)}
            })
        except:
            pass
    finally:
        # Сохраняем результаты интервью
        if realtime_session and realtime_session.context.get("interview_completed") and db:
            await save_interview_results(realtime_session, db)
        
        # Закрываем сессию БД
        if db:
            await db.close()
        
        # Удаляем сессию
        if realtime_session:
            realtime_sessions.pop(realtime_session.session_id, None)
        
        # Закрываем соединения
        if openai_ws:
            await openai_ws.close()
        try:
            await websocket.close()
        except:
            pass


async def proxy_client_to_openai(
    client_ws: WebSocket,
    openai_ws: websockets.WebSocketClientProtocol,
    session: RealtimeSession
):
    """Проксирование сообщений от клиента к OpenAI"""
    try:
        while True:
            data = await client_ws.receive_json()
            
            # Логируем для отладки
            logger.debug(f"Client -> OpenAI: {data.get('type')}")
            
            # Отправляем в OpenAI
            await openai_ws.send(json.dumps(data))
            
    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except Exception as e:
        logger.error(f"Error proxying client to OpenAI: {e}")


async def proxy_openai_to_client(
    openai_ws: websockets.WebSocketClientProtocol,
    client_ws: WebSocket,
    session: RealtimeSession
):
    """Проксирование сообщений от OpenAI к клиенту"""
    try:
        async for message in openai_ws:
            data = json.loads(message)
            
            # Логируем для отладки
            logger.debug(f"OpenAI -> Client: {data.get('type')}")
            
            # Обрабатываем специальные события
            if data.get("type") == "response.function_call":
                # Обрабатываем вызов функции
                function_name = data.get("function", {}).get("name")
                arguments = json.loads(data.get("function", {}).get("arguments", "{}"))
                
                result = await handle_function_call(session, function_name, arguments)
                
                # Отправляем результат обратно в OpenAI
                await openai_ws.send(json.dumps({
                    "type": "function_call_output",
                    "call_id": data.get("call_id"),
                    "output": json.dumps(result)
                }))
                
                # Если интервью завершено, отправляем событие клиенту
                if function_name == "end_interview":
                    await client_ws.send_json({
                        "type": "interview.completed",
                        "overall_score": result.get("overall_score", 0),
                        "passed": result.get("passed", False)
                    })
            
            # Сохраняем элементы разговора
            if data.get("type") == "conversation.item.created":
                item = data.get("item", {})
                session.conversation_items.append(item)
            
            # Отслеживаем прогресс только через question_asked
            if data.get("type") == "response.function_call":
                fn = data.get("function", {}).get("name")
                args_raw = data.get("function", {}).get("arguments", "{}")
                try:
                    args = json.loads(args_raw)
                except Exception:
                    args = {}
                if fn == "question_asked":
                    try:
                        idx = int(args.get("index") or (session.current_question + 1))
                    except Exception:
                        idx = session.current_question + 1
                    if idx > session.current_question:
                        session.current_question = idx
                        await client_ws.send_json({
                            "type": "progress.update",
                            "current": session.current_question,
                            "total": session.total_questions
                        })
                    # Авто‑финиш: если достигли лимита — заставляем модель вызвать end_interview
                    if session.current_question >= session.total_questions and not session.context.get("interview_completed"):
                        await openai_ws.send(json.dumps({
                            "type": "response.create",
                            "response": {
                                "modalities": ["audio", "text"],
                                "instructions": (
                                    "Call the tool `end_interview` NOW with your final overall_score, strengths, weaknesses, "
                                    "and recommendation (hire/maybe/reject). Then say a brief closing line. Do not ask new questions."
                                )
                            }
                        }))
            
            # Пересылаем клиенту
            await client_ws.send_json(data)
            
    except websockets.exceptions.ConnectionClosed:
        logger.info("OpenAI connection closed")
    except Exception as e:
        logger.error(f"Error proxying OpenAI to client: {e}")


@router.get("/session/{session_id}")
async def get_session_info(session_id: str) -> Dict[str, Any]:
    """Получить информацию о сессии"""
    session = realtime_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    
    return session.to_dict()