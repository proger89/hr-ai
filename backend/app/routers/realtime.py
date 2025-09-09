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
    score_and_finalize,
    REALTIME_WS_URL,
    REALTIME_MODEL
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/realtime", tags=["realtime"])


def get_greeting(lang: str) -> str:
    hour = datetime.now().hour
    if lang == "ru":
        if hour < 12:
            return "Доброе утро"
        if hour < 18:
            return "Добрый день"
        return "Добрый вечер"
    else:
        if hour < 12:
            return "Good morning"
        if hour < 18:
            return "Good afternoon"
        return "Good evening"


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
        realtime_session.scenario = scenario  # Сохраняем сценарий в сессии
        realtime_sessions[session_id] = realtime_session
        # Проставим количество вопросов по сценарию, если он задан
        if scenario and isinstance(scenario, list):
            try:
                # Подсчитываем только первичные вопросы (все кроме intro и final)
                primary_count = 0
                for q in scenario:
                    if isinstance(q, dict):
                        competence = q.get("competence", "")
                        if competence and competence not in ["intro", "final"]:
                            primary_count += 1
                realtime_session.total_questions = max(1, primary_count or len(scenario))
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
        greet = get_greeting(candidate_lang)
        ru_instr = (
            f"{greet}! Ты HR-интервьюер.Твоя задача - задать все вопросы сценария собеседнику, обязательно выслушать его ответы до конца. НЕ ОТВЛЕКАЙСЯ НА СТОРОННИЕ ТЕМЫ В БЕСЕДЕ, СФОКУСИРУЙСЯ НА ИНТЕРВЬЮ О КОНКРЕТНОЙ. Ты должна задавать чаще вопрос - а не говорить самой. Тебе нужно понять все профессиональные навыки собеседника, относящиеся к вакансии по сценарию. Также оцени как коммуницирует собеседник. В конце встречи ты должна оценить его soft-skills. Старайся поддерживать беседу корректными, актуальными вопросами, развивая и углубляясь в предмет собеседования, но не слишком затягивать его. Сразу задай первый вопрос: {q_text}. Затем вызови tool `question_asked` с index=1."
            if q_text else
            f"{greet}! Ты HR-интервьюер.Твоя задача - задать все вопросы сценария собеседнику, обязательно выслушать его ответы до конца.  НЕ ОТВЛЕКАЙСЯ НА СТОРОННИЕ ТЕМЫ В БЕСЕДЕ, СФОКУСИРУЙСЯ НА ИНТЕРВЬЮ О КОНКРЕТНОЙ. Ты должна задавать чаще вопрос - а не говорить самой. Тебе нужно понять все профессиональные навыки собеседника, относящиеся к вакансии по сценарию. Также оцени как коммуницирует собеседник. В конце встречи ты должна оценить его soft-skills. Старайся поддерживать беседу корректными, актуальными вопросами, развивая и углубляясь в предмет собеседования, но не слишком затягивать его. Затем вызови tool `question_asked` с index=1."
        )
        en_instr = (
            f"{greet}! You are the HR interviewer. Immediately ask the first question: {q_text}. Then call tool `question_asked` with index=1."
            if q_text else
            f"{greet}! You are the HR interviewer. Immediately ask the first question: Please tell me about yourself. Then call tool `question_asked` with index=1."
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

            etype = data.get("type")
            # Фиксация языка пайплайна от клиента и обновление сессии OpenAI
            if etype == "session.update_lang":
                lang_raw = (data.get("lang") or "").lower()
                lang = "ru" if lang_raw.startswith("ru") else ("en" if lang_raw.startswith("en") else "en")
                session.context["lang"] = lang
                session.lang = lang
                voice = "verse" if lang == "ru" else "alloy"
                session_update = {
                    "type": "session.update",
                    "session": {
                        "voice": voice,
                        "input_audio_transcription": {
                            "model": "gpt-4o-mini-transcribe",
                            "language": ("ru" if lang == "ru" else "en")
                        }
                    }
                }
                await openai_ws.send(json.dumps(session_update))
                # Сообщаем клиенту, что язык обновлён
                await client_ws.send_json({"type": "session.lang.locked", "lang": lang})
                continue


            # Полудуплексная защита на сервере: когда ассистент говорит — не принимаем микрофонный апстрим
            if etype == "input_audio_buffer.append" and session.assistant_speaking:
                continue

            # При явной отмене со стороны клиента сбрасываем флаги
            if etype == "response.cancel":
                try:
                    await openai_ws.send(json.dumps({"type": "response.cancel"}))
                finally:
                    session.assistant_speaking = False
                    session.active_response_id = None
                continue

            # Гарантия одного активного ответа: перед новым response.create отменяем предыдущий
            if etype == "response.create" and session.active_response_id:
                try:
                    await openai_ws.send(json.dumps({"type": "response.cancel"}))
                except Exception:
                    logger.debug("response.cancel before create failed (ignored)")

            # Обрабатываем stt.user_final для трекинга времени речи
            if etype == "stt.user_final":
                ms = data.get("ms", 0)
                if ms > 0:
                    session.user_speaking_ms += ms
                continue
            
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

            # Начало нового ответа: фиксируем активный ответ и флаги
            if data.get("type") == "response.created":
                session.question_marked = False
                response_obj = data.get("response") or {}
                session.active_response_id = response_obj.get("id") or data.get("response_id")
                session.assistant_speaking = True
                # Отправляем клиенту с response_id
                await client_ws.send_json({
                    "type": "response.created",
                    "response": response_obj
                })
                continue

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
                if function_name == "end_interview" and result.get("status") == "success":
                    # Финальный скоринг
                    final_results = await score_and_finalize(session)
                    
                    # Отправляем событие завершения с redirect_url
                    await client_ws.send_json({
                        "type": "interview.completed",
                        "decision": final_results["decision"],
                        "redirect_url": final_results["redirect_url"]
                    })
            
            # Сохраняем элементы разговора
            if data.get("type") == "conversation.item.created":
                item = data.get("item", {})
                session.conversation_items.append(item)
            
            # Отслеживаем прогресс через question_asked (антидребезг: только один раз за ответ и +1 шаг)
            if data.get("type") == "response.function_call":
                fn = data.get("function", {}).get("name")
                args_raw = data.get("function", {}).get("arguments", "{}")
                try:
                    args = json.loads(args_raw)
                except Exception:
                    args = {}
                if fn == "question_asked":
                    if session.question_marked:
                        pass
                    else:
                        session.question_marked = True
                        # Определяем является ли вопрос первичным
                        question_idx = args.get("index", session.current_question + 1)
                        is_primary = args.get("is_primary", True)
                        
                        # Проверяем компетенцию из сценария
                        if not args.get("is_primary") and hasattr(session, "scenario") and session.scenario:
                            try:
                                q_data = session.scenario[question_idx - 1] if question_idx <= len(session.scenario) else None
                                if q_data and isinstance(q_data, dict):
                                    competence = q_data.get("competence", "")
                                    is_primary = competence not in ["intro", "final"]
                            except:
                                pass
                        
                        if is_primary:
                            session.answered_primary += 1
                            idx = session.current_question + 1
                            idx = max(1, min(idx, session.total_questions))
                            if idx > session.current_question:
                                session.current_question = idx
                                await client_ws.send_json({
                                    "type": "progress.update",
                                    "current": session.current_question,
                                    "total": session.total_questions
                                })
                        # Авто‑финиш: если достигли лимита — заставляем модель вызвать end_interview
                        if (
                            session.current_question >= session.total_questions
                            and not session.context.get("interview_completed")
                            and session.answered_primary >= session.min_primary_required
                            and session.user_speaking_ms >= session.min_dialog_ms
                        ):
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

            # Фолбэк удалён: прогресс обновляется только при вызове question_asked

            # Фильтрация аудио-дельт по активному ответу, если Realtime шлёт идентификаторы
            if data.get("type") == "response.audio.delta":
                resp = data.get("response") or {}
                resp_id = resp.get("id") or data.get("response_id")
                active_id = session.active_response_id
                if active_id and resp_id and resp_id != active_id:
                    continue
                # Добавляем response_id к данным для клиента
                data["response_id"] = resp_id or active_id

            # Конец ответа - сбрасываем флаги
            if data.get("type") in ("response.audio.done", "response.done"):
                session.assistant_speaking = False
                session.active_response_id = None
            
            # STT от пользователя - трекаем время
            if data.get("type") == "conversation.item.input_audio_transcription.completed":
                # Примерная оценка длительности по транскрипту
                transcript = data.get("transcript", "")
                words = len(transcript.split())
                # ~150 слов в минуту средняя скорость речи
                session.user_speaking_ms += int((words / 150) * 60 * 1000)
            
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