"""AudioService — descarga + conversión MP3 con yt-dlp y ffmpeg.

Toda la lógica que toca yt-dlp o el sistema de archivos vive aquí.
La capa HTTP (main.py) no debería tocar nada de esto directamente.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yt_dlp
from starlette.background import BackgroundTask

log = logging.getLogger("audiodrop.service")

JOB_TTL_SECONDS = 60 * 60          # 1 hora antes de borrar el MP3
JANITOR_INTERVAL_SECONDS = 60 * 5  # 5 min entre pases del recolector


class ConversionError(Exception):
    """Error de negocio: URL no válida, video privado, demasiado largo, etc."""


class JobNotFound(Exception):
    """El job_id no existe o ya fue limpiado."""


@dataclass
class Job:
    job_id: str
    url: str
    title: str = ""
    thumbnail: str = ""
    duration: int = 0
    status: str = "queued"
    progress: float = 0.0
    message: str = ""
    file_path: Optional[Path] = None
    filename: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    cleaned: bool = False


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^\w\-. ]+", "_", name, flags=re.UNICODE)
    name = name.strip(" ._") or "audio"
    return name[:120]


class AudioService:
    def __init__(self, work_dir: Path, max_duration: int) -> None:
        self.work_dir = work_dir
        self.max_duration = max_duration
        self.jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._shutdown = False

    # ---------------------------------------------------------------- metadata

    async def fetch_metadata(self, url: str) -> dict:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, self._extract_info, url)
        duration = info.get("duration") or 0
        if duration and duration > self.max_duration:
            raise ConversionError(
                f"Video demasiado largo ({duration//60} min). Máximo {self.max_duration//60} min."
            )
        return {
            "title": info.get("title") or "Audio",
            "thumbnail": info.get("thumbnail") or "",
            "duration": duration,
            "uploader": info.get("uploader") or "",
        }

    def _extract_info(self, url: str) -> dict:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": False}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False) or {}
        except yt_dlp.utils.DownloadError as exc:
            msg = str(exc).lower()
            if "private" in msg:
                raise ConversionError("El video es privado.") from exc
            if "unavailable" in msg or "not available" in msg:
                raise ConversionError("El video no está disponible.") from exc
            raise ConversionError("No se pudo leer el video.") from exc
        except Exception as exc:  # noqa: BLE001
            log.exception("Error inesperado leyendo metadatos url=%s", url)
            raise ConversionError("No se pudo procesar la URL.") from exc

    # ---------------------------------------------------------------- jobs

    async def start_job(self, url: str) -> str:
        meta = await self.fetch_metadata(url)
        self._loop = asyncio.get_running_loop()
        job_id = uuid.uuid4().hex
        job = Job(
            job_id=job_id,
            url=url,
            title=meta["title"],
            thumbnail=meta["thumbnail"],
            duration=meta["duration"],
        )
        async with self._lock:
            self.jobs[job_id] = job
        asyncio.create_task(self._run_job(job))
        return job_id

    def subscribe(self, job_id: str) -> asyncio.Queue:
        job = self.jobs.get(job_id)
        if job is None:
            raise JobNotFound(f"Job {job_id} no existe")
        q: asyncio.Queue = asyncio.Queue()
        job.subscribers.append(q)
        q.put_nowait(self._snapshot(job))
        return q

    def file_for(self, job_id: str) -> tuple[Path, str]:
        job = self.jobs.get(job_id)
        if job is None:
            raise JobNotFound(f"Job {job_id} no existe")
        if job.status != "done" or job.file_path is None:
            raise FileNotFoundError("Aún no listo")
        if not job.file_path.exists():
            raise FileNotFoundError("Archivo expirado")
        return job.file_path, job.filename or "audio.mp3"

    def schedule_cleanup(self, job_id: str) -> BackgroundTask:
        return BackgroundTask(self._cleanup_after_download, job_id)

    async def _cleanup_after_download(self, job_id: str) -> None:
        await asyncio.sleep(2)
        await self._cleanup(job_id)

    async def janitor_loop(self) -> None:
        while not self._shutdown:
            try:
                now = time.time()
                stale = [jid for jid, job in self.jobs.items() if now - job.created_at > JOB_TTL_SECONDS]
                for jid in stale:
                    await self._cleanup(jid)
            except Exception:  # noqa: BLE001
                log.exception("Janitor falló")
            await asyncio.sleep(JANITOR_INTERVAL_SECONDS)

    async def shutdown(self) -> None:
        self._shutdown = True
        for jid in list(self.jobs.keys()):
            await self._cleanup(jid)

    # ---------------------------------------------------------------- internos

    async def _run_job(self, job: Job) -> None:
        loop = asyncio.get_running_loop()
        job_dir = self.work_dir / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        try:
            await loop.run_in_executor(None, self._download_and_convert, job, job_dir)
        except ConversionError as exc:
            await self._publish(job, status="error", message=str(exc))
            await self._cleanup(job.job_id, keep_record=True)
        except Exception:  # noqa: BLE001
            log.exception("Job %s falló inesperadamente", job.job_id)
            await self._publish(job, status="error", message="Error interno.")
            await self._cleanup(job.job_id, keep_record=True)

    def _download_and_convert(self, job: Job, job_dir: Path) -> None:
        safe_title = _safe_filename(job.title)
        outtmpl = str(job_dir / f"{safe_title}.%(ext)s")

        def hook(d: dict) -> None:
            status = d.get("status")
            if status == "downloading":
                downloaded = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                pct = (downloaded / total * 80.0) if total else 0.0
                self._publish_sync(job, status="downloading", progress=pct, message="Descargando…")
            elif status == "finished":
                self._publish_sync(job, status="converting", progress=85.0, message="Convirtiendo a MP3…")
            elif status == "error":
                self._publish_sync(job, status="error", message="Error de descarga.")

        opts = {
            "quiet": True,
            "no_warnings": True,
            "outtmpl": outtmpl,
            "format": "bestaudio/best",
            "noplaylist": True,
            "max_filesize": 500 * 1024 * 1024,
            "progress_hooks": [hook],
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(job.url, download=True)
        except yt_dlp.utils.DownloadError as exc:
            raise ConversionError("No se pudo descargar el audio.") from exc

        mp3 = next(job_dir.glob("*.mp3"), None)
        if mp3 is None:
            raise ConversionError("La conversión no produjo MP3.")
        job.file_path = mp3
        job.filename = f"{safe_title}.mp3"
        self._publish_sync(job, status="done", progress=100.0, message="Listo para descargar.")

    def _snapshot(self, job: Job) -> dict:
        return {
            "job_id": job.job_id,
            "status": job.status,
            "progress": round(job.progress, 1),
            "message": job.message,
            "title": job.title,
            "thumbnail": job.thumbnail,
            "duration": job.duration,
            "filename": job.filename,
        }

    def _publish_sync(self, job: Job, *, status: str, progress: float | None = None, message: str = "") -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._publish_threadsafe, job, status, progress, message)

    def _publish_threadsafe(self, job: Job, status: str, progress: float | None, message: str) -> None:
        # Dedup: si nada cambió, no spameamos a los subscriptores.
        prev = (job.status, round(job.progress, 1), job.message)
        job.status = status
        if progress is not None:
            job.progress = max(job.progress, progress)
        if message:
            job.message = message
        curr = (job.status, round(job.progress, 1), job.message)
        if curr == prev:
            return
        snap = self._snapshot(job)
        for q in job.subscribers:
            try:
                q.put_nowait(snap)
            except asyncio.QueueFull:
                pass

    async def _publish(self, job: Job, *, status: str, progress: float | None = None, message: str = "") -> None:
        job.status = status
        if progress is not None:
            job.progress = max(job.progress, progress)
        if message:
            job.message = message
        snap = self._snapshot(job)
        for q in job.subscribers:
            await q.put(snap)

    async def _cleanup(self, job_id: str, *, keep_record: bool = False) -> None:
        job = self.jobs.get(job_id)
        if job is None or job.cleaned:
            return
        job.cleaned = True
        job_dir = self.work_dir / job_id
        if job_dir.exists():
            try:
                shutil.rmtree(job_dir)
            except OSError:
                log.warning("No pude borrar %s", job_dir)
        if not keep_record:
            self.jobs.pop(job_id, None)
