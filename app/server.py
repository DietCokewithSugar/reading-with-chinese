"""FastAPI server for the Reading-with-Chinese PDF translation web app.

Design goals (from the product brief):
  * Open the page and use it immediately -- no login, no database.
  * The DeepSeek API key lives only in the browser (localStorage) and is sent
    per-request; the server never persists it.
  * Uploaded and translated PDFs are kept only as local files on disk, grouped
    by job id, and cleaned up over time.
  * No upload size limit -- long PDFs are split, translated concurrently and
    merged back together by the translation engine.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, Form, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import translator

# --------------------------------------------------------------------------- #
# Local on-disk storage (no database).
# --------------------------------------------------------------------------- #
WORK_DIR = Path(__file__).resolve().parent.parent / "data" / "jobs"
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Jobs older than this are garbage-collected from disk.
JOB_TTL_SECONDS = 6 * 60 * 60

# Memory guards. The pdf2zh stack (onnxruntime + the layout model + per-page
# image rendering) is memory-hungry, and every concurrent chunk worker holds a
# copy of that working set. On small instances (e.g. Render's 512 MB tiers)
# unbounded concurrency causes OOM restarts (HTTP 502). These caps clamp
# whatever the browser requests so a small box stays within its RAM budget;
# raise them via env vars on larger instances.
MAX_CONCURRENCY = max(1, int(os.getenv("RWC_MAX_CONCURRENCY", "2")))
MAX_THREAD = max(1, int(os.getenv("RWC_MAX_THREAD", "4")))
# Cap how many translation jobs run at once across all users on this instance.
MAX_ACTIVE_JOBS = max(1, int(os.getenv("RWC_MAX_ACTIVE_JOBS", "1")))


@dataclass
class Job:
    id: str
    filename: str
    status: str = "pending"  # pending | running | done | error | cancelled
    pages_done: int = 0
    pages_total: int = 0
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    @property
    def dir(self) -> Path:
        return WORK_DIR / self.id

    def to_dict(self) -> dict:
        pct = 0
        if self.pages_total:
            pct = round(100 * self.pages_done / self.pages_total)
        return {
            "id": self.id,
            "filename": self.filename,
            "status": self.status,
            "pages_done": self.pages_done,
            "pages_total": self.pages_total,
            "percent": pct,
            "error": self.error,
            "has_mono": (self.dir / "translated.pdf").exists(),
            "has_dual": (self.dir / "bilingual.pdf").exists(),
        }


JOBS: Dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def _gc_jobs() -> None:
    """Remove jobs (memory + disk) that have outlived their TTL."""
    now = time.time()
    with JOBS_LOCK:
        stale = [j for j in JOBS.values() if now - j.created_at > JOB_TTL_SECONDS]
        for job in stale:
            JOBS.pop(job.id, None)
            shutil.rmtree(job.dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Background worker.
# --------------------------------------------------------------------------- #
def _run_job(job: Job, params: dict) -> None:
    job.status = "running"
    try:
        source = (job.dir / "source.pdf").read_bytes()
        job.pages_total = translator.page_count(source)

        def progress(done: int, total: int) -> None:
            job.pages_done = done
            if total:
                job.pages_total = total

        if params["engine"] == "babeldoc":
            mono, dual = translator.translate_pdf_babeldoc(
                source,
                api_key=params["api_key"],
                model_name=params["model_name"],
                lang_in=params["lang_in"],
                lang_out=params["lang_out"],
                concurrency=params["concurrency"],
                progress_cb=progress,
                cancel_event=job.cancel_event,
            )
        else:
            mono, dual = translator.translate_pdf(
                source,
                api_key=params["api_key"],
                model_name=params["model_name"],
                lang_in=params["lang_in"],
                lang_out=params["lang_out"],
                chunk_size=params["chunk_size"],
                concurrency=params["concurrency"],
                thread=params["thread"],
                translate_figures=params["translate_figures"],
                progress_cb=progress,
                cancel_event=job.cancel_event,
            )
        if job.cancel_event.is_set():
            job.status = "cancelled"
            return
        (job.dir / "translated.pdf").write_bytes(mono)
        (job.dir / "bilingual.pdf").write_bytes(dual)
        job.pages_done = job.pages_total
        job.status = "done"
    except Exception as exc:  # noqa: BLE001 -- surface any failure to the client
        job.status = "error"
        job.error = str(exc) or exc.__class__.__name__
        traceback.print_exc()


# --------------------------------------------------------------------------- #
# App + routes.
# --------------------------------------------------------------------------- #
app = FastAPI(title="Reading with Chinese", version="1.0.0")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "model_ready": translator.model_ready()}


@app.post("/api/translate")
async def create_translation(
    file: UploadFile = File(...),
    api_key: str = Form(...),
    model_name: str = Form("deepseek-chat"),
    lang_in: str = Form("en"),
    lang_out: str = Form("zh"),
    chunk_size: int = Form(8),
    concurrency: int = Form(6),
    thread: int = Form(4),
    engine: str = Form("pdf2zh"),
    translate_figures: bool = Form(False),
) -> JSONResponse:
    if not api_key.strip():
        raise HTTPException(status_code=400, detail="DeepSeek API key is required")

    _gc_jobs()

    # Reject new work if the instance is already at its concurrent-job limit,
    # rather than piling on more memory pressure and triggering an OOM 502.
    with JOBS_LOCK:
        active = sum(1 for j in JOBS.values() if j.status in ("pending", "running"))
    if active >= MAX_ACTIVE_JOBS:
        raise HTTPException(
            status_code=429,
            detail="服务器正忙（已有翻译任务在进行），请稍后再试。",
        )

    job = Job(id=uuid.uuid4().hex, filename=file.filename or "document.pdf")
    job.dir.mkdir(parents=True, exist_ok=True)

    # Stream the upload to disk in chunks so there is no in-memory size limit.
    source_path = job.dir / "source.pdf"
    with source_path.open("wb") as out:
        while True:
            chunk = await file.read(1 << 20)  # 1 MiB
            if not chunk:
                break
            out.write(chunk)

    if source_path.stat().st_size == 0:
        shutil.rmtree(job.dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    params = {
        "api_key": api_key.strip(),
        "model_name": model_name.strip() or "deepseek-chat",
        "lang_in": lang_in.strip() or "en",
        "lang_out": lang_out.strip() or "zh",
        "chunk_size": max(1, int(chunk_size)),
        # Clamp to the instance memory budget regardless of what the UI sent.
        "concurrency": min(max(1, int(concurrency)), MAX_CONCURRENCY),
        "thread": min(max(1, int(thread)), MAX_THREAD),
        "engine": "babeldoc" if str(engine).lower() == "babeldoc" else "pdf2zh",
        "translate_figures": bool(translate_figures),
    }

    with JOBS_LOCK:
        JOBS[job.id] = job

    threading.Thread(target=_run_job, args=(job, params), daemon=True).start()
    return JSONResponse({"job_id": job.id})


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.cancel_event.set()
    return {"ok": True}


_KINDS = {"mono": "translated.pdf", "dual": "bilingual.pdf"}


@app.get("/api/jobs/{job_id}/file/{kind}")
def job_file(job_id: str, kind: str, download: int = 0):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if kind not in _KINDS:
        raise HTTPException(status_code=400, detail="Unknown file kind")
    path = job.dir / _KINDS[kind]
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not ready")

    stem = Path(job.filename).stem
    suffix = "zh" if kind == "mono" else "bilingual"
    nice_name = f"{stem}-{suffix}.pdf"
    disposition = "attachment" if download else "inline"
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{nice_name}"'},
    )


# Serve the single-page frontend at "/". Mounted last so it doesn't shadow /api.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
