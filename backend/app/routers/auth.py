from __future__ import annotations

import time
import hmac
import hashlib
import base64
from typing import Dict, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import settings


router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str

class VerifyRequest(BaseModel):
    token: str


def _sign(payload: str) -> str:
    mac = hmac.new(settings.auth_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


@router.post("/login")
def login(req: LoginRequest) -> Dict[str, Any]:
    if req.username != settings.admin_user or req.password != settings.admin_password:
        raise HTTPException(status_code=401, detail="invalid credentials")
    exp = int(time.time()) + 24 * 3600
    payload = f"u={req.username};exp={exp}"
    sig = _sign(payload)
    token = f"v1.{payload}.{sig}"
    return {"token": token, "exp": exp}


@router.post("/verify")
def verify(req: VerifyRequest) -> Dict[str, Any]:
    try:
        token = req.token
        if not token or not token.startswith("v1."):
            raise ValueError("bad token")
        _, payload, sig = token.split(".", 2)
        if _sign(payload) != sig:
            raise ValueError("bad signature")
        parts = dict(item.split("=", 1) for item in payload.split(";"))
        if int(parts.get("exp", "0")) < int(time.time()):
            raise ValueError("expired")
        return {"ok": True, "user": parts.get("u")}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=401, detail=str(e))


