"""AudioDrop — FastAPI entrypoint.

Convierte un video de YouTube en MP3 y lo entrega vía descarga directa.
Mantén el archivo simple: este módulo sólo expone HTTP y delega el trabajo
al módulo `audio_service`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from audio_service import AudioService, ConversionError, JobNotFound

LOG_LEVEL = os.environ.get("AUDIODROP_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
)
log = logging.getLogger("audiodrop")

BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = Path(os.environ.get("AUDIODROP_WORK_DIR", "/tmp/audiodrop"))
MAX_DURATION = int(os.environ.get("AUDIODROP_MAX_DURATION", "1800"))  # 30 min

WORK_DIR.mkdir(parents=True, exist_ok=True)

service = AudioService(work_dir=WORK_DIR, max_duration=MAX_DURATION)

app = FastAPI(title="AudioDrop", version="1.0.0", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


YOUTUBE_RE = re.compile(
    r"^(https?://)?((www|m|music)\.)?(youtube\.com/(watch\?v=|shorts/|embed/)|youtu\.be/)[\w\-]{6,}",
    re.IGNORECASE,
)


def _is_valid_youtube_url(url: str) -> bool:
    if not url or len(url) > 512:
        return False
    return bool(YOUTUBE_RE.match(url.strip()))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "version": app.version}


_ADMIN_LAN_IP = os.environ.get("AUDIODROP_ADMIN_IP", "192.168.68.83")


@app.get("/api/admin-eligible")
async def admin_eligible(request: Request) -> dict:
    """Sólo true si la petición llegó directo por LAN (no via Cloudflare) desde la IP del admin.

    Mecánica de seguridad:
      1) `cf-ray` o `cf-connecting-ip` presentes → viene por Cloudflare → no admin.
      2) La IP claim es la primera del `X-Forwarded-For` (que pone Apache local). Si no hay
         XFF, usamos la del socket. Sólo coincide con `_ADMIN_LAN_IP` cuando el cliente está
         realmente en la LAN y resuelve via DNS local (split-horizon).
    """
    cf_headers = request.headers.get("cf-ray") or request.headers.get("cf-connecting-ip")
    if cf_headers:
        return {"eligible": False}
    xff = (request.headers.get("x-forwarded-for") or "").split(",")
    client_ip = xff[0].strip() if xff and xff[0].strip() else (request.client.host if request.client else "")
    return {"eligible": client_ip == _ADMIN_LAN_IP}


@app.post("/api/metadata")
async def metadata(payload: dict) -> JSONResponse:
    url = (payload or {}).get("url", "").strip()
    if not _is_valid_youtube_url(url):
        raise HTTPException(status_code=400, detail="URL inválida")
    try:
        info = await service.fetch_metadata(url)
    except ConversionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(info)


@app.post("/api/convert")
async def convert(payload: dict) -> JSONResponse:
    url = (payload or {}).get("url", "").strip()
    format_key = (payload or {}).get("format", "mp3-192").strip()
    if not _is_valid_youtube_url(url):
        raise HTTPException(status_code=400, detail="URL inválida")
    if not re.fullmatch(r"(mp3-(128|192|320)|video-(360p|480p|720p|720p60|1080p|1080p60|1440p|1440p60|2160p|2160p60))", format_key):
        raise HTTPException(status_code=400, detail="Formato no soportado")
    try:
        job_id = await service.start_job(url, format_key=format_key)
    except ConversionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"job_id": job_id})


@app.get("/api/progress/{job_id}")
async def progress(job_id: str) -> StreamingResponse:
    if not re.fullmatch(r"[a-f0-9\-]{8,40}", job_id):
        raise HTTPException(status_code=400, detail="ID inválido")
    try:
        queue = service.subscribe(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def event_stream():
        try:
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("status") in ("done", "error"):
                    break
        except asyncio.CancelledError:
            log.info("SSE cliente desconectado job=%s", job_id)
            raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/download/{job_id}")
async def download(job_id: str) -> FileResponse:
    if not re.fullmatch(r"[a-f0-9\-]{8,40}", job_id):
        raise HTTPException(status_code=400, detail="ID inválido")
    try:
        path, filename, mime = service.file_for(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=410, detail="Archivo expirado") from exc

    background = service.schedule_cleanup(job_id)
    return FileResponse(
        path=path,
        filename=filename,
        media_type=mime,
        background=background,
    )


@app.on_event("startup")
async def on_startup() -> None:
    asyncio.create_task(service.janitor_loop())
    log.info("AudioDrop listo — work_dir=%s max_duration=%ss", WORK_DIR, MAX_DURATION)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await service.shutdown()
