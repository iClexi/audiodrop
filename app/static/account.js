(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const summary = $("account-summary");
  const historyList = $("history-list");
  const sessionsList = $("sessions-list");
  const logout = $("account-logout");
  const logoutOthers = $("logout-others");
  let currentSessionId = "";

  const escapeHtml = (value) =>
    String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");

  const fmtDate = (iso) => {
    if (!iso) return "—";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    return date.toLocaleString();
  };

  const actionLabel = (event) => {
    const map = {
      metadata: "Consultó video",
      transcript: "Desgrabó texto",
      convert: "Preparó descarga",
      download: "Descargó archivo",
      browser_telemetry: "Abrió VideoDrop",
      login: "Inició sesión",
      register: "Creó cuenta",
      admin_access: "Entró al admin",
    };
    return map[event] || event;
  };

  const renderHistory = (events) => {
    if (!historyList) return;
    if (!events.length) {
      historyList.innerHTML = `<p class="muted">Todavía no hay actividad guardada en esta cuenta.</p>`;
      return;
    }
    historyList.innerHTML = events.map((event) => {
      const payload = event.payload || {};
      const title = payload.title || payload.filename || payload.url || event.path || "VideoDrop";
      const detail = [
        payload.format_key,
        payload.language,
        payload.source,
        payload.characters ? `${payload.characters} caracteres` : "",
      ].filter(Boolean).join(" · ");
      const url = payload.url ? `<a href="${escapeHtml(payload.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(payload.url)}</a>` : "";
      return `
        <article class="history-item">
          <div>
            <strong>${escapeHtml(actionLabel(event.event_type))}</strong>
            <span>${escapeHtml(fmtDate(event.created_at))}</span>
          </div>
          <p>${escapeHtml(title)}</p>
          ${detail ? `<small>${escapeHtml(detail)}</small>` : ""}
          ${url ? `<small>${url}</small>` : ""}
        </article>
      `;
    }).join("");
  };

  const renderSessions = (sessions) => {
    if (!sessionsList) return;
    if (!sessions.length) {
      sessionsList.innerHTML = `<p class="muted">No hay sesiones activas.</p>`;
      return;
    }
    sessionsList.innerHTML = sessions.map((session) => {
      const revoked = !!session.revoked_at;
      const current = session.id === currentSessionId;
      return `
        <article class="session-item ${revoked ? "is-revoked" : ""}">
          <div>
            <strong>${escapeHtml(session.device_label || "Dispositivo")}${current ? " · actual" : ""}</strong>
            <span>${escapeHtml(session.ip || "IP no disponible")}</span>
          </div>
          <small>Última actividad: ${escapeHtml(fmtDate(session.last_seen_at))}</small>
          <small>Creada: ${escapeHtml(fmtDate(session.created_at))}</small>
          ${revoked ? `<small>Revocada: ${escapeHtml(fmtDate(session.revoked_at))}</small>` : ""}
          <button class="ghost revoke-session" type="button" data-session="${escapeHtml(session.id)}" ${revoked || current ? "disabled" : ""}>Cerrar</button>
        </article>
      `;
    }).join("");
    sessionsList.querySelectorAll(".revoke-session").forEach((button) => {
      button.addEventListener("click", async () => {
        const id = button.getAttribute("data-session");
        if (!id) return;
        const res = await fetch(`/api/account/sessions/${encodeURIComponent(id)}/revoke`, {
          method: "POST",
          credentials: "same-origin",
        });
        if (res.ok) await loadSessions();
      });
    });
  };

  const requireMe = async () => {
    const res = await fetch("/api/auth/me", { credentials: "same-origin" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.user) {
      window.location.href = "/";
      return null;
    }
    if (summary) summary.textContent = `${data.user.username}, aquí aparece lo que haces mientras estás conectado.`;
    return data.user;
  };

  const loadHistory = async () => {
    const res = await fetch("/api/account/history", { credentials: "same-origin" });
    const data = await res.json().catch(() => ({}));
    if (res.ok) renderHistory(data.events || []);
  };

  async function loadSessions() {
    const res = await fetch("/api/account/sessions", { credentials: "same-origin" });
    const data = await res.json().catch(() => ({}));
    if (res.ok) {
      currentSessionId = data.current_session_id || "";
      renderSessions(data.sessions || []);
    }
  }

  logout?.addEventListener("click", async () => {
    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" }).catch(() => {});
    window.location.href = "/";
  });

  logoutOthers?.addEventListener("click", async () => {
    const res = await fetch("/api/account/sessions/logout-others", {
      method: "POST",
      credentials: "same-origin",
    });
    if (res.ok) await loadSessions();
  });

  requireMe().then((user) => {
    if (!user) return;
    loadHistory();
    loadSessions();
  });
})();
