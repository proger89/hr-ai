from __future__ import annotations

import asyncio
from typing import AsyncIterator, Dict

import grpc
import os

from ..services.oauth import get_salutespeech_access_token


class STTNotReadyError(RuntimeError):
    pass


async def recognize_stream(
    audio_chunks: AsyncIterator[bytes],
    language: str = "ru-RU",
    sample_rate: int = 16000,
) -> AsyncIterator[Dict[str, str]]:
    """
    Прокидывает аудио-чанки в SaluteSpeech streaming и выдаёт события:
    {"type":"partial","text":...} или {"type":"final","text":...}.

    Требует установленных gRPC-стабов от proto recognition-stream v2.
    Если стабы отсутствуют, выбрасывает STTNotReadyError с инструкцией.
    """
    try:
        # Сначала пытаемся загрузить наши сгенерированные v2 стабы (относительный импорт из app.stt)
        try:
            from . import recognitionv2_pb2 as pb  # type: ignore
            # gRPC-плагин генерирует абсолютный импорт `import recognitionv2_pb2` внутри *_pb2_grpc.py.
            # Зарегистрируем модуль в sys.modules под ожидаемым именем, чтобы импорт прошёл.
            import sys as _sys  # noqa: N812
            _sys.modules.setdefault("recognitionv2_pb2", pb)
            from . import recognitionv2_pb2_grpc as stt_grpc  # type: ignore
        except Exception:
            # Популярные варианты имён модулей в других примерах (глобальные имена)
            try:
                import recognitionv2_pb2 as pb  # type: ignore
                import recognitionv2_pb2_grpc as stt_grpc  # type: ignore
            except Exception:
                import salutespeech_pb2 as pb  # type: ignore
                import salutespeech_pb2_grpc as stt_grpc  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise STTNotReadyError(
            "gRPC стабы SaluteSpeech не найдены. Сгенерируйте *_pb2.py и *_pb2_grpc.py из proto."
        ) from exc

    access_token, _ = get_salutespeech_access_token()

    # Если ходим через локальный TLS-прокси (stunnel), то к нему подключаемся без TLS
    use_plaintext = os.getenv("SMARTSPEECH_PLAINTEXT") in ("1", "true", "True")

    # Опциональный путь к bundle с корпоративным CA для прямого TLS
    # По умолчанию ожидаем готовый бандл в /app/ca/ru_bundle.pem
    ca_bundle_path = os.getenv("SMARTSPEECH_CA_BUNDLE", "/app/ca/ru_bundle.pem")
    root_certs: bytes | None = None
    if not use_plaintext:
        candidates: list[bytes] = []
        try:
            import os as _os
            from os import path as _p
            import base64 as _b64
            import certifi as _certifi  # type: ignore

            def _to_pem(data: bytes) -> bytes:
                if b"-----BEGIN CERTIFICATE-----" in data:
                    return data
                b64 = _b64.b64encode(data).decode("ascii")
                wrapped = "\n".join(b64[i : i + 64] for i in range(0, len(b64), 64))
                return (
                    b"-----BEGIN CERTIFICATE-----\n"
                    + wrapped.encode("ascii")
                    + b"\n-----END CERTIFICATE-----\n"
                )

            def _read_if_exists(p: str) -> None:
                if _p.exists(p):
                    try:
                        with open(p, "rb") as _fh:
                            data = _fh.read()
                        if data:
                            candidates.append(_to_pem(data))
                    except Exception:
                        pass

            # 1) Если указан готовый бандл-файл — используем только его
            if _p.isfile(ca_bundle_path):
                _read_if_exists(ca_bundle_path)
            else:
                # 2) Иначе соберём минимум: certifi + RU CA
                try:
                    _cert_path = _certifi.where()
                    if _p.exists(_cert_path):
                        with open(_cert_path, "rb") as _fh:
                            candidates.append(_fh.read())
                except Exception:
                    pass
                dir_path = "/app/ca"
                _read_if_exists(_p.join(dir_path, "russian_trusted_root_ca.cer"))
                _read_if_exists(_p.join(dir_path, "russian_trusted_sub_ca.cer"))

            if candidates:
                root_certs = b"".join(candidates)
        except Exception:
            root_certs = None

    # Пара keepalive-настроек для стабильности
    options = [
        ("grpc.keepalive_time_ms", 20000),
        ("grpc.keepalive_timeout_ms", 10000),
        ("grpc.http2.min_time_between_pings_ms", 15000),
        ("grpc.http2.max_pings_without_data", 0),
        ("grpc.max_receive_message_length", 20 * 1024 * 1024),
        ("grpc.max_send_message_length", 20 * 1024 * 1024),
    ]
    # Не переопределяем SNI/authority — пусть gRPC установит автоматически
    # Диагностика загруженных корневых
    try:
        size = len(root_certs or b"")
        print(f"[STT] endpoint={endpoint} root_certs_bytes={size}")
    except Exception:
        pass
    
    metadata = (("authorization", f"Bearer {access_token}"),)

    # Берём явный endpoint, корректно обрабатываем пустую переменную
    endpoint = (os.getenv("SMARTSPEECH_GRPC_ENDPOINT") or "smartspeech.sber.ru:443").strip()
    
    # Используем российские корневые сертификаты НУЦ Минцифры
    # Явно передаем содержимое, если нашли (предпочтительно)
    # Доверим выбор корней самому gRPC (учтёт GRPC_DEFAULT_SSL_ROOTS_FILE_PATH)
    ssl_creds = grpc.ssl_channel_credentials(root_certificates=root_certs or None)
    
    channel_factory = lambda: grpc.aio.secure_channel(endpoint, ssl_creds, options=options)
    async with channel_factory() as channel:
        # Имя сервиса в v2 может быть SmartSpeech
        try:
            stub = stt_grpc.SmartSpeechStub(channel)  # type: ignore[attr-defined]
        except Exception:
            stub = stt_grpc.RecognizerStub(channel)  # type: ignore[attr-defined]

        async def req_iter() -> AsyncIterator[object]:
            # 1) опции
            # Для v2 поля обёрнуты в OptionalBool; пробуем оба варианта
            try:
                opts = pb.RecognitionOptions(  # type: ignore[attr-defined]
                    audio_encoding=pb.RecognitionOptions.AudioEncoding.PCM_S16LE,
                    sample_rate=sample_rate,
                    language=language,
                    enable_partial_results={"enable": True},
                    enable_multi_utterance={"enable": True},
                    no_speech_timeout={"seconds": 7},
                    max_speech_timeout={"seconds": 20},
                )
            except Exception:
                opts = pb.RecognitionOptions(  # type: ignore[attr-defined]
                    audio_encoding=pb.AudioEncoding.PCM_S16LE,  # type: ignore[attr-defined]
                    sample_rate=sample_rate,
                    enable_partial_results=True,
                    enable_multi_utterance=True,
                    language=language,
                    no_speech_timeout={"seconds": 7},
                    max_speech_timeout={"seconds": 20},
                )
            yield pb.RecognitionRequest(options=opts)  # type: ignore[attr-defined]

            # 2) аудио-чанки
            async for chunk in audio_chunks:
                if not chunk:
                    continue
                yield pb.RecognitionRequest(audio_chunk=chunk)  # type: ignore[attr-defined]

        async for resp in stub.Recognize(req_iter(), metadata=metadata):  # type: ignore[attr-defined]
            # v2: resp.response=transcription/backend_info/...
            tr = getattr(resp, "transcription", None)
            if tr is None:
                continue
            results = getattr(tr, "results", None)
            if not results:
                continue
            hyp = results[0]
            text = getattr(hyp, "normalized_text", None) or getattr(hyp, "text", "")
            if getattr(tr, "eou", False):
                yield {"type": "final", "text": text}
            else:
                yield {"type": "partial", "text": text}


