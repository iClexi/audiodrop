<div align="center">

# AudioDrop

### YouTube a MP3 — rápido, elegante y sin fricción.

<br/>

![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![Python](https://img.shields.io/badge/Python_3.10+-1e3a8a?style=for-the-badge&logo=python&logoColor=white)
![yt-dlp](https://img.shields.io/badge/yt--dlp-c1121f?style=for-the-badge&logo=youtube&logoColor=white)
![ffmpeg](https://img.shields.io/badge/ffmpeg-007808?style=for-the-badge&logo=ffmpeg&logoColor=white)
![Glassmorphism](https://img.shields.io/badge/UI-Glassmorphism-7c3aed?style=for-the-badge&logo=cssdesignawards&logoColor=white)

</div>

---

## ¿Qué es esto?

**AudioDrop** es una app web minimalista para convertir videos de YouTube en archivos MP3.
Backend en Python con **FastAPI**, descargas con **yt-dlp**, conversión con **ffmpeg**,
y un frontend en HTML/CSS/JS puro con tema oscuro, glassmorphism y animaciones suaves.

- Pega una URL → te mostramos thumbnail, título y duración.
- Pulsa convertir → vemos el progreso en tiempo real (SSE).
- Descarga el MP3 → lo borramos del servidor a los pocos segundos.
- Sin cuentas, sin trackers, sin pasos extra.

## Características

- 🎵 Audio MP3 a 192 kbps por defecto.
- ⚡ Progreso en vivo con Server-Sent Events.
- 🔒 Sanitización de nombres, validación de URL, límite de duración (30 min).
- 🧹 Limpieza automática de archivos temporales.
- 📱 Responsive total — móvil y escritorio.
- 🪟 UI premium tipo SaaS: dark mode, glassmorphism, blur, gradients.
- 🛡️ Servicio aislado en systemd con hardening básico.

## Stack

| Capa       | Herramienta                                |
|------------|--------------------------------------------|
| Backend    | Python 3.10+, FastAPI, Uvicorn             |
| Descarga   | yt-dlp                                     |
| Audio      | ffmpeg (postprocessor)                     |
| Streaming  | SSE (`StreamingResponse` de Starlette)     |
| Frontend   | HTML5, CSS3 (custom props, backdrop-filter), JS vanilla |
| Fuentes    | Inter + JetBrains Mono (Google Fonts)      |
| Despliegue | systemd + Apache/nginx reverse proxy o Cloudflare Tunnel |

## Estructura

```
audiodrop-work/
├── app/
│   ├── main.py             # Endpoints HTTP (FastAPI)
│   ├── audio_service.py    # yt-dlp + ffmpeg + cleanup
│   ├── templates/
│   │   └── index.html      # Vista principal
│   └── static/
│       ├── style.css       # Glassmorphism dark theme
│       └── app.js          # State machine del frontend
├── deploy/
│   ├── audiodrop.service       # Unidad systemd
│   ├── 006-audiodrop.conf      # vhost Apache
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
| GET    | `/api/health`                 | Healthcheck                                  |
| POST   | `/api/metadata`               | Lee título, thumbnail y duración             |
| POST   | `/api/convert`                | Lanza el job y devuelve `job_id`             |
| GET    | `/api/progress/{job_id}`      | Stream SSE con el progreso                   |
| GET    | `/api/download/{job_id}`      | Descarga el MP3 (borra el archivo después)   |

## Quickstart — desarrollo local

Requisitos: `python3` 3.10+, `ffmpeg` en el `PATH`.

```bash
git clone <este-repo> audiodrop && cd audiodrop
./run.sh
# Abre http://localhost:3400
```

## Despliegue en Ubuntu / Debian (sin Docker)

El proyecto incluye un instalador idempotente:

```bash
sudo bash deploy/install.sh
```

Hace lo siguiente:

1. Instala `python3-venv`, `python3-pip` y `ffmpeg`.
2. Crea el usuario `infra` si no existe.
3. Copia el código a `/opt/audiodrop/app`.
4. Crea un venv en `/opt/audiodrop/venv` con las dependencias.
5. Escribe `/etc/audiodrop/audiodrop.env` con los defaults.
6. Habilita la unidad `audiodrop.service` y arranca el servicio.

Después puedes copiar el vhost de Apache:

```bash
sudo cp deploy/006-audiodrop.conf /etc/apache2/sites-enabled/
sudo systemctl reload apache2
```

O servirlo con nginx:

```nginx
server {
  listen 80;
  server_name audiodrop.example.com;

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

O detrás de **Cloudflare Tunnel** apuntando al `127.0.0.1:3400` de la VM.

## Variables de entorno

| Variable                  | Default            | Para qué sirve                                  |
|---------------------------|--------------------|-------------------------------------------------|
| `AUDIODROP_PORT`          | `3400`             | Puerto de escucha (interno).                    |
| `AUDIODROP_WORK_DIR`      | `/tmp/audiodrop`   | Directorio para descargas temporales.           |
| `AUDIODROP_MAX_DURATION`  | `1800`             | Duración máxima permitida del video (segundos). |
| `AUDIODROP_LOG_LEVEL`     | `INFO`             | Nivel de logs.                                  |

## Seguridad

- El servicio corre como **`infra`** (no root) con `NoNewPrivileges`, `ProtectSystem=full`, `ProtectHome=true` y `MemoryMax=600M`.
- Se validan las URLs con regex y se sanitizan los nombres de archivo.
- Se rechazan videos privados o no disponibles con un mensaje legible.
- Se limita la duración a 30 minutos por defecto y el tamaño a 500 MB.
- El cuerpo HTTP está topado en 8 KiB por el reverse proxy (sólo necesitamos una URL).
- Las descargas se borran tras 1 hora o tras la descarga del usuario.

## Limitaciones / por hacer

- Sin cola persistente: si reinicias el servicio, los jobs en curso se pierden.
- Un único worker por proceso. Para escalar horizontal añade varios workers de uvicorn + un job store.
- Sólo procesa videos individuales (no playlists).

## Licencia

MIT. Usa la herramienta de forma responsable y respeta los términos de servicio de YouTube
y los derechos del contenido al que se acceda.
