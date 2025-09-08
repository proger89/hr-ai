from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from ..stt.client import recognize_stream
import asyncio
import contextlib
import numpy as np  # type: ignore
import time

router = APIRouter()


@router.websocket("/ws/stt")
async def stt_ws(ws: WebSocket):
    await ws.accept()

    # Язык из query (?lang=ru-RU|en-US), по умолчанию ru-RU
    try:
        lang = ws.query_params.get("lang", "ru-RU")  # type: ignore[attr-defined]
    except Exception:
        lang = "ru-RU"

    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
    outq: asyncio.Queue[dict] = asyncio.Queue(maxsize=200)

    async def reader():
        vad_state = {"voice": False, "last_emit": 0.0}
        thr = 0.025  # RMS threshold for voice
        try:
            while True:
                data = await ws.receive_bytes()
                # Ограничим размер, чтобы не разрасталось
                if queue.qsize() > 90:
                    _ = queue.get_nowait()
                # VAD (RMS) вычисление поверх входящих PCM16
                try:
                    arr = np.frombuffer(data, dtype='<i2')
                    if arr.size:
                        rms = float(np.sqrt(np.mean((arr.astype(np.float32) / 32768.0) ** 2)))
                        voice = rms > thr
                        now = time.monotonic()
                        changed = voice != vad_state["voice"]
                        rate_limited = (now - vad_state["last_emit"]) >= 0.5 if voice else True
                        if changed or rate_limited:
                            item = {"type": "vad", "voice": voice, "rms": round(rms, 4)}
                            try:
                                outq.put_nowait(item)
                            except asyncio.QueueFull:
                                try:
                                    _ = outq.get_nowait()
                                except Exception:
                                    pass
                                await outq.put(item)
                            vad_state["voice"] = voice
                            vad_state["last_emit"] = now
                except Exception:
                    # не ломаем поток при ошибке VAD
                    pass
                await queue.put(data)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            # завершающий пустой блок
            await queue.put(b"")

    async def chunk_iter():
        while True:
            chunk = await queue.get()
            if chunk == b"":
                break
            yield chunk

    async def recognizer():
        async for event in recognize_stream(chunk_iter(), language=lang):
            try:
                outq.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    _ = outq.get_nowait()
                except Exception:
                    pass
                await outq.put(event)

    async def sender():
        try:
            while True:
                item = await outq.get()
                await ws.send_json(item)
        except WebSocketDisconnect:
            return
        except Exception:
            return

    reader_task = asyncio.create_task(reader())
    rec_task = asyncio.create_task(recognizer())
    sender_task = asyncio.create_task(sender())
    try:
        await asyncio.wait(
            {reader_task, rec_task, sender_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for t in (reader_task, rec_task, sender_task):
            with contextlib.suppress(Exception):
                t.cancel()
        for t in (reader_task, rec_task, sender_task):
            with contextlib.suppress(Exception):
                await t


