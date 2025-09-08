import os
from typing import Optional


def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name, default)
    return value


class Settings:
    profile: str = get_env("PROFILE", "sber") or "sber"
    smartspeech_auth_key: Optional[str] = get_env("SBER_SMARTSPEECH_AUTH_KEY")
    gigachat_auth_token: Optional[str] = get_env("GIGACHAT_AUTH_TOKEN")
    gigachat_base_url: str = get_env("GIGACHAT_BASE_URL", "https://gigachat.devices.sberbank.ru/api/v1")
    yadisk_oauth: Optional[str] = get_env("YADISK_OAUTH")
    database_url: Optional[str] = get_env("DATABASE_URL", "postgresql+asyncpg://sber:sber@db:5432/sber")
    admin_user: str = get_env("ADMIN_USER", "admin") or "admin"
    admin_password: str = get_env("ADMIN_PASSWORD", "admin") or "admin"
    auth_secret: str = get_env("AUTH_SECRET", "change_me_secret") or "change_me_secret"
    # Storage
    storage_backend: str = get_env("STORAGE_BACKEND", "local") or "local"  # local|s3
    storage_local_root: str = get_env("STORAGE_LOCAL_ROOT", "/app/uploads") or "/app/uploads"
    s3_bucket: Optional[str] = get_env("S3_BUCKET")
    s3_region: Optional[str] = get_env("S3_REGION")
    s3_endpoint: Optional[str] = get_env("S3_ENDPOINT")  # для S3-совместимых (MinIO, VK, Yandex)
    s3_access_key: Optional[str] = get_env("S3_ACCESS_KEY")
    s3_secret_key: Optional[str] = get_env("S3_SECRET_KEY")
    # VoIP providers (optional)
    voip_provider: str = get_env("VOIP_PROVIDER", "simulated") or "simulated"  # voximplant|zadarma|simulated
    voximplant_account: Optional[str] = get_env("VOXIMPLANT_ACCOUNT")
    voximplant_api_key: Optional[str] = get_env("VOXIMPLANT_API_KEY")
    zadarma_key: Optional[str] = get_env("ZADARMA_KEY")
    zadarma_secret: Optional[str] = get_env("ZADARMA_SECRET")
    # SMTP (manual emails)
    smtp_host: Optional[str] = get_env("SMTP_HOST")
    smtp_port: int = int(get_env("SMTP_PORT", "587") or "587")
    smtp_user: Optional[str] = get_env("SMTP_USER")
    smtp_password: Optional[str] = get_env("SMTP_PASSWORD")
    smtp_from: Optional[str] = get_env("SMTP_FROM")
    # Telegram Bot
    telegram_bot_token: Optional[str] = get_env("TELEGRAM_BOT_TOKEN")
    # Generic SMS HTTP sender (POST JSON: { to, text })
    sms_http_url: Optional[str] = get_env("SMS_HTTP_URL")
    sms_http_auth: Optional[str] = get_env("SMS_HTTP_AUTH")  # e.g., "Bearer xyz"
    # Escalation
    escalation_enabled: bool = (get_env("ESCALATION_ENABLED", "0") or "0") in {"1", "true", "yes", "on"}
    escalation_check_interval_sec: int = int(get_env("ESCALATION_CHECK_INTERVAL_SEC", "300") or "300")
    escalation_reminder_hours: int = int(get_env("ESCALATION_REMINDER_HOURS", "6") or "6")
    escalation_autocall_hours: int = int(get_env("ESCALATION_AUTOCALL_HOURS", "24") or "24")
    # Redis/Celery
    redis_url: str = get_env("REDIS_URL", "redis://redis:6379/0") or "redis://redis:6379/0"
    celery_broker_url: str = get_env("CELERY_BROKER_URL", redis_url) or redis_url
    celery_result_backend: str = get_env("CELERY_RESULT_BACKEND", redis_url) or redis_url


settings = Settings()


