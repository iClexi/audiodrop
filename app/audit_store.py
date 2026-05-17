"""Persistencia de auditoria y bloqueo de IP para VideoDrop (PostgreSQL)."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import psycopg

log = logging.getLogger("audiodrop.audit")


class AuditStore:
    """Capa de acceso a datos para eventos de trafico y lista de bloqueo."""

    def __init__(self, database_url: str | None) -> None:
        self.database_url = (database_url or "").strip()
        self.enabled = bool(self.database_url)

    def _connect(self) -> psycopg.Connection[Any]:
        if not self.enabled:
            raise RuntimeError("AUDIODROP_DATABASE_URL no configurado.")
        return psycopg.connect(self.database_url, autocommit=True, connect_timeout=5)

    async def init_schema(self) -> None:
        if not self.enabled:
            log.warning("Auditoria desactivada: falta AUDIODROP_DATABASE_URL.")
            return
        await asyncio.to_thread(self._init_schema_sync)

    def _init_schema_sync(self) -> None:
        ddl = """
        CREATE TABLE IF NOT EXISTS users (
            id UUID PRIMARY KEY,
            username TEXT NOT NULL,
            email TEXT NOT NULL,
            email_normalized TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            password_iterations INTEGER NOT NULL DEFAULT 210000,
            role TEXT NOT NULL DEFAULT 'user',
            created_ip TEXT NOT NULL DEFAULT '',
            created_user_agent TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower_idx
            ON users (LOWER(username));
        CREATE INDEX IF NOT EXISTS users_created_at_idx
            ON users (created_at DESC);

        CREATE TABLE IF NOT EXISTS user_sessions (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            device_label TEXT NOT NULL DEFAULT '',
            ip TEXT NOT NULL DEFAULT '',
            user_agent TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ
        );

        CREATE INDEX IF NOT EXISTS user_sessions_user_id_idx
            ON user_sessions (user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS user_sessions_token_hash_idx
            ON user_sessions (token_hash);

        CREATE TABLE IF NOT EXISTS blocked_ips (
            ip TEXT PRIMARY KEY,
            reason TEXT NOT NULL,
            blocked_by TEXT NOT NULL DEFAULT 'admin',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS audit_events (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            event_type TEXT NOT NULL,
            method TEXT,
            path TEXT,
            query_string TEXT,
            status_code INTEGER,
            client_ip TEXT,
            public_ip TEXT,
            user_agent TEXT,
            referer TEXT,
            request_host TEXT,
            request_scheme TEXT,
            user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb
        );

        ALTER TABLE audit_events
            ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE SET NULL;

        CREATE INDEX IF NOT EXISTS idx_audit_events_created_at
            ON audit_events (created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_events_event_type
            ON audit_events (event_type);
        CREATE INDEX IF NOT EXISTS idx_audit_events_client_ip
            ON audit_events (client_ip);
        CREATE INDEX IF NOT EXISTS idx_audit_events_user_id
            ON audit_events (user_id, created_at DESC);
        """
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(ddl)
        except psycopg.Error:
            log.exception("No se pudo inicializar el esquema de auditoria.")
            raise

    async def log_event(self, meta: dict[str, Any], payload: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        await asyncio.to_thread(self._log_event_sync, meta, payload or {})

    def _log_event_sync(self, meta: dict[str, Any], payload: dict[str, Any]) -> None:
        sql = """
        INSERT INTO audit_events (
            event_type, method, path, query_string, status_code,
            client_ip, public_ip, user_agent, referer, request_host,
            request_scheme, user_id, payload
        ) VALUES (
            %(event_type)s, %(method)s, %(path)s, %(query_string)s, %(status_code)s,
            %(client_ip)s, %(public_ip)s, %(user_agent)s, %(referer)s, %(request_host)s,
            %(request_scheme)s, %(user_id)s, %(payload)s::jsonb
        );
        """
        params = dict(meta)
        params.setdefault("user_id", None)
        params["payload"] = json.dumps(payload, ensure_ascii=False)
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
        except psycopg.Error:
            log.exception("No se pudo guardar evento de auditoria.")

    async def create_user(
        self,
        *,
        user_id: str,
        username: str,
        email: str,
        email_normalized: str,
        password_hash: str,
        password_salt: str,
        password_iterations: int,
        created_ip: str,
        created_user_agent: str,
        role: str = "user",
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Base de datos no configurada.")
        return await asyncio.to_thread(
            self._create_user_sync,
            user_id,
            username,
            email,
            email_normalized,
            password_hash,
            password_salt,
            password_iterations,
            created_ip,
            created_user_agent,
            role,
        )

    def _create_user_sync(
        self,
        user_id: str,
        username: str,
        email: str,
        email_normalized: str,
        password_hash: str,
        password_salt: str,
        password_iterations: int,
        created_ip: str,
        created_user_agent: str,
        role: str,
    ) -> dict[str, Any]:
        sql = """
        INSERT INTO users (
            id, username, email, email_normalized, password_hash, password_salt,
            password_iterations, role, created_ip, created_user_agent
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id, username, email, role, created_at;
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    user_id,
                    username,
                    email,
                    email_normalized,
                    password_hash,
                    password_salt,
                    password_iterations,
                    role,
                    created_ip,
                    created_user_agent,
                ),
            )
            row = cur.fetchone()
        return self._public_user(row)

    async def find_user_by_email(self, email_normalized: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        return await asyncio.to_thread(self._find_user_by_email_sync, email_normalized)

    def _find_user_by_email_sync(self, email_normalized: str) -> dict[str, Any] | None:
        sql = """
        SELECT id, username, email, role, created_at, password_hash, password_salt, password_iterations
        FROM users
        WHERE email_normalized = %s
        LIMIT 1;
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (email_normalized,))
            row = cur.fetchone()
        if not row:
            return None
        user = self._public_user(row[:5])
        user.update(
            {
                "password_hash": row[5],
                "password_salt": row[6],
                "password_iterations": row[7],
            }
        )
        return user

    async def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        token_hash: str,
        device_label: str,
        ip: str,
        user_agent: str,
        expires_at: datetime,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Base de datos no configurada.")
        return await asyncio.to_thread(
            self._create_session_sync,
            session_id,
            user_id,
            token_hash,
            device_label,
            ip,
            user_agent,
            expires_at,
        )

    def _create_session_sync(
        self,
        session_id: str,
        user_id: str,
        token_hash: str,
        device_label: str,
        ip: str,
        user_agent: str,
        expires_at: datetime,
    ) -> dict[str, Any]:
        sql = """
        INSERT INTO user_sessions (id, user_id, token_hash, device_label, ip, user_agent, expires_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        RETURNING id, user_id, device_label, ip, user_agent, created_at, last_seen_at, expires_at, revoked_at;
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (session_id, user_id, token_hash, device_label, ip, user_agent, expires_at))
            row = cur.fetchone()
        return self._session_row(row)

    async def auth_for_token(self, token_hash: str) -> dict[str, Any] | None:
        if not self.enabled or not token_hash:
            return None
        return await asyncio.to_thread(self._auth_for_token_sync, token_hash)

    def _auth_for_token_sync(self, token_hash: str) -> dict[str, Any] | None:
        sql = """
        SELECT
            s.id, s.user_id, s.device_label, s.ip, s.user_agent, s.created_at,
            s.last_seen_at, s.expires_at, s.revoked_at,
            u.id, u.username, u.email, u.role, u.created_at
        FROM user_sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = %s
          AND s.revoked_at IS NULL
          AND s.expires_at > NOW()
        LIMIT 1;
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (token_hash,))
            row = cur.fetchone()
            if not row:
                return None
            cur.execute("UPDATE user_sessions SET last_seen_at = NOW() WHERE id = %s;", (row[0],))
        return {
            "session": self._session_row(row[:9]),
            "user": self._public_user(row[9:14]),
        }

    async def revoke_session(self, *, user_id: str, session_id: str) -> bool:
        if not self.enabled:
            raise RuntimeError("Base de datos no configurada.")
        return await asyncio.to_thread(self._revoke_session_sync, user_id, session_id)

    def _revoke_session_sync(self, user_id: str, session_id: str) -> bool:
        sql = """
        UPDATE user_sessions
        SET revoked_at = NOW()
        WHERE id = %s AND user_id = %s AND revoked_at IS NULL;
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (session_id, user_id))
            return bool(cur.rowcount)

    async def revoke_other_sessions(self, *, user_id: str, keep_session_id: str) -> int:
        if not self.enabled:
            raise RuntimeError("Base de datos no configurada.")
        return await asyncio.to_thread(self._revoke_other_sessions_sync, user_id, keep_session_id)

    def _revoke_other_sessions_sync(self, user_id: str, keep_session_id: str) -> int:
        sql = """
        UPDATE user_sessions
        SET revoked_at = NOW()
        WHERE user_id = %s AND id <> %s AND revoked_at IS NULL;
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (user_id, keep_session_id))
            return int(cur.rowcount or 0)

    async def list_user_sessions(self, user_id: str) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        return await asyncio.to_thread(self._list_user_sessions_sync, user_id)

    def _list_user_sessions_sync(self, user_id: str) -> list[dict[str, Any]]:
        sql = """
        SELECT id, user_id, device_label, ip, user_agent, created_at, last_seen_at, expires_at, revoked_at
        FROM user_sessions
        WHERE user_id = %s
        ORDER BY last_seen_at DESC
        LIMIT 80;
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (user_id,))
            rows = cur.fetchall()
        return [self._session_row(row) for row in rows]

    async def user_history(self, user_id: str, limit: int = 120) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        return await asyncio.to_thread(self._user_history_sync, user_id, limit)

    def _user_history_sync(self, user_id: str, limit: int) -> list[dict[str, Any]]:
        sql = """
        SELECT id, created_at, event_type, path, status_code, payload, request_host, request_scheme
        FROM audit_events
        WHERE user_id = %s
          AND event_type IN (
              'metadata', 'transcript', 'convert', 'download',
              'browser_telemetry', 'admin_access', 'login', 'register'
          )
        ORDER BY created_at DESC
        LIMIT %s;
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (user_id, limit))
            rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "created_at": r[1].isoformat() if r[1] else None,
                "event_type": r[2],
                "path": r[3],
                "status_code": r[4],
                "payload": r[5] or {},
                "request_host": r[6],
                "request_scheme": r[7],
            }
            for r in rows
        ]

    async def is_ip_blocked(self, ip: str) -> bool:
        if not self.enabled or not ip:
            return False
        return await asyncio.to_thread(self._is_ip_blocked_sync, ip)

    def _is_ip_blocked_sync(self, ip: str) -> bool:
        sql = "SELECT 1 FROM blocked_ips WHERE ip = %s LIMIT 1;"
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, (ip,))
                return cur.fetchone() is not None
        except psycopg.Error:
            log.exception("No se pudo consultar bloqueo para IP=%s", ip)
            return False

    async def block_ip(self, ip: str, reason: str, blocked_by: str) -> None:
        if not self.enabled:
            raise RuntimeError("Base de datos no configurada.")
        await asyncio.to_thread(self._block_ip_sync, ip, reason, blocked_by)

    def _block_ip_sync(self, ip: str, reason: str, blocked_by: str) -> None:
        sql = """
        INSERT INTO blocked_ips (ip, reason, blocked_by)
        VALUES (%s, %s, %s)
        ON CONFLICT (ip)
        DO UPDATE SET reason = EXCLUDED.reason, blocked_by = EXCLUDED.blocked_by, created_at = NOW();
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (ip, reason, blocked_by))

    async def unblock_ip(self, ip: str) -> bool:
        if not self.enabled:
            raise RuntimeError("Base de datos no configurada.")
        return await asyncio.to_thread(self._unblock_ip_sync, ip)

    def _unblock_ip_sync(self, ip: str) -> bool:
        sql = "DELETE FROM blocked_ips WHERE ip = %s;"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (ip,))
            return cur.rowcount > 0

    async def forget_client_events(self, ip: str, user_agent: str) -> int:
        if not self.enabled:
            raise RuntimeError("Base de datos no configurada.")
        return await asyncio.to_thread(self._forget_client_events_sync, ip, user_agent)

    def _forget_client_events_sync(self, ip: str, user_agent: str) -> int:
        sql = """
        DELETE FROM audit_events
        WHERE COALESCE(NULLIF(public_ip, ''), NULLIF(client_ip, ''), 'unknown') = %s
          AND COALESCE(NULLIF(user_agent, ''), 'unknown') = %s;
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (ip, user_agent))
            return int(cur.rowcount or 0)

    async def list_blocked_ips(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        return await asyncio.to_thread(self._list_blocked_ips_sync)

    def _list_blocked_ips_sync(self) -> list[dict[str, Any]]:
        sql = """
        SELECT ip, reason, blocked_by, created_at
        FROM blocked_ips
        ORDER BY created_at DESC;
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return [
            {
                "ip": r[0],
                "reason": r[1],
                "blocked_by": r[2],
                "created_at": r[3].isoformat() if r[3] else None,
            }
            for r in rows
        ]

    async def get_overview(self, limit: int = 200) -> dict[str, Any]:
        if not self.enabled:
            return {"events": [], "blocked_ips": [], "summary": {"requests_24h": 0, "downloads_24h": 0}}
        return await asyncio.to_thread(self._get_overview_sync, limit)

    def _get_overview_sync(self, limit: int) -> dict[str, Any]:
        events_sql = """
        SELECT
            e.id, e.created_at, e.event_type, e.method, e.path, e.query_string, e.status_code,
            e.client_ip, e.public_ip, e.user_agent, e.referer, e.request_host, e.request_scheme,
            e.payload, e.user_id, u.username, u.email
        FROM audit_events e
        LEFT JOIN users u ON u.id = e.user_id
        ORDER BY e.created_at DESC
        LIMIT %s;
        """
        summary_sql = """
        SELECT
            COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours') AS requests_24h,
            COUNT(*) FILTER (WHERE event_type = 'download' AND created_at >= NOW() - INTERVAL '24 hours') AS downloads_24h,
            COUNT(DISTINCT COALESCE(public_ip, client_ip, '') || '|' || COALESCE(user_agent, ''))
                FILTER (WHERE created_at >= NOW() - INTERVAL '10 minutes') AS active_clients_10m
        FROM audit_events;
        """
        users_sql = "SELECT COUNT(*) FROM users;"
        clients_sql = """
        WITH base AS (
            SELECT
                COALESCE(NULLIF(public_ip, ''), NULLIF(client_ip, ''), 'unknown') AS ip,
                COALESCE(NULLIF(user_agent, ''), 'unknown') AS user_agent,
                created_at,
                path,
                payload
            FROM audit_events
            WHERE created_at >= NOW() - INTERVAL '30 minutes'
        ),
        ranked AS (
            SELECT
                ip,
                user_agent,
                created_at,
                path,
                payload,
                COUNT(*) OVER (PARTITION BY ip, user_agent) AS events_count,
                MAX(created_at) OVER (PARTITION BY ip, user_agent) AS last_seen,
                ROW_NUMBER() OVER (PARTITION BY ip, user_agent ORDER BY created_at DESC) AS rn
            FROM base
        )
        SELECT ip, user_agent, last_seen, events_count, path, payload
        FROM ranked
        WHERE rn = 1
        ORDER BY last_seen DESC
        LIMIT 80;
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(events_sql, (limit,))
            event_rows = cur.fetchall()
            cur.execute(summary_sql)
            summary_row = cur.fetchone()
            cur.execute(users_sql)
            users_row = cur.fetchone()
            cur.execute(clients_sql)
            client_rows = cur.fetchall()
            cur.execute(
                "SELECT ip, reason, blocked_by, created_at FROM blocked_ips ORDER BY created_at DESC;"
            )
            blocked_rows = cur.fetchall()
        events = []
        for r in event_rows:
            events.append(
                {
                    "id": r[0],
                    "created_at": r[1].isoformat() if r[1] else None,
                    "event_type": r[2],
                    "method": r[3],
                    "path": r[4],
                    "query_string": r[5],
                    "status_code": r[6],
                    "client_ip": r[7],
                    "public_ip": r[8],
                    "user_agent": r[9],
                    "referer": r[10],
                    "request_host": r[11],
                    "request_scheme": r[12],
                    "payload": r[13] or {},
                    "user_id": str(r[14]) if r[14] else None,
                    "username": r[15],
                    "user_email": r[16],
                }
            )
        blocked_ips = [
            {
                "ip": r[0],
                "reason": r[1],
                "blocked_by": r[2],
                "created_at": r[3].isoformat() if r[3] else None,
            }
            for r in blocked_rows
        ]
        active_clients = [
            {
                "ip": r[0],
                "user_agent": r[1],
                "last_seen": r[2].isoformat() if r[2] else None,
                "events_count": int(r[3] or 0),
                "last_path": r[4],
                "payload": r[5] or {},
            }
            for r in client_rows
        ]
        return {
            "events": events,
            "blocked_ips": blocked_ips,
            "active_clients": active_clients,
            "summary": {
                "requests_24h": int(summary_row[0] or 0),
                "downloads_24h": int(summary_row[1] or 0),
                "active_clients_10m": int(summary_row[2] or 0),
                "users_total": int((users_row or [0])[0] or 0),
            },
        }

    @staticmethod
    def _iso(value: Any) -> str | None:
        if not value:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.isoformat()
        return str(value)

    @classmethod
    def _public_user(cls, row: Any) -> dict[str, Any]:
        return {
            "id": str(row[0]),
            "username": row[1],
            "email": row[2],
            "role": row[3],
            "created_at": cls._iso(row[4]),
        }

    @classmethod
    def _session_row(cls, row: Any) -> dict[str, Any]:
        return {
            "id": str(row[0]),
            "user_id": str(row[1]),
            "device_label": row[2],
            "ip": row[3],
            "user_agent": row[4],
            "created_at": cls._iso(row[5]),
            "last_seen_at": cls._iso(row[6]),
            "expires_at": cls._iso(row[7]),
            "revoked_at": cls._iso(row[8]),
        }
