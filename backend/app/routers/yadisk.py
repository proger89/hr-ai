from fastapi import APIRouter, HTTPException
from ..config import settings
import requests

router = APIRouter(prefix="/api/yadisk", tags=["yadisk"])


@router.post("/upload")
def upload_to_disk(path: str, content: bytes):
    if not settings.yadisk_oauth:
        raise HTTPException(status_code=500, detail="YADISK_OAUTH not set")
    base = "https://cloud-api.yandex.net/v1/disk"
    r = requests.get(
        f"{base}/resources/upload",
        params={"path": path, "overwrite": "true"},
        headers={"Authorization": f"OAuth {settings.yadisk_oauth}"},
        timeout=15,
    )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    href = r.json()["href"]
    up = requests.put(href, data=content, timeout=60)
    if up.status_code not in (200,201,202):
        raise HTTPException(status_code=up.status_code, detail=up.text)
    return {"ok": True, "path": path}


