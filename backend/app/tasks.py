from __future__ import annotations

import json
import os
import time
from typing import Literal

from .celery_app import celery_app


def _ensure_reports_dir() -> str:
    base = "/app/reports"
    os.makedirs(base, exist_ok=True)
    return base


@celery_app.task(name="generate_report_task")
def generate_report_task(candidate_id: str | int, vacancy_id: str | int, fmt: Literal["pdf", "json", "csv"] = "pdf") -> dict:
    # Импорт локальный, чтобы воркер не требовал reportlab когда формат не pdf
    base = _ensure_reports_dir()
    report_id = f"{int(time.time())}-{candidate_id}-{vacancy_id}"
    if fmt == "json":
        path = os.path.join(base, f"{report_id}.json")
        payload = {
            "candidate_id": candidate_id,
            "vacancy_id": vacancy_id,
            "summary": {
                "match_pct": 0.78,
                "decision": "pending",
                "strengths": ["Python", "AsyncIO", "Системное мышление"],
                "gaps": ["Kubernetes", "ML Ops", "Go"],
            },
            "scores": {"tech": 0.76, "comm": 0.68, "cases": 0.78, "total": 0.74},
            "quotes": [
                {"t0": 12.4, "t1": 18.7, "text": "Снизили p95 на 35%", "url": "/media/a1.ogg"}
            ],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    elif fmt == "csv":
        path = os.path.join(base, f"{report_id}.csv")
        import csv  # noqa: WPS433

        rows = [
            ["candidate_id", "vacancy_id", "tech", "comm", "cases", "total", "decision", "match_pct"],
            [candidate_id, vacancy_id, 0.76, 0.68, 0.78, 0.74, "pending", 0.78],
        ]
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerows(rows)
    else:
        try:
            from reportlab.lib.pagesizes import A4  # type: ignore
            from reportlab.pdfgen import canvas  # type: ignore
        except Exception as e:  # noqa: BLE001
            return {"error": f"reportlab not available: {e}"}
        path = os.path.join(base, f"{report_id}.pdf")
        c = canvas.Canvas(path, pagesize=A4)
        width, height = A4
        c.setTitle(f"Interview Report #{report_id}")
        y = height - 50

        def line(txt: str, dy: int = 20):
            nonlocal y
            c.drawString(40, y, txt)
            y -= dy

        line(f"Interview Report: {report_id}")
        line(f"Candidate ID: {candidate_id}")
        line(f"Vacancy ID: {vacancy_id}")
        line("Summary:")
        line(" - Decision: pending")
        line(" - Match: 78%")
        line("Scores:")
        line(" - Tech: 0.76, Comm: 0.68, Cases: 0.78, Total: 0.74")
        line("Strengths: Python, AsyncIO, System thinking")
        line("Gaps: Kubernetes, ML Ops, Go")
        c.showPage()
        c.save()

    return {"report_id": report_id, "url": f"/api/report/{report_id}"}


