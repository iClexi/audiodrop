(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const requests24h = $("requests-24h");
  const downloads24h = $("downloads-24h");
  const eventsBody = $("events-table")?.querySelector("tbody");
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

  const refresh = async () => {
    const res = await fetch("/api/admin/overview", { credentials: "same-origin" });
    if (!res.ok) {
      throw new Error("No se pudo cargar el panel.");
    }
    const data = await res.json();
    requests24h.textContent = String(data?.summary?.requests_24h ?? 0);
    downloads24h.textContent = String(data?.summary?.downloads_24h ?? 0);
    renderBlocked(data?.blocked_ips || []);
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
