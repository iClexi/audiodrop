<div align="center">

# VideoDrop Studio

### YouTube downloads, long-video segments, captions, and operational visibility.

![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![Python](https://img.shields.io/badge/Python_3.10+-1e3a8a?style=for-the-badge&logo=python&logoColor=white)
![yt-dlp](https://img.shields.io/badge/yt--dlp-c1121f?style=for-the-badge&logo=youtube&logoColor=white)
![ffmpeg](https://img.shields.io/badge/ffmpeg-007808?style=for-the-badge&logo=ffmpeg&logoColor=white)
![Vanilla JS](https://img.shields.io/badge/Vanilla_JS-f7df1e?style=for-the-badge&logo=javascript&logoColor=111827)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-30557c?style=for-the-badge&logo=postgresql&logoColor=white)
![SSE](https://img.shields.io/badge/Server--Sent_Events-111827?style=for-the-badge&logo=icloud&logoColor=white)

**Tags:** `fastapi` · `yt-dlp` · `ffmpeg` · `youtube-downloader` · `captions` · `sse` · `admin-dashboard` · `self-hosted`

</div>

---

## What It Is

VideoDrop Studio is a focused web app for preparing YouTube media downloads and extracting captions when the source video exposes subtitles. It is built for a clean user flow: paste a link, inspect the available formats, convert only what is needed, and download the result.

The app supports audio downloads, video downloads, long-video segmentation, caption extraction, a local admin dashboard, optional account history, and optional operational auditing through PostgreSQL.

## Core Experience

- Paste a YouTube URL.
- Fetch metadata: title, thumbnail, channel, duration, and available qualities.
- Choose audio or video output.
- Convert through `yt-dlp` and `ffmpeg`.
- Stream job progress through Server-Sent Events.
- Download the generated file.
- Extract captions/transcripts when YouTube provides them.
- For longer videos, select a segment instead of forcing one huge conversion.

## Features

- MP3 output at common bitrates.
- MP4 output using available source qualities.
- Long-video segment support.
- Caption and subtitle extraction when exposed by the source.
- Metadata caching to avoid repeated slow lookups for the same URL.
- SSE progress stream for conversion jobs.
- Temporary file cleanup after download and periodic janitor cleanup.
- Legal acceptance flow with terms and privacy pages.
- Optional reCAPTCHA v3 for suspicious traffic.
- Optional user accounts and account history.
- Local/admin panel for operational visibility.
- Block/unblock IP controls for abuse handling.
- Session and activity visibility without hiding what is collected.
- Sentry integration through environment variables.

## Responsible Data Handling

VideoDrop is designed for transparent operational auditing, not hidden tracking. Browser telemetry and admin-visible activity must be explicit, visible in the UI/legal text, and handled through documented endpoints.

Do not commit production secrets, admin entry secrets, session secrets, Sentry DSNs, database URLs, or real service tokens.

## Tech Stack

| Layer | Stack |
| --- | --- |
| Backend | Python 3.10+, FastAPI, Uvicorn |
| Downloading | `yt-dlp` |
| Conversion | `ffmpeg` |
| Progress | Starlette `StreamingResponse` with SSE |
| Frontend | HTML templates, vanilla JavaScript, CSS |
| Persistence | Optional PostgreSQL audit store |
| Abuse Controls | Optional reCAPTCHA v3, admin IP allowlist, IP blocking |
| Deployment | systemd service behind a reverse proxy |

## Project Structure

```text
audiodrop-work/
├── app/
│   ├── main.py              # FastAPI routes and app wiring
│   ├── audio_service.py     # yt-dlp, ffmpeg, jobs, segments, cleanup
│   ├── audit_store.py       # Optional PostgreSQL audit/admin store
│   ├── templates/
│   │   ├── index.html
│   │   ├── admin.html
│   │   ├── account.html
│   │   ├── terms.html
│   │   └── privacy.html
│   └── static/
│       ├── app.js
│       ├── admin.js
│       ├── account.js
│       └── style.css
├── deploy/
│   ├── audiodrop.service
│   ├── 006-audiodrop.conf
│   └── install.sh
├── requirements.txt
├── run.sh
├── .env.example
└── README.md
```

## Main Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Main UI |
| `GET` | `/admin` | Admin dashboard |
| `GET` | `/cuenta` | Account dashboard |
| `GET` | `/terminos` | Terms |
| `GET` | `/privacidad` | Privacy |
| `GET` | `/api/health` | Health check |
| `POST` | `/api/metadata` | Resolve video metadata and available outputs |
| `POST` | `/api/transcript` | Extract captions/subtitles when available |
| `POST` | `/api/convert` | Start a conversion job |
| `GET` | `/api/progress/{job_id}` | SSE job progress |
| `GET` | `/api/download/{job_id}` | Download and cleanup generated file |
| `GET` | `/api/admin/overview` | Admin activity summary |
| `POST` | `/api/admin/block-ip` | Block an abusive IP |
| `POST` | `/api/admin/unblock-ip` | Unblock an IP |
| `POST` | `/api/admin/forget-client` | Remove session-associated events |

## Local Development

Requirements:

- Python 3.10+
- `ffmpeg` available in `PATH`

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 127.0.0.1 --port 3400 --reload
```

Then open:

```text
http://127.0.0.1:3400
```

## Environment

Use `.env.example` as a safe template. Real values belong in an ignored local file or a protected server environment file.

Important settings:

| Variable | Purpose |
| --- | --- |
| `AUDIODROP_PORT` | HTTP listen port. |
| `AUDIODROP_WORK_DIR` | Temporary working directory for media jobs. |
| `AUDIODROP_MAX_DURATION` | Segment size / duration policy in seconds. |
| `AUDIODROP_ADMIN_IP` | Admin allowlist IP or trusted internal source. |
| `AUDIODROP_ADMIN_ENTRY_SECRET` | Optional admin shortcut secret. Keep empty in Git. |
| `AUDIODROP_DATABASE_URL` | Optional PostgreSQL DSN for audit/history/blocking. |
| `AUDIODROP_RECAPTCHA_*` | Optional reCAPTCHA v3 configuration. |
| `AUDIODROP_SENTRY_*` | Optional Sentry configuration. |

Never commit a filled `.env`, admin secret, database DSN, Sentry DSN, API key, or tunnel token.

## Deployment Notes

The repo includes a systemd unit, reverse-proxy template, and install script for a no-Docker deployment.

High-level deployment flow:

1. Install Python, `ffmpeg`, and app dependencies.
2. Copy the app to the target runtime directory.
3. Put secrets in a protected environment file outside Git.
4. Start or restart `audiodrop.service`.
5. Verify `/api/health`.
6. Check logs for failed downloads, permission issues, or missing `ffmpeg`.

For Server-Sent Events, the reverse proxy should avoid buffering the progress stream and allow longer read timeouts for conversions.

## Performance Notes

- Metadata is cached briefly in memory.
- `yt-dlp` uses concurrent fragments for DASH downloads.
- Video output prefers remuxing over re-encoding whenever possible.
- Audio conversion only runs the audio pipeline.
- SSE updates are deduplicated to avoid noisy progress frames.
- Temporary outputs are cleaned after download and by a periodic janitor.

## Security Notes

- The service should run as a non-root user.
- Filenames are sanitized.
- URLs are validated before processing.
- Private or unavailable videos return user-friendly errors.
- Download jobs are temporary and not intended as long-term storage.
- Admin features should be restricted by trusted network controls and environment configuration.
- reCAPTCHA is optional and only activates when configured.

## License

Use responsibly. Respect the rights of content owners and the terms of the services you access.
