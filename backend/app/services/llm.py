from __future__ import annotations

import json
import os
import re
from typing import Dict, Any, Optional

import requests

# Prefer OpenAI when credentials are present
from .openai_service import chat_completion  # type: ignore


def _ollama_base() -> str:
    return os.getenv("OLLAMA_HOST", "http://ollama:11434").rstrip("/")


def _ollama_model() -> str:
    # Default lightweight SOTA small model for local inference
    return os.getenv("OLLAMA_MODEL", "qwen2.5:3b-instruct-q4_K_M")


def _extract_json(text: str) -> Dict[str, Any]:
    try:
        # Prefer first top-level JSON object
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return {}
        return json.loads(m.group(0))
    except Exception:
        return {}


def ensure_model() -> bool:
    """Ensure model is available locally. Pull if absent. Returns True if ready."""
    name = _ollama_model()
    base = _ollama_base()
    try:
        r = requests.post(base + "/api/show", json={"name": name}, timeout=10)
        if r.ok:
            return True
    except Exception:
        pass
    # Try to pull (streaming)
    try:
        with requests.post(base + "/api/pull", json={"name": name}, stream=True, timeout=600) as resp:
            if not resp.ok:
                return False
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    j = json.loads(line)
                    if (j.get("status") or "").lower() == "success":
                        return True
                except Exception:
                    continue
    except Exception:
        return False
    return False


def generate_scenario_with_llm(jd_text: str, lang: str | None = "ru") -> Optional[Dict[str, str]]:
    """Generate scenario via OpenAI if available, otherwise fallback to local Ollama.
    Returns dict with keys: intro, experience, stack, cases, communication, final
    """
    language = (lang or "ru").lower()
    system = (
        "Ты помощник HR-интервьюера. Сгенерируй структурированный сценарий интервью для кандидата на основе описания вакансии."
        if language.startswith("ru")
        else "You are an HR interviewer assistant. Generate a structured interview scenario based on the job description."
    )
    user = (
        "Выведи строго JSON со строковыми полями: intro, experience, stack, cases, communication, final.\n"
        "Пиши на русском. Описание вакансии ниже между ---:\n---\n" + (jd_text or "") + "\n---\n"
        if language.startswith("ru")
        else "Return STRICT JSON with string fields: intro, experience, stack, cases, communication, final.\n"
             "Write in English. Job description is below between ---:\n---\n" + (jd_text or "") + "\n---\n"
    )
    # 1) Try OpenAI
    try:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        data = chat_completion(messages)
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        parsed = _extract_json(text)
        out = {"intro":"","experience":"","stack":"","cases":"","communication":"","final":""}
        if isinstance(parsed, dict):
            out.update({k: str(parsed.get(k, "") or "") for k in out.keys()})
        if any((out.get(k) or "").strip() for k in out.keys()):
            return out
    except Exception:
        pass

    # 2) Fallback to local Ollama
    payload_ol = {
        "model": _ollama_model(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": 0.2},
    }
    try:
        ensure_model()
        resp = requests.post(_ollama_base() + "/api/chat", json=payload_ol, timeout=120)
        if not resp.ok:
            return None
        data = resp.json()
        content = ((data or {}).get("message") or {}).get("content", "")
        parsed = _extract_json(content)
        out = {"intro":"","experience":"","stack":"","cases":"","communication":"","final":""}
        if isinstance(parsed, dict):
            out.update({k: str(parsed.get(k, "") or "") for k in out.keys()})
        if any((out.get(k) or "").strip() for k in out.keys()):
            return out
        return None
    except Exception:
        return None


