"""Persistencia de auditoria y bloqueo de IP para VideoDrop (PostgreSQL)."""
from __future__ import annotations

import asyncio
import json
import logging
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
            payload JSONB NOT NULL DEFAULT '{}'::jsonb
        );

        CREATE INDEX IF NOT EXISTS idx_audit_events_created_at
            ON audit_events (created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_events_event_type
            ON audit_events (event_type);
        CREATE INDEX IF NOT EXISTS idx_audit_events_client_ip
            ON audit_events (client_ip);
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
            request_scheme, payload
        ) VALUES (
            %(event_type)s, %(method)s, %(path)s, %(query_string)s, %(status_code)s,
            %(client_ip)s, %(public_ip)s, %(user_agent)s, %(referer)s, %(request_host)s,
            %(request_scheme)s, %(payload)s::jsonb
        );
        """
        params = dict(meta)
        params["payload"] = json.dumps(payload, ensure_ascii=False)
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
        except psycopg.Error:
            log.exception("No se pudo guardar evento de auditoria.")

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
            id, created_at, event_type, method, path, query_string, status_code,
            client_ip, public_ip, user_agent, referer, request_host, request_scheme, payload
        FROM audit_events
        ORDER BY created_at DESC
        LIMIT %s;
        """
        summary_sql = """
        SELECT
            COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours') AS requests_24h,
            COUNT(*) FILTER (WHERE event_type = 'download' AND created_at >= NOW() - INTERVAL '24 hours') AS downloads_24h
        FROM audit_events;
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(events_sql, (limit,))
            event_rows = cur.fetchall()
            cur.execute(summary_sql)
            summary_row = cur.fetchone()
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
        return {
            "events": events,
            "blocked_ips": blocked_ips,
            "summary": {
                "requests_24h": int(summary_row[0] or 0),
                "downloads_24h": int(summary_row[1] or 0),
            },
        }
