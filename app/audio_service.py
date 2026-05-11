"""MediaService — descarga + conversión con yt-dlp y ffmpeg.

Soporta dos modos:
- audio: extrae MP3 a 128/192/320 kbps
- video: descarga MP4 hasta 1080p (30 o 60 fps)

La capa HTTP nunca debería tocar yt-dlp ni el sistema de archivos directamente.
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

JOB_TTL_SECONDS = 60 * 60
JANITOR_INTERVAL_SECONDS = 60 * 5
INFO_CACHE_TTL = 90  # segundos: reusamos info extraída para no llamar a yt-dlp 2 veces seguidas
INFO_CACHE: dict[str, tuple[float, dict]] = {}

# Cliente "ios" suele entregar URLs HLS sin SABR, evita el retry de yt-dlp.
_BASE_YDL_OPTS: dict = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "concurrent_fragment_downloads": 5,
    "extractor_args": {"youtube": {"player_client": ["ios", "web"]}},
    "cachedir": "/tmp/audiodrop-cache",
    "retries": 2,
    "fragment_retries": 2,
}

# Opciones de audio que siempre ofrecemos (yt-dlp + ffmpeg pueden generar cualquiera).
AUDIO_OPTIONS = [
    {"key": "mp3-320", "label": "MP3 · 320 kbps", "bitrate": "320"},
    {"key": "mp3-192", "label": "MP3 · 192 kbps", "bitrate": "192"},
    {"key": "mp3-128", "label": "MP3 · 128 kbps", "bitrate": "128"},
]

# Resoluciones de video que ofrecemos cuando están disponibles. (height, fps, label, key)
# fps=60 sólo se ofrece si el video tiene un formato real con fps>=50 a esa altura.
VIDEO_QUALITIES = [
    (2160, 60, "4K · 60 fps", "video-2160p60"),
    (2160, 30, "4K", "video-2160p"),
    (1440, 60, "1440p · 60 fps", "video-1440p60"),
    (1440, 30, "1440p", "video-1440p"),
    (1080, 60, "1080p · 60 fps", "video-1080p60"),
    (1080, 30, "1080p", "video-1080p"),
    (720, 60, "720p · 60 fps", "video-720p60"),
    (720, 30, "720p", "video-720p"),
    (480, 30, "480p", "video-480p"),
    (360, 30, "360p", "video-360p"),
]


class ConversionError(Exception):
    """Error de negocio: URL no válida, video privado, demasiado largo, etc."""


class JobNotFound(Exception):
    """El job_id no existe o ya fue limpiado."""


@dataclass
class Job:
    job_id: str
    url: str
    format_key: str = "mp3-192"
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
    name = name.strip(" ._") or "media"
    return name[:120]


def _parse_format_key(key: str) -> tuple[str, dict]:
    """Devuelve ('audio'|'video', params) o lanza ConversionError."""
    if key.startswith("mp3-"):
        bitrate = key.split("-", 1)[1]
        if bitrate not in ("128", "192", "320"):
            raise ConversionError("Bitrate de audio no soportado.")
        return "audio", {"bitrate": bitrate}
    if key.startswith("video-"):
        for height, fps, _label, k in VIDEO_QUALITIES:
            if k == key:
                return "video", {"height": height, "fps": fps}
        raise ConversionError("Calidad de video no soportada.")
    raise ConversionError("Formato no reconocido.")


def _available_video_options(info: dict) -> list[dict]:
    """Filtra VIDEO_QUALITIES contra los formatos reales que tiene el video.

    - Una altura está disponible si hay al menos un formato de video con esa altura.
    - La variante "60 fps" se ofrece sólo cuando algún formato a esa altura tiene fps>=50.
    """
    formats = info.get("formats") or []
    max_fps_per_height: dict[int, float] = {}
    for f in formats:
        if (f.get("vcodec") or "none") == "none":
            continue
        h = f.get("height") or 0
        fps = float(f.get("fps") or 0)
        if h <= 0:
            continue
        if fps > max_fps_per_height.get(h, 0):
            max_fps_per_height[h] = fps

    available = []
    for height, fps_target, label, key in VIDEO_QUALITIES:
        if height not in max_fps_per_height:
            continue
        if fps_target == 60 and max_fps_per_height[height] < 50:
            continue
        available.append({"key": key, "label": label, "height": height, "fps": fps_target})
    return available


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
        info = await self._cached_info(url)
        duration = info.get("duration") or 0
        if duration and duration > self.max_duration:
            raise ConversionError(
                f"Video demasiado largo ({duration//60} min). Máximo {self.max_duration//60} min."
            )
        return {
            "title": info.get("title") or "Media",
            "thumbnail": info.get("thumbnail") or "",
            "duration": duration,
            "uploader": info.get("uploader") or "",
            "audio_options": AUDIO_OPTIONS,
            "video_options": _available_video_options(info),
        }

    async def _cached_info(self, url: str) -> dict:
        """Devuelve info de yt-dlp con cache corto (evita llamar dos veces seguidas)."""
        now = time.time()
        cached = INFO_CACHE.get(url)
        if cached and now - cached[0] < INFO_CACHE_TTL:
            return cached[1]
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, self._extract_info, url)
        INFO_CACHE[url] = (now, info)
        # Limpia entradas viejas para no crecer infinito.
        for k, (ts, _) in list(INFO_CACHE.items()):
            if now - ts > INFO_CACHE_TTL * 4:
                INFO_CACHE.pop(k, None)
        return info

    def _extract_info(self, url: str) -> dict:
        opts = {**_BASE_YDL_OPTS, "skip_download": True, "extract_flat": False}
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

    async def start_job(self, url: str, format_key: str = "mp3-192") -> str:
        _parse_format_key(format_key)
        # Reusa la info ya extraída por /api/metadata (cache de 90s); evita llamar yt-dlp 2 veces.
        info = await self._cached_info(url)
        duration = info.get("duration") or 0
        if duration and duration > self.max_duration:
            raise ConversionError(
                f"Video demasiado largo ({duration//60} min). Máximo {self.max_duration//60} min."
            )
        self._loop = asyncio.get_running_loop()
        job_id = uuid.uuid4().hex
        job = Job(
            job_id=job_id,
            url=url,
            format_key=format_key,
            title=info.get("title") or "Media",
            thumbnail=info.get("thumbnail") or "",
            duration=duration,
        )
        async with self._lock:
            self.jobs[job_id] = job
        # Estado inicial visible inmediatamente: el cliente verá "Preparando" sin esperar.
        job.status = "downloading"
        job.message = "Preparando…"
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

    def file_for(self, job_id: str) -> tuple[Path, str, str]:
        """Devuelve (path, filename, mime)."""
        job = self.jobs.get(job_id)
        if job is None:
            raise JobNotFound(f"Job {job_id} no existe")
        if job.status != "done" or job.file_path is None:
            raise FileNotFoundError("Aún no listo")
        if not job.file_path.exists():
            raise FileNotFoundError("Archivo expirado")
        kind, _ = _parse_format_key(job.format_key)
        mime = "audio/mpeg" if kind == "audio" else "video/mp4"
        return job.file_path, job.filename or job.file_path.name, mime

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
        kind, params = _parse_format_key(job.format_key)
        outtmpl = str(job_dir / f"{safe_title}.%(ext)s")

        def hook(d: dict) -> None:
            status = d.get("status")
            if status == "downloading":
                downloaded = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                pct = (downloaded / total * 80.0) if total else 0.0
                self._publish_sync(job, status="downloading", progress=pct, message="Descargando…")
            elif status == "finished":
                msg = "Convirtiendo a MP3…" if kind == "audio" else "Procesando video…"
                self._publish_sync(job, status="converting", progress=85.0, message=msg)
            elif status == "error":
                self._publish_sync(job, status="error", message="Error de descarga.")

        base_opts: dict = {
            **_BASE_YDL_OPTS,
            "outtmpl": outtmpl,
            "max_filesize": 2 * 1024 * 1024 * 1024,  # 2 GiB hard cap
            "progress_hooks": [hook],
        }

        if kind == "audio":
            # Prefiere m4a (AAC nativo de YouTube): ffmpeg sólo re-encodea a MP3, no re-mux.
            opts = {
                **base_opts,
                "format": "bestaudio[ext=m4a]/bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": params["bitrate"],
                    }
                ],
            }
            expected_ext = "mp3"
        else:
            height = params["height"]
            fps = params["fps"]
            fps_filter = "" if fps == 60 else "[fps<=30]"
            # Priorizamos formatos mp4+m4a: merge es sólo remux (rápido, sin re-encode).
            # Fallback a cualquier formato + merge a mp4 si no hay mp4 nativo.
            fmt = (
                f"bestvideo[height<={height}][ext=mp4][vcodec^=avc]{fps_filter}+bestaudio[ext=m4a]/"
                f"bestvideo[height<={height}][ext=mp4]{fps_filter}+bestaudio[ext=m4a]/"
                f"bestvideo[height<={height}]{fps_filter}+bestaudio/"
                f"best[height<={height}]"
            )
            opts = {
                **base_opts,
                "format": fmt,
                "merge_output_format": "mp4",
                # SIN FFmpegVideoConvertor — el merge ya entrega .mp4 sin re-encode innecesario.
            }
            expected_ext = "mp4"

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(job.url, download=True)
        except yt_dlp.utils.DownloadError as exc:
            raise ConversionError("No se pudo descargar el media.") from exc

        final = next(job_dir.glob(f"*.{expected_ext}"), None)
        if final is None:
            # fallback: cualquier archivo grande en el dir
            files = sorted(job_dir.iterdir(), key=lambda p: p.stat().st_size, reverse=True)
            final = files[0] if files else None
        if final is None:
            raise ConversionError("La conversión no produjo archivo final.")
        job.file_path = final
        job.filename = f"{safe_title}.{final.suffix.lstrip('.')}"
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
            "format_key": job.format_key,
        }

    def _publish_sync(self, job: Job, *, status: str, progress: float | None = None, message: str = "") -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._publish_threadsafe, job, status, progress, message)

    def _publish_threadsafe(self, job: Job, status: str, progress: float | None, message: str) -> None:
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
