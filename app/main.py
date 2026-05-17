"""VideoDrop — FastAPI entrypoint."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import sentry_sdk

from audio_service import AudioService, ConversionError, JobNotFound
from audit_store import AuditStore

LOG_LEVEL = os.environ.get("AUDIODROP_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
)
log = logging.getLogger("audiodrop")

BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = Path(os.environ.get("AUDIODROP_WORK_DIR", "/tmp/audiodrop"))
MAX_DURATION = int(os.environ.get("AUDIODROP_MAX_DURATION", "1800"))  # 30 min
ADMIN_LAN_IP = os.environ.get("AUDIODROP_ADMIN_IP", "127.0.0.1")
ADMIN_ENTRY_SECRET = os.environ.get("AUDIODROP_ADMIN_ENTRY_SECRET", "").strip()
ADMIN_SESSION_COOKIE = "videodrop_admin"
ADMIN_SESSION_TTL_SECONDS = max(300, int(os.environ.get("AUDIODROP_ADMIN_SESSION_TTL_SECONDS", "21600")))
USER_SESSION_COOKIE = "videodrop_session"
USER_SESSION_TTL_SECONDS = max(3600, int(os.environ.get("AUDIODROP_SESSION_TTL_SECONDS", "2592000")))
PASSWORD_ITERATIONS = max(120000, int(os.environ.get("AUDIODROP_PASSWORD_ITERATIONS", "210000")))
SENTRY_DSN = os.environ.get("AUDIODROP_SENTRY_DSN", "").strip()
SENTRY_ENVIRONMENT = os.environ.get("AUDIODROP_SENTRY_ENVIRONMENT", os.environ.get("AUDIODROP_ENV", "production"))
RECAPTCHA_SITE_KEY = os.environ.get("AUDIODROP_RECAPTCHA_SITE_KEY", "").strip()
RECAPTCHA_SECRET_KEY = os.environ.get("AUDIODROP_RECAPTCHA_SECRET_KEY", "").strip()


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


RECAPTCHA_SCORE_THRESHOLD = max(0.0, min(1.0, _float_env("AUDIODROP_RECAPTCHA_SCORE_THRESHOLD", 0.45)))


def _sentry_traces_sample_rate() -> float:
    raw_value = os.environ.get("AUDIODROP_SENTRY_TRACES_SAMPLE_RATE", "0.1")
    try:
        sample_rate = float(raw_value)
    except ValueError:
        sample_rate = 0.1
    return max(0.0, min(1.0, sample_rate))


SENTRY_TRACES_SAMPLE_RATE = _sentry_traces_sample_rate()
SENTRY_RELEASE = os.environ.get("AUDIODROP_SENTRY_RELEASE", "").strip()

if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT,
        release=SENTRY_RELEASE or None,
        traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
        send_default_pii=False,
    )

WORK_DIR.mkdir(parents=True, exist_ok=True)

service = AudioService(work_dir=WORK_DIR, max_duration=MAX_DURATION)
audit_store = AuditStore(os.environ.get("AUDIODROP_DATABASE_URL"))

app = FastAPI(title="VideoDrop", version="1.3.0", docs_url=None, redoc_url=None)
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


def _request_client_ip(request: Request) -> str:
    xff = (request.headers.get("x-forwarded-for") or "").split(",")
    if xff and xff[0].strip():
        return xff[0].strip()
    return request.client.host if request.client else ""


def _request_public_ip(request: Request) -> str:
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    return _request_client_ip(request)


def _request_meta(request: Request, *, event_type: str, status_code: int) -> dict[str, Any]:
    auth = getattr(request.state, "current_auth", None) or {}
    user = auth.get("user") if isinstance(auth, dict) else None
    return {
        "event_type": event_type,
        "method": request.method,
        "path": request.url.path,
        "query_string": request.url.query or "",
        "status_code": status_code,
        "client_ip": _request_client_ip(request),
        "public_ip": _request_public_ip(request),
        "user_agent": request.headers.get("user-agent", ""),
        "referer": request.headers.get("referer", ""),
        "request_host": request.headers.get("host", ""),
        "request_scheme": request.url.scheme,
        "user_id": user.get("id") if isinstance(user, dict) else None,
    }


def _admin_session_payload() -> str:
    now = int(time.time())
    payload = json.dumps(
        {"iat": now, "exp": now + ADMIN_SESSION_TTL_SECONDS, "scope": "admin"},
        separators=(",", ":"),
        sort_keys=True,
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _sign_admin_session(payload: str) -> str:
    return hmac.new(ADMIN_ENTRY_SECRET.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()


def _create_admin_session_token() -> str:
    payload = _admin_session_payload()
    return f"{payload}.{_sign_admin_session(payload)}"


def _has_admin_session(request: Request) -> bool:
    if not ADMIN_ENTRY_SECRET:
        return False
    token = request.cookies.get(ADMIN_SESSION_COOKIE, "")
    try:
        payload, signature = token.split(".", 1)
    except ValueError:
        return False
    expected = _sign_admin_session(payload)
    if not hmac.compare_digest(signature, expected):
        return False
    padded_payload = payload + "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(padded_payload.encode("ascii")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return False
    try:
        expires_at = int(data.get("exp") or 0)
    except (TypeError, ValueError):
        return False
    return data.get("scope") == "admin" and expires_at >= int(time.time())


def _is_local_admin_request(request: Request) -> bool:
    # Si hay cabeceras de Cloudflare, no consideramos esta peticion "local admin".
    if request.headers.get("cf-ray") or request.headers.get("cf-connecting-ip"):
        return False
    return _request_client_ip(request) == ADMIN_LAN_IP


def _is_admin_request(request: Request) -> bool:
    auth = getattr(request.state, "current_auth", None) or {}
    user = auth.get("user") if isinstance(auth, dict) else None
    if isinstance(user, dict) and user.get("role") == "admin":
        return True
    return _is_local_admin_request(request) or _has_admin_session(request)


def _require_admin(request: Request) -> None:
    if not _is_admin_request(request):
        raise HTTPException(status_code=404, detail="No encontrado")


def _is_secure_cookie_request(request: Request) -> bool:
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return forwarded_proto == "https" or request.url.scheme == "https" or bool(request.headers.get("cf-ray"))


def _template_context(request: Request) -> dict[str, Any]:
    auth = getattr(request.state, "current_auth", None) or {}
    return {
        "request": request,
        "sentry_dsn": SENTRY_DSN,
        "sentry_environment": SENTRY_ENVIRONMENT,
        "sentry_release": SENTRY_RELEASE,
        "sentry_traces_sample_rate": SENTRY_TRACES_SAMPLE_RATE,
        "recaptcha_site_key": RECAPTCHA_SITE_KEY,
        "current_user": auth.get("user") if isinstance(auth, dict) else None,
    }


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()[:160]


def _clean_username(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:40]


def _validate_password(value: Any) -> str:
    password = str(value or "")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 8 caracteres.")
    if len(password) > 200:
        raise HTTPException(status_code=400, detail="La contraseña es demasiado larga.")
    return password


def _password_digest(password: str, salt: str, iterations: int = PASSWORD_ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac(
        "sha512",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        int(iterations),
        dklen=64,
    ).hex()


def _hash_password(password: str) -> dict[str, Any]:
    salt = secrets.token_urlsafe(18)
    return {
        "hash": _password_digest(password, salt, PASSWORD_ITERATIONS),
        "salt": salt,
        "iterations": PASSWORD_ITERATIONS,
    }


def _verify_password(password: str, user: dict[str, Any]) -> bool:
    try:
        digest = _password_digest(password, str(user.get("password_salt") or ""), int(user.get("password_iterations") or 0))
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(digest, str(user.get("password_hash") or ""))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _device_label(user_agent: str) -> str:
    ua = user_agent.lower()
    if "iphone" in ua:
        device = "iPhone"
    elif "ipad" in ua:
        device = "iPad"
    elif "android" in ua:
        device = "Android"
    elif "windows" in ua:
        device = "Windows"
    elif "mac os" in ua or "macintosh" in ua:
        device = "Mac"
    elif "linux" in ua:
        device = "Linux"
    else:
        device = "Dispositivo"

    if "edg/" in ua:
        browser = "Edge"
    elif "firefox/" in ua:
        browser = "Firefox"
    elif "safari/" in ua and "chrome/" not in ua:
        browser = "Safari"
    elif "chrome/" in ua or "chromium/" in ua:
        browser = "Chrome"
    else:
        browser = "Navegador"
    return f"{browser} en {device}"


async def _current_auth(request: Request) -> dict[str, Any] | None:
    if hasattr(request.state, "current_auth"):
        return request.state.current_auth
    request.state.current_auth = None
    token = request.cookies.get(USER_SESSION_COOKIE, "")
    if not token:
        return None
    try:
        auth = await audit_store.auth_for_token(_token_hash(token))
    except Exception as exc:  # noqa: BLE001 - no tumbamos descargas anonimas por fallo de auth.
        log.warning("No se pudo resolver sesión de usuario: %s", exc)
        auth = None
    request.state.current_auth = auth
    return auth


async def _require_user(request: Request) -> dict[str, Any]:
    auth = await _current_auth(request)
    user = auth.get("user") if isinstance(auth, dict) else None
    if not isinstance(user, dict):
        raise HTTPException(status_code=401, detail="Inicia sesión.")
    return user


async def _attach_user_session(request: Request, response: JSONResponse, user: dict[str, Any]) -> None:
    token = secrets.token_urlsafe(36)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=USER_SESSION_TTL_SECONDS)
    session = await audit_store.create_session(
        session_id=str(uuid.uuid4()),
        user_id=str(user["id"]),
        token_hash=_token_hash(token),
        device_label=_device_label(request.headers.get("user-agent", "")),
        ip=_request_public_ip(request)[:80],
        user_agent=(request.headers.get("user-agent", "") or "")[:700],
        expires_at=expires_at,
    )
    request.state.current_auth = {"user": user, "session": session}
    response.set_cookie(
        USER_SESSION_COOKIE,
        token,
        max_age=USER_SESSION_TTL_SECONDS,
        httponly=True,
        secure=_is_secure_cookie_request(request),
        samesite="lax",
        path="/",
    )


def _clear_user_cookie(response: JSONResponse) -> None:
    response.delete_cookie(USER_SESSION_COOKIE, path="/", samesite="lax")


def _is_suspicious_request(request: Request) -> bool:
    ua = (request.headers.get("user-agent") or "").lower()
    if not ua:
        return True
    bad_tokens = (
        "bot",
        "crawler",
        "spider",
        "curl",
        "wget",
        "python-requests",
        "httpx",
        "go-http-client",
        "headless",
    )
    if any(token in ua for token in bad_tokens):
        return True
    if request.url.path.startswith("/api/") and not request.headers.get("accept-language"):
        return True
    return False


def _verify_recaptcha_sync(token: str, action: str) -> tuple[bool, dict[str, Any]]:
    form = urllib.parse.urlencode({"secret": RECAPTCHA_SECRET_KEY, "response": token}).encode()
    req = urllib.request.Request(
        "https://www.google.com/recaptcha/api/siteverify",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as res:  # noqa: S310 - Google reCAPTCHA endpoint
            data = json.loads(res.read(16 * 1024).decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo verificar reCAPTCHA: %s", exc)
        return False, {"error": "verification_failed"}
    score = float(data.get("score") or 0.0)
    returned_action = str(data.get("action") or "")
    action_ok = not returned_action or returned_action == action
    return bool(data.get("success")) and action_ok and score >= RECAPTCHA_SCORE_THRESHOLD, data


async def _enforce_captcha_if_needed(request: Request, payload: dict, action: str) -> None:
    if not RECAPTCHA_SECRET_KEY:
        return
    token = str((payload or {}).get("captcha_token") or "").strip()
    if not token and not _is_suspicious_request(request):
        return
    if not token:
        meta = _request_meta(request, event_type="captcha_required", status_code=403)
        await audit_store.log_event(meta, payload={"action": action})
        raise HTTPException(status_code=403, detail="Captcha requerido.")

    ok, result = await asyncio.to_thread(_verify_recaptcha_sync, token, action)
    if ok:
        return
    meta = _request_meta(request, event_type="captcha_failed", status_code=403)
    await audit_store.log_event(
        meta,
        payload={"action": action, "score": result.get("score"), "errors": result.get("error-codes", [])},
    )
    raise HTTPException(status_code=403, detail="Captcha no válido.")


@app.middleware("http")
async def firewall_and_audit_middleware(request: Request, call_next):
    path = request.url.path
    client_ip = _request_client_ip(request)

    if client_ip and await audit_store.is_ip_blocked(client_ip):
        meta = _request_meta(request, event_type="blocked_request", status_code=403)
        await audit_store.log_event(meta, payload={"reason": "ip_blocked"})
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Acceso denegado"}, status_code=403)
        return HTMLResponse("<h1>403 - Acceso denegado</h1>", status_code=403)

    await _current_auth(request)

    try:
        response = await call_next(request)
    except Exception:
        meta = _request_meta(request, event_type="server_error", status_code=500)
        await audit_store.log_event(meta, payload={"error": "unhandled_exception"})
        raise

    if not path.startswith("/static/"):
        event_type = "page_view" if request.method == "GET" and not path.startswith("/api/") else "api_request"
        meta = _request_meta(request, event_type=event_type, status_code=response.status_code)
        await audit_store.log_event(meta, payload={})
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", _template_context(request))


@app.get("/cuenta", response_class=HTMLResponse)
async def account_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("account.html", _template_context(request))


@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request) -> HTMLResponse:
    _require_admin(request)
    meta = _request_meta(request, event_type="admin_access", status_code=200)
    await audit_store.log_event(meta, payload={"panel": "main"})
    return templates.TemplateResponse("admin.html", _template_context(request))


@app.get("/terminos", response_class=HTMLResponse)
async def terms_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("terms.html", _template_context(request))


@app.get("/privacidad", response_class=HTMLResponse)
async def privacy_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("privacy.html", _template_context(request))


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "version": app.version, "database_enabled": audit_store.enabled}


@app.get("/api/admin-eligible")
async def admin_eligible(request: Request) -> dict:
    return {"eligible": _is_admin_request(request)}


@app.get("/api/auth/me")
async def auth_me(request: Request) -> JSONResponse:
    auth = await _current_auth(request)
    user = auth.get("user") if isinstance(auth, dict) else None
    return JSONResponse({"ok": True, "user": user})


@app.post("/api/auth/register")
async def auth_register(request: Request, payload: dict) -> JSONResponse:
    if not audit_store.enabled:
        raise HTTPException(status_code=503, detail="Registro no disponible temporalmente.")
    username = _clean_username((payload or {}).get("username"))
    email = str((payload or {}).get("email") or "").strip()[:160]
    email_normalized = _normalize_email(email)
    password = _validate_password((payload or {}).get("password"))
    if len(username) < 2:
        raise HTTPException(status_code=400, detail="El usuario debe tener al menos 2 caracteres.")
    if not re.fullmatch(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email_normalized):
        raise HTTPException(status_code=400, detail="Email inválido.")
    digest = _hash_password(password)
    try:
        user = await audit_store.create_user(
            user_id=str(uuid.uuid4()),
            username=username,
            email=email,
            email_normalized=email_normalized,
            password_hash=digest["hash"],
            password_salt=digest["salt"],
            password_iterations=digest["iterations"],
            created_ip=_request_public_ip(request)[:80],
            created_user_agent=(request.headers.get("user-agent", "") or "")[:700],
        )
    except Exception as exc:  # noqa: BLE001 - ocultamos detalles internos de unicidad/DB.
        detail = str(exc).lower()
        if "duplicate" in detail or "unique" in detail:
            raise HTTPException(status_code=409, detail="Ese usuario o email ya existe.") from exc
        raise
    response = JSONResponse({"ok": True, "user": user}, status_code=201)
    await _attach_user_session(request, response, user)
    meta = _request_meta(request, event_type="register", status_code=201)
    await audit_store.log_event(meta, payload={"email": email_normalized})
    return response


@app.post("/api/auth/login")
async def auth_login(request: Request, payload: dict) -> JSONResponse:
    if not audit_store.enabled:
        raise HTTPException(status_code=503, detail="Login no disponible temporalmente.")
    email_normalized = _normalize_email((payload or {}).get("email"))
    password = str((payload or {}).get("password") or "")
    user = await audit_store.find_user_by_email(email_normalized)
    if not user or not _verify_password(password, user):
        raise HTTPException(status_code=401, detail="Email o contraseña incorrectos.")
    public_user = {k: user[k] for k in ("id", "username", "email", "role", "created_at")}
    response = JSONResponse({"ok": True, "user": public_user})
    await _attach_user_session(request, response, public_user)
    meta = _request_meta(request, event_type="login", status_code=200)
    await audit_store.log_event(meta, payload={"email": email_normalized})
    return response


@app.post("/api/auth/logout")
async def auth_logout(request: Request) -> JSONResponse:
    auth = await _current_auth(request)
    session = auth.get("session") if isinstance(auth, dict) else None
    user = auth.get("user") if isinstance(auth, dict) else None
    if isinstance(session, dict) and isinstance(user, dict):
        await audit_store.revoke_session(user_id=str(user["id"]), session_id=str(session["id"]))
    response = JSONResponse({"ok": True})
    _clear_user_cookie(response)
    return response


@app.get("/api/account/history")
async def account_history(request: Request) -> JSONResponse:
    user = await _require_user(request)
    events = await audit_store.user_history(str(user["id"]))
    return JSONResponse({"ok": True, "events": events})


@app.get("/api/account/sessions")
async def account_sessions(request: Request) -> JSONResponse:
    user = await _require_user(request)
    auth = await _current_auth(request)
    current_session = (auth or {}).get("session") if isinstance(auth, dict) else None
    sessions = await audit_store.list_user_sessions(str(user["id"]))
    return JSONResponse(
        {
            "ok": True,
            "sessions": sessions,
            "current_session_id": current_session.get("id") if isinstance(current_session, dict) else None,
        }
    )


@app.post("/api/account/sessions/logout-others")
async def account_logout_others(request: Request) -> JSONResponse:
    user = await _require_user(request)
    auth = await _current_auth(request)
    session = (auth or {}).get("session") if isinstance(auth, dict) else None
    if not isinstance(session, dict):
        raise HTTPException(status_code=401, detail="Sesión no válida.")
    count = await audit_store.revoke_other_sessions(user_id=str(user["id"]), keep_session_id=str(session["id"]))
    return JSONResponse({"ok": True, "revoked": count})


@app.post("/api/account/sessions/{session_id}/revoke")
async def account_revoke_session(request: Request, session_id: str) -> JSONResponse:
    user = await _require_user(request)
    try:
        normalized = str(uuid.UUID(session_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Sesión inválida.") from exc
    removed = await audit_store.revoke_session(user_id=str(user["id"]), session_id=normalized)
    if not removed:
        raise HTTPException(status_code=404, detail="Sesión no encontrada.")
    return JSONResponse({"ok": True})


@app.post("/api/admin-shortcut")
async def admin_shortcut(request: Request, payload: dict) -> JSONResponse:
    if not ADMIN_ENTRY_SECRET:
        raise HTTPException(status_code=404, detail="No encontrado")
    secret = str((payload or {}).get("secret") or "")
    ok = hmac.compare_digest(secret, ADMIN_ENTRY_SECRET)
    meta = _request_meta(request, event_type="admin_shortcut", status_code=200 if ok else 403)
    await audit_store.log_event(meta, payload={"ok": ok, "session": ok})
    if not ok:
        raise HTTPException(status_code=403, detail="No autorizado")
    response = JSONResponse({"ok": True, "redirect": "/admin"})
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        _create_admin_session_token(),
        max_age=ADMIN_SESSION_TTL_SECONDS,
        httponly=True,
        secure=_is_secure_cookie_request(request),
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/telemetry")
async def browser_telemetry(request: Request, payload: dict) -> JSONResponse:
    browser = (payload or {}).get("browser", {})
    page = (payload or {}).get("page", "")
    consent = bool((payload or {}).get("consent_accepted", False))
    if len(page) > 200:
        raise HTTPException(status_code=400, detail="Ruta inválida")

    meta = _request_meta(request, event_type="browser_telemetry", status_code=201)
    await audit_store.log_event(
        meta,
        payload={
            "page": page,
            "consent_accepted": consent,
            "browser": browser if isinstance(browser, dict) else {},
        },
    )
    return JSONResponse({"ok": True}, status_code=201)


@app.get("/api/admin/overview")
async def admin_overview(request: Request) -> JSONResponse:
    _require_admin(request)
    data = await audit_store.get_overview(limit=250)
    return JSONResponse(data)


@app.post("/api/admin/block-ip")
async def admin_block_ip(request: Request, payload: dict) -> JSONResponse:
    _require_admin(request)
    ip_raw = ((payload or {}).get("ip") or "").strip()
    reason = ((payload or {}).get("reason") or "Bloqueo manual desde panel admin").strip()
    if not ip_raw:
        raise HTTPException(status_code=400, detail="Falta IP.")
    try:
        normalized_ip = str(ip_address(ip_raw))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="IP inválida.") from exc
    if normalized_ip == ADMIN_LAN_IP:
        raise HTTPException(status_code=400, detail="No puedes bloquear tu IP de administrador.")
    blocked_by = _request_client_ip(request) or "admin"
    await audit_store.block_ip(normalized_ip, reason, blocked_by)

    meta = _request_meta(request, event_type="admin_block_ip", status_code=201)
    await audit_store.log_event(meta, payload={"target_ip": normalized_ip, "reason": reason})
    return JSONResponse({"ok": True}, status_code=201)


@app.post("/api/admin/unblock-ip")
async def admin_unblock_ip(request: Request, payload: dict) -> JSONResponse:
    _require_admin(request)
    ip_raw = ((payload or {}).get("ip") or "").strip()
    if not ip_raw:
        raise HTTPException(status_code=400, detail="Falta IP.")
    try:
        normalized_ip = str(ip_address(ip_raw))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="IP inválida.") from exc
    removed = await audit_store.unblock_ip(normalized_ip)
    if not removed:
        raise HTTPException(status_code=404, detail="IP no estaba bloqueada.")
    meta = _request_meta(request, event_type="admin_unblock_ip", status_code=200)
    await audit_store.log_event(meta, payload={"target_ip": normalized_ip})
    return JSONResponse({"ok": True})


@app.post("/api/admin/forget-client")
async def admin_forget_client(request: Request, payload: dict) -> JSONResponse:
    _require_admin(request)
    ip_raw = ((payload or {}).get("ip") or "").strip()
    user_agent = str((payload or {}).get("user_agent") or "").strip()
    if not ip_raw or ip_raw == "unknown" or not user_agent:
        raise HTTPException(status_code=400, detail="Faltan datos de la sesión.")
    try:
        normalized_ip = str(ip_address(ip_raw))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="IP inválida.") from exc
    deleted = await audit_store.forget_client_events(normalized_ip, user_agent)
    meta = _request_meta(request, event_type="admin_forget_client", status_code=200)
    await audit_store.log_event(meta, payload={"target_ip": normalized_ip, "deleted_events": deleted})
    return JSONResponse({"ok": True, "deleted_events": deleted})


@app.post("/api/metadata")
async def metadata(request: Request, payload: dict) -> JSONResponse:
    url = (payload or {}).get("url", "").strip()
    if not _is_valid_youtube_url(url):
        raise HTTPException(status_code=400, detail="URL inválida")
    await _enforce_captcha_if_needed(request, payload, "metadata")
    try:
        info = await service.fetch_metadata(url)
    except ConversionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    meta = _request_meta(request, event_type="metadata", status_code=200)
    await audit_store.log_event(
        meta,
        payload={
            "url": url,
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "segments": len(info.get("segments") or []),
        },
    )
    return JSONResponse(info)


@app.post("/api/transcript")
async def transcript(request: Request, payload: dict) -> JSONResponse:
    url = (payload or {}).get("url", "").strip()
    language = (payload or {}).get("language", "").strip()[:12]
    if not _is_valid_youtube_url(url):
        raise HTTPException(status_code=400, detail="URL inválida")
    await _enforce_captcha_if_needed(request, payload, "transcript")
    try:
        data = await service.fetch_transcript(url, preferred_language=language)
    except ConversionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    meta = _request_meta(request, event_type="transcript", status_code=200)
    await audit_store.log_event(
        meta,
        payload={
            "url": url,
            "language": data.get("language"),
            "source": data.get("source"),
            "characters": data.get("characters"),
        },
    )
    return JSONResponse(data)


@app.post("/api/convert")
async def convert(request: Request, payload: dict) -> JSONResponse:
    url = (payload or {}).get("url", "").strip()
    format_key = (payload or {}).get("format", "mp3-192").strip()
    raw_segment_index = (payload or {}).get("segment_index")
    segment_index: int | None = None
    if raw_segment_index is not None:
        try:
            segment_index = int(raw_segment_index)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Parte inválida") from exc
    if not _is_valid_youtube_url(url):
        raise HTTPException(status_code=400, detail="URL inválida")
    await _enforce_captcha_if_needed(request, payload, "convert")
    if not re.fullmatch(
        r"(mp3-(128|192|320)|video-(360p|480p|720p|720p60|1080p|1080p60|1440p|1440p60|2160p|2160p60|4320p|4320p60))",
        format_key,
    ):
        raise HTTPException(status_code=400, detail="Formato no soportado")
    try:
        job_id = await service.start_job(url, format_key=format_key, segment_index=segment_index)
    except ConversionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    meta = _request_meta(request, event_type="convert", status_code=202)
    await audit_store.log_event(
        meta,
        payload={"job_id": job_id, "url": url, "format_key": format_key, "segment_index": segment_index},
    )
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
async def download(request: Request, job_id: str) -> FileResponse:
    if not re.fullmatch(r"[a-f0-9\-]{8,40}", job_id):
        raise HTTPException(status_code=400, detail="ID inválido")
    try:
        path, filename, mime = service.file_for(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=410, detail="Archivo expirado") from exc

    job = service.jobs.get(job_id)
    meta = _request_meta(request, event_type="download", status_code=200)
    await audit_store.log_event(
        meta,
        payload={
            "job_id": job_id,
            "url": job.url if job else "",
            "title": job.title if job else "",
            "format_key": job.format_key if job else "",
            "filename": filename,
        },
    )

    background = service.schedule_cleanup(job_id)
    return FileResponse(
        path=path,
        filename=filename,
        media_type=mime,
        background=background,
    )


@app.on_event("startup")
async def on_startup() -> None:
    await audit_store.init_schema()
    asyncio.create_task(service.janitor_loop())
    log.info(
        "VideoDrop listo — work_dir=%s max_duration=%ss db_enabled=%s",
        WORK_DIR,
        MAX_DURATION,
        audit_store.enabled,
    )


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await service.shutdown()
