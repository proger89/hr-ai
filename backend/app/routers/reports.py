from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Literal, Dict, Any
import os
import json
import time
import io
from ..services.storage import storage
from ..tasks import generate_report_task


router = APIRouter(prefix="/api/report", tags=["report"])


class ReportRequest(BaseModel):
    candidate_id: str | int
    vacancy_id: str | int
    format: Literal["pdf", "json", "csv"] = Field(default="pdf")


def _ensure_reports_dir() -> str:
    base = "/app/reports"
    os.makedirs(base, exist_ok=True)
    return base


@router.post("/generate")
def generate_report(req: ReportRequest) -> Dict[str, Any]:
    base = _ensure_reports_dir()
    report_id = f"{int(time.time())}-{req.candidate_id}-{req.vacancy_id}"
    s3_url: str | None = None
    if req.format == "json":
        filename = f"{report_id}.json"
        path = os.path.join(base, filename)
        payload = {
            "candidate_id": req.candidate_id,
            "vacancy_id": req.vacancy_id,
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
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        with open(path, "wb") as f:
            f.write(data)
        _, s3_url = storage.save_bytes(data, "reports", filename)
    elif req.format == "csv":
        filename = f"{report_id}.csv"
        path = os.path.join(base, filename)
        # Простой CSV с ключевыми метриками
        import csv  # noqa: WPS433
        rows = [
            ["candidate_id", "vacancy_id", "tech", "comm", "cases", "total", "decision", "match_pct"],
            [req.candidate_id, req.vacancy_id, 0.76, 0.68, 0.78, 0.74, "pending", 0.78],
        ]
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerows(rows)
        data = buf.getvalue().encode("utf-8")
        with open(path, "wb") as f:
            f.write(data)
        _, s3_url = storage.save_bytes(data, "reports", filename)
    else:
        # Генерация реального PDF (reportlab)
        try:
            from reportlab.lib.pagesizes import A4  # type: ignore
            from reportlab.pdfgen import canvas  # type: ignore
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"reportlab not available: {e}")
        filename = f"{report_id}.pdf"
        path = os.path.join(base, filename)
        mem = io.BytesIO()
        c = canvas.Canvas(mem, pagesize=A4)
        width, height = A4
        c.setTitle(f"Interview Report #{report_id}")
        y = height - 50
        def line(txt: str, dy: int = 20):
            nonlocal y
            c.drawString(40, y, txt)
            y -= dy
        line(f"Interview Report: {report_id}")
        line(f"Candidate ID: {req.candidate_id}")
        line(f"Vacancy ID: {req.vacancy_id}")
        line("Summary:")
        line(" - Decision: pending")
        line(" - Match: 78%")
        line("Scores:")
        line(" - Tech: 0.76, Comm: 0.68, Cases: 0.78, Total: 0.74")
        line("Strengths: Python, AsyncIO, System thinking")
        line("Gaps: Kubernetes, ML Ops, Go")
        c.showPage()
        c.save()
        data = mem.getvalue()
        with open(path, "wb") as f:
            f.write(data)
        _, s3_url = storage.save_bytes(data, "reports", filename)
    resp: Dict[str, Any] = {"url": f"/api/report/{report_id}"}
    if s3_url:
        resp["s3_url"] = s3_url
    return resp


class AsyncReportRequest(BaseModel):
    candidate_id: str | int
    vacancy_id: str | int
    format: Literal["pdf", "json", "csv"] = Field(default="pdf")


@router.post("/generate_async")
def generate_report_async(req: AsyncReportRequest) -> Dict[str, Any]:
    task = generate_report_task.delay(req.candidate_id, req.vacancy_id, req.format)
    return {"task_id": task.id, "status": "queued"}


@router.get("/{report_id}")
def download_report(report_id: str):
    base = _ensure_reports_dir()
    pdf_path = os.path.join(base, f"{report_id}.pdf")
    json_path = os.path.join(base, f"{report_id}.json")
    csv_path = os.path.join(base, f"{report_id}.csv")
    if os.path.exists(pdf_path):
        f = open(pdf_path, "rb")
        return StreamingResponse(f, media_type="application/pdf")
    if os.path.exists(json_path):
        f = open(json_path, "rb")
        return StreamingResponse(f, media_type="application/json")
    if os.path.exists(csv_path):
        f = open(csv_path, "rb")
        return StreamingResponse(f, media_type="text/csv")
    # Если локально нет — пытаемся подтянуть из S3, если включен s3 backend
    # Пробуем все расширения по очереди
    for ext, mime in (("pdf", "application/pdf"), ("json", "application/json"), ("csv", "text/csv")):
        data = storage.load_bytes("reports", f"{report_id}.{ext}")
        if data:
            return StreamingResponse(io.BytesIO(data), media_type=mime)
    raise HTTPException(status_code=404, detail="report not found")


# Simple finish endpoint to be called by frontend tool
@router.post("/finish")
def finish_interview(interview_id: str | int) -> Dict[str, Any]:
    # Placeholder: hook scoring/summary generation here if needed
    # For now, just return redirect URL
    return {"status": "ok", "redirect_url": f"/complete.html?id={interview_id}"}


