from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
import smtplib
from email.message import EmailMessage
from ..config import settings
import requests


router = APIRouter(prefix="/api/notify", tags=["notify"])


class EmailRequest(BaseModel):
    to: EmailStr
    subject: str
    text: str


@router.post("/email")
def send_email(body: EmailRequest):
    if not settings.smtp_host or not settings.smtp_from:
        raise HTTPException(status_code=500, detail="SMTP not configured")
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = body.to
    msg["Subject"] = body.subject
    msg.set_content(body.text)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as s:
            s.starttls()
            if settings.smtp_user and settings.smtp_password:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


class SmsRequest(BaseModel):
    to: str
    text: str


@router.post("/sms")
def send_sms(body: SmsRequest):
    if not settings.sms_http_url:
        raise HTTPException(status_code=500, detail="SMS not configured")
    try:
        headers = {"Content-Type": "application/json"}
        if settings.sms_http_auth:
            auth_name, _, auth_val = settings.sms_http_auth.partition(" ")
            headers["Authorization"] = f"{auth_name or 'Bearer'} {auth_val or settings.sms_http_auth}"
        r = requests.post(settings.sms_http_url, json={"to": body.to, "text": body.text}, headers=headers, timeout=10)
        if r.status_code >= 300:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


class TelegramRequest(BaseModel):
    chat_id: str
    text: str


@router.post("/telegram")
def send_telegram(body: TelegramRequest):
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=500, detail="Telegram not configured")
    try:
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        r = requests.post(url, json={"chat_id": body.chat_id, "text": body.text}, timeout=10)
        if r.status_code >= 300:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


