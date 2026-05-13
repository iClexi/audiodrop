(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const requests24h = $("requests-24h");
  const downloads24h = $("downloads-24h");
  const active10m = $("active-10m");
  const eventsBody = $("events-table")?.querySelector("tbody");
  const clientsBody = $("clients-table")?.querySelector("tbody");
  const blockedBody = $("blocked-table")?.querySelector("tbody");
  const blockForm = $("block-form");
  const blockIpInput = $("block-ip");
  const blockReasonInput = $("block-reason");

  const escapeHtml = (value) =>
    String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");

  const fmtDate = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString();
  };

  const browserSummary = (client) => {
    const browser = client?.payload?.browser || {};
    const parts = [
      browser.platform,
      browser.language,
      browser.timezone,
      browser.screen?.width && browser.screen?.height
        ? `${browser.screen.width}x${browser.screen.height}`
        : "",
    ].filter(Boolean);
    return parts.length ? parts.join(" · ") : "Sin telemetría de navegador";
  };

  const blockClient = async (ip) => {
    if (!ip || ip === "unknown") {
      alert("No hay IP válida para bloquear.");
      return;
    }
    const res = await fetch("/api/admin/block-ip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ip, reason: "Bloqueo desde sesiones recientes" }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      alert(data.detail || "No se pudo bloquear la IP.");
      return;
    }
    await refresh();
  };

  const forgetClient = async (ip, userAgent) => {
    if (!ip || ip === "unknown" || !userAgent) {
      alert("No hay datos suficientes para borrar esta sesión.");
      return;
    }
    const ok = window.confirm("Borrar los eventos recientes de esta sesión del panel?");
    if (!ok) return;
    const res = await fetch("/api/admin/forget-client", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ip, user_agent: userAgent }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      alert(data.detail || "No se pudo borrar la sesión.");
      return;
    }
    await refresh();
  };

  const renderBlocked = (rows) => {
    if (!blockedBody) return;
    blockedBody.innerHTML = "";
    if (!rows.length) {
      blockedBody.innerHTML = `<tr><td colspan="4" class="muted">Sin IPs bloqueadas.</td></tr>`;
      return;
    }
    for (const row of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><code>${escapeHtml(row.ip)}</code></td>
        <td>${escapeHtml(row.reason)}</td>
        <td>${escapeHtml(fmtDate(row.created_at))}</td>
        <td><button class="ghost unblock-btn" data-ip="${escapeHtml(row.ip)}">Desbloquear</button></td>
      `;
      blockedBody.appendChild(tr);
    }
    blockedBody.querySelectorAll(".unblock-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const ip = btn.getAttribute("data-ip");
        if (!ip) return;
        const res = await fetch("/api/admin/unblock-ip", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ip }),
        });
        if (!res.ok) {
          alert("No se pudo desbloquear la IP.");
          return;
        }
        await refresh();
      });
    });
  };

  const renderEvents = (rows) => {
    if (!eventsBody) return;
    eventsBody.innerHTML = "";
    if (!rows.length) {
      eventsBody.innerHTML = `<tr><td colspan="6" class="muted">Sin eventos.</td></tr>`;
      return;
    }
    for (const ev of rows) {
      const payload = JSON.stringify(ev.payload || {});
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(fmtDate(ev.created_at))}</td>
        <td><code>${escapeHtml(ev.event_type)}</code></td>
        <td><code>${escapeHtml(ev.public_ip || ev.client_ip || "")}</code></td>
        <td>${escapeHtml(ev.path || "")}</td>
        <td>${escapeHtml(ev.status_code ?? "")}</td>
        <td class="payload-cell" title="${escapeHtml(payload)}">${escapeHtml(payload)}</td>
      `;
      eventsBody.appendChild(tr);
    }
  };

  const renderClients = (rows) => {
    if (!clientsBody) return;
    clientsBody.innerHTML = "";
    if (!rows.length) {
      clientsBody.innerHTML = `<tr><td colspan="6" class="muted">Sin sesiones recientes.</td></tr>`;
      return;
    }
    for (const client of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(fmtDate(client.last_seen))}</td>
        <td><code>${escapeHtml(client.ip || "")}</code></td>
        <td class="payload-cell client-device" title="${escapeHtml(client.user_agent || "")}">
          <strong>${escapeHtml(browserSummary(client))}</strong>
          <small>${escapeHtml(client.user_agent || "")}</small>
        </td>
        <td>${escapeHtml(client.events_count ?? 0)}</td>
        <td>${escapeHtml(client.last_path || "")}</td>
        <td>
          <div class="client-actions">
            <button class="ghost block-client" type="button">Bloquear</button>
            <button class="ghost forget-client" type="button">Olvidar</button>
          </div>
        </td>
      `;
      tr.querySelector(".block-client")?.addEventListener("click", () => blockClient(client.ip || ""));
      tr.querySelector(".forget-client")?.addEventListener("click", () => forgetClient(client.ip || "", client.user_agent || ""));
      clientsBody.appendChild(tr);
    }
  };

  const refresh = async () => {
    const res = await fetch("/api/admin/overview", { credentials: "same-origin" });
    if (!res.ok) {
      throw new Error("No se pudo cargar el panel.");
    }
    const data = await res.json();
    requests24h.textContent = String(data?.summary?.requests_24h ?? 0);
    downloads24h.textContent = String(data?.summary?.downloads_24h ?? 0);
    if (active10m) active10m.textContent = String(data?.summary?.active_clients_10m ?? 0);
    renderBlocked(data?.blocked_ips || []);
    renderClients(data?.active_clients || []);
    renderEvents(data?.events || []);
  };

  blockForm?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const ip = blockIpInput?.value.trim() || "";
    const reason = blockReasonInput?.value.trim() || "Bloqueo manual";
    if (!ip) return;
    const res = await fetch("/api/admin/block-ip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ip, reason }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      alert(data.detail || "No se pudo bloquear la IP.");
      return;
    }
    if (blockIpInput) blockIpInput.value = "";
    if (blockReasonInput) blockReasonInput.value = "";
    await refresh();
  });

  refresh().catch((err) => {
    console.error(err);
    alert("Error cargando panel admin.");
  });
})();
