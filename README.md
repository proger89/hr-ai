## Sber Interviewer — краткая документация (Windows/PowerShell + Docker)

### Требования
- **Windows**: 10/11
- **PowerShell**: 5.1+ или PowerShell 7+
- **Docker Desktop** с поддержкой Docker Compose v2

### Быстрый старт (PowerShell)
```powershell
cd D:\sites\sber
# Заполните .env (см. ниже)
docker compose up -d --build
```
- **Фронтенд**: `http://localhost:8080`
- **Проверка бэкенда**: `curl http://localhost:8001/healthz`

Остановка/очистка:
```powershell
docker compose stop
# ВНИМАНИЕ: удалит volume с БД (данные пропадут)
docker compose down -v
```

### .env (переменные окружения)
Создайте файл `.env` рядом с `docker-compose.yml`:
```dotenv
PROFILE=sber

# Обязательно для внешних интеграций
SBER_SMARTSPEECH_AUTH_KEY=base64_client_colon_secret   # OAuth для SmartSpeech (Basic)
GIGACHAT_AUTH_KEY=base64_client_colon_secret           # OAuth для GigaChat (Basic)

# Опционально
GIGACHAT_BASE_URL=https://gigachat.devices.sberbank.ru/api/v1
YADISK_OAUTH=
DATABASE_URL=postgresql+asyncpg://sber:sber@db:5432/sber
ADMIN_USER=admin
ADMIN_PASSWORD=admin
AUTH_SECRET=change_me_secret

# Хранилище файлов: local или s3
STORAGE_BACKEND=local
STORAGE_LOCAL_ROOT=/app/uploads
# S3 (если STORAGE_BACKEND=s3)
S3_BUCKET=
S3_REGION=
S3_ENDPOINT=
S3_ACCESS_KEY=
S3_SECRET_KEY=

# VoIP
VOIP_PROVIDER=simulated # voximplant|zadarma|simulated
VOXIMPLANT_ACCOUNT=
VOXIMPLANT_API_KEY=
ZADARMA_KEY=
ZADARMA_SECRET=
```

### Что работает в Docker
- **backend** (FastAPI, Python 3.11)
  - Внутренний порт 8000, проброшен на `localhost:8001`
  - HTTP-префикс `/api`, WebSocket `/ws`
  - CA-бандл для TLS: `/app/ca/ru_bundle.pem`
- **db** (PostgreSQL 15)
 - **db** (PostgreSQL 16 + pgvector)
  - Внутренний порт 5432, проброшен на `localhost:5433`
  - Данные в volume `pgdata`
- **nginx** (статический фронт + реверс-прокси)
  - Внутренний порт 80, проброшен на `localhost:8080`
  - Раздаёт `frontend/` и проксирует `/api/` и `/ws/` на backend
- **stunnel** (TLS-туннель к smartspeech.sber.ru)
  - Слушает `0.0.0.0:7443`, подключается к `smartspeech.sber.ru:443`

### Порты
- **8080**: Nginx (фронт и прокси)
- **8001**: Backend (FastAPI)
- **5433**: PostgreSQL
- **7443**: stunnel

### Основные маршруты
- **HTTP (через Nginx)**: `http://localhost:8080`
  - `/api/auth/login`, `/api/auth/verify`
  - `/api/tts/synthesize`, `/api/tts/synthesize/stream`, `/api/tts/stop/{request_id}`
  - `/api/gigachat/chat`, `/api/gigachat/embeddings`
  - `/api/upload/jd`, `/api/upload/cv`
  - `/api/match/score` — скоринг пары JD↔CV
  - `/api/match/shortlist` — топ‑K по простому скору
  - `/api/match/score_and_shortlist` — подробный скоринг списком + топ‑K
  - `/api/scheduler/slots?vacancy_id=` — список слотов
  - `/api/scheduler/slot` (POST) — создать слот
  - `/api/scheduler/book` (POST) — забронировать слот
  - `/api/scheduler/slot/{id}/ics` — скачать ICS для слота
  - `/api/dialog/next` (POST) — следующий вопрос по ответу (через GigaChat с фолбэком)
  - `/api/dialog/followup` (POST) — уточняющий вопрос по ответу
  - `/api/analysis/score` (POST) — оценка кандидата (tech/comm/cases/total)
  - `/api/analysis/rank` (POST) — ранжирование списка кандидатов
  - `/api/invitations/verify` — верификация одноразового инвайт‑токена (TTL, replay)
  - `/api/embeddings` (POST) — сохраняет эмбеддинг (kind, ref_id, vector)
  - `/api/embeddings/search` (POST) — поиск по косинусному подобию (упрощённо — dot)
  - `/api/contacts` (POST) — создать событие контакта
  - `/api/contacts/{candidate_id}` — список событий контактов
  - `/api/vacancies` — список вакансий
  - `/api/upload/jd_text` (POST) — создать вакансию из текста
  - `/api/voip/call` (POST) — создать исходящий звонок (симуляция/провайдер)
  - `/api/voip/webhook` (POST) — приём вебхуков провайдера (call.started/dtmf/finished)
  - `/api/voip/call/{id}` — статус звонка и события
  - `/api/voip/calls?limit=20` — список последних звонков
  - `/api/voip/ivr/next?call_id=` — сгенерировать IVR-подсказку по ближайшим слотам
  - `/api/voip/prescreen/start` (POST) — начать телефонный прескрининг (DTMF)
  - `/healthz`
- **WebSocket**:
  - `ws://localhost:8080/ws/stt?lang=ru-RU` — распознавание речи (STT)
  - `ws://localhost:8001/ws/audio` — тестовый echo по аудиофреймам
- **Фронтенд-страницы**: `/`, `/i/`, `/v/`, `/s/` (см. `nginx/conf.d/default.conf`)

### Вход в HR‑админку
- URL: `http://localhost:8080/hr.html`
- Логин/пароль по умолчанию: `admin` / `admin`
- Настраиваются через переменные окружения `.env`:
  - `ADMIN_USER=admin`
  - `ADMIN_PASSWORD=admin`
  - `AUTH_SECRET=change_me_secret` (секрет для JWT)
- Проверка через API:
```powershell
curl -X POST -F "username=admin" -F "password=admin" http://localhost:8080/api/auth/login
```

### Заметки по безопасности
- Ключи для SmartSpeech/GigaChat храните вне репозитория.
- Для запросов к внешним сервисам используется CA-бандл `/app/ca/ru_bundle.pem`.
- В dev-режиме OAuth к SmartSpeech может выполняться с ослабленной проверкой TLS (см. `backend/app/services/oauth.py`). В проде включайте строгую проверку.

### Разработка
- Код бэкенда монтируется из `./backend/app` в контейнер. После изменений перезапускайте контейнер бэкенда, если изменения не подхватились автоматически.
