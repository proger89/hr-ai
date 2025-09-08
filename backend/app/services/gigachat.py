from __future__ import annotations

import os
import time
from typing import Any, Dict, Tuple
from uuid import uuid4

import requests


_TOKEN_CACHE: Tuple[str, float] | None = None


def _verify_path() -> str | bool:
    path = "/app/ca/ru_bundle.pem"
    return path if os.path.exists(path) else True


def get_gigachat_access_token(scope: str = "GIGACHAT_API_PERS") -> Tuple[str, float]:
    global _TOKEN_CACHE
    if _TOKEN_CACHE and _TOKEN_CACHE[1] > time.time():
        return _TOKEN_CACHE

    auth_key = os.getenv("GIGACHAT_AUTH_KEY")
    if not auth_key:
        raise RuntimeError("GIGACHAT_AUTH_KEY is not set")

    oauth_url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    headers = {
        "Authorization": f"Basic {auth_key}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "RqUID": str(uuid4()),
    }
    resp = requests.post(
        oauth_url,
        headers=headers,
        data={"scope": scope},
        timeout=15,
        verify=_verify_path(),
    )
    resp.raise_for_status()
    data = resp.json()
    access_token = data["access_token"]
    expires_at = time.time() + float(data.get("expires_in", 1800)) - 60
    _TOKEN_CACHE = (access_token, expires_at)
    return _TOKEN_CACHE


def _base_url() -> str:
    return os.getenv("GIGACHAT_BASE_URL", "https://gigachat.devices.sberbank.ru/api/v1").rstrip("/")


def gc_post_json(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    token, _ = get_gigachat_access_token()
    url = f"{_base_url()}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=60, verify=_verify_path())
    resp.raise_for_status()
    return resp.json()


