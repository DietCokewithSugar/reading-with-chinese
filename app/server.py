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

import hashlib
import hmac
import json
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import translator

# --------------------------------------------------------------------------- #
# Local on-disk storage (no database). The job directory IS the source of
# truth: results can always be reconstructed from disk, so they survive page
# reloads and server restarts. Point RWC_DATA_DIR at a persistent disk (e.g. a
# Render Disk) to also survive redeploys.
# --------------------------------------------------------------------------- #
_default_data = Path(__file__).resolve().parent.parent / "data" / "jobs"
WORK_DIR = Path(os.getenv("RWC_DATA_DIR", str(_default_data)))
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
        # Clamp for display: some engines (BabelDOC) report a multi-stage
        # overall progress that can momentarily exceed 100%.
        done = min(self.pages_done, self.pages_total) if self.pages_total else self.pages_done
        pct = 0
        if self.pages_total:
            pct = min(100, round(100 * self.pages_done / self.pages_total))
        return {
            "id": self.id,
            "filename": self.filename,
            "status": self.status,
            "pages_done": done,
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


def _write_meta(job: Job, engine: str) -> None:
    """Persist a tiny sidecar so a job can be reconstructed from disk later.

    This is not a database -- just enough metadata (original filename, engine)
    to render nice download names and restore results after a reload/restart.
    """
    try:
        (job.dir / "meta.json").write_text(
            json.dumps({"filename": job.filename, "engine": engine,
                        "created_at": job.created_at}),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 -- metadata is best-effort
        pass


def _load_job_from_disk(job_id: str) -> Optional[Job]:
    """Rebuild a Job from its on-disk directory when it is not in memory."""
    if not job_id or "/" in job_id or "\\" in job_id:
        return None
    jdir = WORK_DIR / job_id
    if not jdir.is_dir():
        return None

    meta = {}
    try:
        meta = json.loads((jdir / "meta.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass

    job = Job(id=job_id, filename=meta.get("filename") or "document.pdf")
    if "created_at" in meta:
        job.created_at = meta["created_at"]

    mono = jdir / "translated.pdf"
    dual = jdir / "bilingual.pdf"
    if (mono.exists() and mono.stat().st_size > 0) or (dual.exists() and dual.stat().st_size > 0):
        job.status = "done"
        job.pages_done = job.pages_total = 1  # report 100% for restored results
    elif (jdir / "source.pdf").exists():
        # We have the upload but no result and no live worker -> it was
        # interrupted (e.g. the server restarted mid-translation).
        job.status = "error"
        job.error = "任务被中断（可能因服务重启），请重新翻译。"
    else:
        return None
    return job


def _get_job(job_id: str) -> Optional[Job]:
    """Return a live in-memory job, or reconstruct it from disk."""
    job = JOBS.get(job_id)
    if job is not None:
        return job
    return _load_job_from_disk(job_id)


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

# --------------------------------------------------------------------------- #
# Optional site-wide access password.
# --------------------------------------------------------------------------- #
# Set RWC_ACCESS_PASSWORD in the environment (e.g. Render dashboard) to require
# a password before anyone can use the site. Leave it unset to keep the site
# open (useful for local development). The password itself is never stored in
# the repo or sent to the client; only a derived cookie token is used.
ACCESS_PASSWORD = os.getenv("RWC_ACCESS_PASSWORD", "").strip()
_AUTH_COOKIE = "rwc_auth"
_OPEN_PATHS = {"/login", "/api/login", "/api/health", "/favicon.ico"}


def _expected_token() -> str:
    return hashlib.sha256(("rwc::" + ACCESS_PASSWORD).encode()).hexdigest()


def _is_authed(request) -> bool:
    if not ACCESS_PASSWORD:
        return True
    token = request.cookies.get(_AUTH_COOKIE, "")
    return bool(token) and hmac.compare_digest(token, _expected_token())


@app.middleware("http")
async def access_gate(request, call_next):
    if not ACCESS_PASSWORD or request.url.path in _OPEN_PATHS or _is_authed(request):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "需要登录"}, status_code=401)
    return RedirectResponse("/login")


_LOGIN_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录 · Reading with Chinese</title>
<style>
  html,body{height:100%;margin:0;background:#0f1420;color:#e7ecf5;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;}
  .wrap{height:100%;display:grid;place-items:center;}
  .card{background:#171e2e;border:1px solid #2a3350;border-radius:14px;padding:34px 30px;width:320px;
    box-shadow:0 20px 60px rgba(0,0,0,.45);}
  .logo{width:46px;height:46px;display:grid;place-items:center;border-radius:12px;font-weight:700;font-size:22px;
    color:#fff;background:linear-gradient(135deg,#4f7cff,#8a5bff);margin:0 auto 16px;}
  h1{font-size:18px;text-align:center;margin:0 0 4px;}
  p{color:#8b95ad;font-size:13px;text-align:center;margin:0 0 22px;}
  input{width:100%;box-sizing:border-box;background:#1f2740;border:1px solid #2a3350;color:#e7ecf5;
    border-radius:9px;padding:11px 12px;font-size:15px;outline:none;}
  input:focus{border-color:#4f7cff;}
  button{width:100%;margin-top:14px;border:none;border-radius:10px;padding:12px;font-size:15px;font-weight:600;
    color:#fff;cursor:pointer;background:linear-gradient(135deg,#4f7cff,#6f8bff);}
  .err{color:#ff5d6c;font-size:13px;text-align:center;min-height:18px;margin-top:12px;}
</style></head><body><div class="wrap"><form class="card" id="f">
  <div class="logo">译</div>
  <h1>Reading with Chinese</h1>
  <p>请输入访问口令</p>
  <input id="pw" type="password" placeholder="访问口令" autofocus autocomplete="current-password">
  <button type="submit">进入</button>
  <div class="err" id="err"></div>
</form></div>
<script>
  const f=document.getElementById('f');
  f.addEventListener('submit',async(e)=>{e.preventDefault();
    const err=document.getElementById('err');err.textContent='';
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:'password='+encodeURIComponent(document.getElementById('pw').value)});
    if(r.ok){location.href='/';}else{err.textContent='口令错误，请重试';}
  });
</script></body></html>"""


@app.get("/login")
def login_page() -> HTMLResponse:
    return HTMLResponse(_LOGIN_HTML)


@app.post("/api/login")
def login(password: str = Form(...)) -> JSONResponse:
    if not ACCESS_PASSWORD:
        return JSONResponse({"ok": True})  # gate disabled
    if not hmac.compare_digest(password.strip(), ACCESS_PASSWORD):
        raise HTTPException(status_code=401, detail="口令错误")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        _AUTH_COOKIE,
        _expected_token(),
        max_age=30 * 24 * 3600,
        httponly=True,
        samesite="lax",
    )
    return resp


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
    _write_meta(job, params["engine"])

    threading.Thread(target=_run_job, args=(job, params), daemon=True).start()
    return JSONResponse({"job_id": job.id})


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    job = JOBS.get(job_id)  # only live jobs can be cancelled
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.cancel_event.set()
    return {"ok": True}


_KINDS = {"mono": "translated.pdf", "dual": "bilingual.pdf"}


@app.get("/api/jobs/{job_id}/file/{kind}")
def job_file(job_id: str, kind: str, download: int = 0):
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if kind not in _KINDS:
        raise HTTPException(status_code=400, detail="Unknown file kind")
    path = job.dir / _KINDS[kind]
    if not path.exists() or path.stat().st_size == 0:
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
