from __future__ import annotations

import hmac
import hashlib
import base64
import json
import time
from typing import Dict, Any, Tuple


def hash_password(password: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), password.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_password(password: str, secret: str, stored_hash: str) -> bool:
    calc = hash_password(password, secret)
    return hmac.compare_digest(calc, stored_hash)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_json(obj: Dict[str, Any]) -> str:
    return _b64url(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def sign_jwt_like(claims: Dict[str, Any], secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    h = _b64url_json(header)
    p = _b64url_json(claims)
    data = f"{h}.{p}".encode("utf-8")
    sig = _b64url(hmac.new(secret.encode("utf-8"), data, hashlib.sha256).digest())
    return f"{h}.{p}.{sig}"


def verify_jwt_like(token: str, secret: str) -> Tuple[Dict[str, Any], bool]:
    try:
        h, p, s = token.split(".")
        data = f"{h}.{p}".encode("utf-8")
        exp_sig = _b64url(hmac.new(secret.encode("utf-8"), data, hashlib.sha256).digest())
        if not hmac.compare_digest(exp_sig, s):
            return {}, False
        pad = lambda x: x + "=" * ((4 - len(x) % 4) % 4)
        claims = json.loads(base64.urlsafe_b64decode(pad(p)).decode("utf-8"))
        if int(claims.get("exp", 0)) < int(time.time()):
            return claims, False
        return claims, True
    except Exception:
        return {}, False


