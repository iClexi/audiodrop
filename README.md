<div align="center">

# AudioDrop

### YouTube → MP3 o MP4. Rápido, elegante y sin fricción.

<br/>

![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![Python](https://img.shields.io/badge/Python_3.10+-1e3a8a?style=for-the-badge&logo=python&logoColor=white)
![yt-dlp](https://img.shields.io/badge/yt--dlp-c1121f?style=for-the-badge&logo=youtube&logoColor=white)
![ffmpeg](https://img.shields.io/badge/ffmpeg-007808?style=for-the-badge&logo=ffmpeg&logoColor=white)
![Glassmorphism](https://img.shields.io/badge/UI-Glassmorphism-7c3aed?style=for-the-badge&logo=cssdesignawards&logoColor=white)

</div>

---

## ¿Qué es esto?

**AudioDrop** es una app web minimalista para descargar audio (**MP3**) o video (**MP4**) de YouTube,
con selector de calidad. Backend en Python con **FastAPI**, descargas con **yt-dlp**, conversión con
**ffmpeg**, frontend en HTML/CSS/JS puro con tema oscuro, glassmorphism y animaciones suaves.

- Pega una URL → preview con thumbnail, título, uploader y duración.
- Elige **Audio** o **Video** y escoge la calidad disponible.
- Pulsa convertir → progreso en tiempo real (Server-Sent Events).
- Descarga el archivo → lo borramos del servidor a los pocos segundos.
- Sin fricción para el usuario final, con controles de seguridad para operación.

## Características

- 🎵 **Audio MP3** a 128 / 192 / 320 kbps.
- 🎬 **Video MP4** hasta 4K, con variantes 60 fps cuando el video original las tiene
  (360p · 480p · 720p · 720p60 · 1080p · 1080p60 · 1440p · 1440p60 · 4K · 4K60).
- ⚡ Optimizado para arrancar rápido: `concurrent_fragment_downloads=5`, cache de
  metadatos, sin re-encode de video cuando los streams ya están en mp4.
- 📡 Progreso en vivo con Server-Sent Events.
- 🔒 Sanitización de nombres, validación de URL, límite de duración (30 min) y tamaño (2 GiB).
- 🧹 Limpieza automática: archivos borrados al descargar + janitor cada 5 min.
- 📱 Responsive total — móvil y escritorio (probado iPhone 13 Pro e iPad).
- 🪟 UI premium tipo SaaS: dark mode, glassmorphism, blur, gradients.
- 🛡️ Servicio aislado en systemd con hardening básico (no root, `ProtectSystem=strict`, `MemoryMax=600M`).
- 🧭 Panel admin interno (`/admin`) con:
  - monitoreo de tráfico,
  - registro de descargas y eventos,
  - bloqueo/desbloqueo de IP en caliente.
- 🔗 Acortador de enlaces público (`/acortador`) con redirección corta (`/s/{code}`).
- 🗃️ Auditoría en PostgreSQL (`AUDIODROP_DATABASE_URL`).
- 📜 Popup legal + páginas de Términos y Privacidad.

## Stack

| Capa       | Herramienta                                |
|------------|--------------------------------------------|
| Backend    | Python 3.10+, FastAPI, Uvicorn             |
| Descarga   | yt-dlp (`player_client: ios,web` para evitar SABR) |
| Conversión | ffmpeg (postprocessor de yt-dlp)           |
| Streaming  | SSE (`StreamingResponse` de Starlette)     |
| Frontend   | HTML5, CSS3 (custom props, backdrop-filter), JS vanilla |
| Fuentes    | Inter + JetBrains Mono (Google Fonts)      |
| Despliegue | systemd + Apache/nginx reverse proxy o Cloudflare Tunnel |

## Estructura

```
audiodrop-work/
├── app/
│   ├── main.py             # Endpoints HTTP (FastAPI)
│   ├── audio_service.py    # yt-dlp + ffmpeg + cleanup + cache
│   ├── templates/
│   │   └── index.html      # Vista principal con tabs Audio/Video
│   └── static/
│       ├── style.css       # Glassmorphism dark theme
│       └── app.js          # State machine del frontend
├── deploy/
│   ├── audiodrop.service       # Unidad systemd
│   ├── 006-audiodrop.conf      # vhost Apache (con Cache-Control: no-store en /static)
│   └── install.sh              # Instalador one-shot
├── requirements.txt
├── .env.example
├── .gitignore
├── run.sh
└── README.md
```

## Endpoints

| Método | Ruta                          | Descripción                                  |
|--------|-------------------------------|----------------------------------------------|
| GET    | `/`                           | UI principal                                 |
| GET    | `/admin`                      | Panel admin (sólo red local admin)          |
| GET    | `/acortador`                 | UI pública para crear enlaces cortos        |
| GET    | `/terminos`                   | Página de términos y condiciones            |
| GET    | `/privacidad`                 | Política de privacidad                       |
| GET    | `/api/health`                 | Healthcheck                                  |
| POST   | `/api/telemetry`              | Telemetría de navegador (consentida)         |
| POST   | `/api/shortener/create`       | Crea un enlace corto                         |
| GET    | `/s/{code}`                   | Redirecciona al enlace original              |
| GET    | `/api/admin/overview`         | Resumen y eventos para el panel admin        |
| POST   | `/api/admin/block-ip`         | Bloquear IP                                  |
| POST   | `/api/admin/unblock-ip`       | Desbloquear IP                               |
| POST   | `/api/metadata`               | Título, thumbnail, duración + `audio_options` y `video_options` disponibles |
| POST   | `/api/convert`                | Lanza el job. Body: `{url, format}` donde `format` ∈ `mp3-128 \| mp3-192 \| mp3-320 \| video-<height>[60]` |
| GET    | `/api/progress/{job_id}`      | Stream SSE con el progreso                   |
| GET    | `/api/download/{job_id}`      | Descarga el archivo (borra el server side después) |

### Ejemplo

```bash
# 1) Metadata (lista calidades disponibles para ese video)
curl -s -X POST http://localhost:3400/api/metadata \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ"}' | jq

# 2) Convertir a video 1080p
JOB=$(curl -s -X POST http://localhost:3400/api/convert \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ","format":"video-1080p"}' | jq -r .job_id)

# 3) Escuchar progreso
curl -N http://localhost:3400/api/progress/$JOB

# 4) Descargar
curl -o video.mp4 http://localhost:3400/api/download/$JOB
```

## Quickstart — desarrollo local

Requisitos: `python3` 3.10+, `ffmpeg` en el `PATH`.

```bash
git clone https://github.com/iClexi/audiodrop && cd audiodrop
./run.sh
# Abre http://localhost:3400
```

## Despliegue en Ubuntu / Debian (sin Docker)

```bash
sudo bash deploy/install.sh
```

Hace lo siguiente, idempotente:

1. Instala `python3-venv`, `python3-pip` y `ffmpeg`.
2. Crea el usuario `infra` si no existe.
3. Copia el código a `/opt/audiodrop/app`.
4. Crea un venv en `/opt/audiodrop/venv` con las dependencias.
5. Escribe `/etc/audiodrop/audiodrop.env` con los defaults.
6. Habilita la unidad `audiodrop.service` y arranca el servicio.

Después, copia el vhost de Apache (con `Cache-Control: no-store` para evitar JS/CSS viejo en CDN):

```bash
sudo cp deploy/006-audiodrop.conf /etc/apache2/sites-enabled/
sudo a2enmod headers
sudo systemctl reload apache2
```

O sirvelo con nginx:

```nginx
server {
  listen 80;
  server_name audiodrop.example.com;

  location /static/ {
    proxy_pass http://127.0.0.1:3400/static/;
    add_header Cache-Control "no-store, must-revalidate";
  }
  location / {
    proxy_pass http://127.0.0.1:3400;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-Host $host;
    proxy_buffering off;          # SSE necesita esto
    proxy_read_timeout 300s;
  }
}
```

O detrás de **Cloudflare Tunnel** apuntando al `127.0.0.1:3400` (o al puerto del socat
si usas un patrón socat → VIP HA).

## Variables de entorno

| Variable                  | Default            | Para qué sirve                                  |
|---------------------------|--------------------|-------------------------------------------------|
| `AUDIODROP_PORT`          | `3400`             | Puerto de escucha (interno).                    |
| `AUDIODROP_WORK_DIR`      | `/tmp/audiodrop`   | Directorio para descargas temporales.           |
| `AUDIODROP_MAX_DURATION`  | `1800`             | Duración máxima permitida del video (segundos). |
| `AUDIODROP_LOG_LEVEL`     | `INFO`             | Nivel de logs.                                  |
| `AUDIODROP_ADMIN_IP`      | `192.168.68.83`    | IP LAN autorizada para panel admin.             |
| `AUDIODROP_DATABASE_URL`  | *(vacío)*          | DSN PostgreSQL para auditoría y bloqueo IP.     |

## Performance

Lo que se ha optimizado para que "Preparando…" no se eternice:

- **Cache de metadatos** (90 s, in-memory): `/api/metadata` y `/api/convert` ya no llaman a
  yt-dlp dos veces seguidas para la misma URL.
- **`player_client=[ios, web]`**: evita el modo SABR que añade retries en YouTube.
- **`concurrent_fragment_downloads=5`**: las descargas DASH bajan 5 fragmentos en paralelo.
- **Sin re-encode innecesario de video**: usamos `merge_output_format='mp4'` y un selector que
  prioriza `mp4+m4a` (sólo remux, no transcode). Quitamos `FFmpegVideoConvertor` que forzaba un
  re-encode adicional.
- **Audio prefiere m4a** como fuente → ffmpeg sólo convierte audio (no re-empaqueta video).
- **Dedup de SSE**: el servidor no spamea frames repetidos al cliente.
- **`cachedir`** de yt-dlp en `/tmp/audiodrop-cache` para reuso de signatures entre runs.

## Seguridad

- El servicio corre como **`infra`** (no root) con `NoNewPrivileges`, `ProtectSystem=strict`,
  `ProtectHome=true` y `MemoryMax=600M`.
- URLs validadas por regex, nombres de archivo sanitizados.
- Videos privados o no disponibles devuelven un mensaje legible (no stacktrace).
- Duración máxima 30 min por defecto, tamaño máximo 2 GiB.
- Cuerpo HTTP topado a 8 KiB por el reverse proxy.
- Descargas se borran tras 1 h o al completar la descarga del cliente.

## Limitaciones

- Sin cola persistente: reiniciar el servicio pierde los jobs en curso.
- Un único worker por proceso. Para alta concurrencia: varios workers de uvicorn + un job store
  (Redis / SQLite).
- Sólo procesa videos individuales (no playlists).

## Licencia

MIT. Úsalo responsablemente y respeta los Términos de Servicio de YouTube y los derechos del
contenido al que se acceda.
