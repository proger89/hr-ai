import time
import requests
from typing import Tuple
from uuid import uuid4
from ..config import settings

# Смартспич может отдавать токен через общий шлюз NGW; оставим smartspeech как дефолт
OAUTH_URLS = [
    "https://smartspeech.sber.ru/rest/v1/oauth",  # Актуальный endpoint
    "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",  # Альтернативный
]


def get_salutespeech_access_token() -> Tuple[str, float]:
    if not settings.smartspeech_auth_key:
        raise RuntimeError("SBER_SMARTSPEECH_AUTH_KEY is not set")
    last_err = None
    resp = None
    for url in OAUTH_URLS:
        try:
            # допускаем оба формата тела; некоторые окружения требуют отключить строгую валидацию TLS
            headers = {
                "Authorization": f"Basic {settings.smartspeech_auth_key}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "RqUID": str(uuid4()),
            }
            resp = requests.post(
                url,
                headers=headers,
                data={"scope": "SALUTE_SPEECH_PERS"},
                timeout=10,
                verify=False,
            )
            if resp.status_code == 200:
                break
            last_err = RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    if not resp or resp.status_code != 200:
        raise RuntimeError(str(last_err) if last_err else "OAuth failed")
    resp.raise_for_status()
    data = resp.json()
    expires_at = time.time() + float(data.get("expires_in", 1800)) - 60
    return data["access_token"], expires_at


