(() => {
  "use strict";

  const form = document.getElementById("shortener-form");
  const urlInput = document.getElementById("target-url");
  const hint = document.getElementById("shortener-hint");
  const resultBox = document.getElementById("shortener-result");
  const shortUrlInput = document.getElementById("short-url");
  const copyBtn = document.getElementById("copy-short-url");
  const shortenBtn = document.getElementById("shorten-btn");

  const setHint = (message, level = "") => {
    hint.textContent = message || "";
    hint.classList.remove("error", "success");
    if (level) hint.classList.add(level);
  };

  const setLoading = (loading) => {
    shortenBtn.disabled = loading;
    urlInput.disabled = loading;
  };

  form?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const targetUrl = (urlInput?.value || "").trim();
    if (!/^https?:\/\/\S+$/i.test(targetUrl)) {
      setHint("URL inválida. Debe iniciar con http:// o https://", "error");
      return;
    }
    setLoading(true);
    setHint("Creando enlace corto…");
    try {
      const res = await fetch("/api/shortener/create", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: targetUrl }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setHint(data.detail || "No se pudo crear el enlace.", "error");
        return;
      }
      shortUrlInput.value = data.short_url || "";
      resultBox.classList.remove("hidden");
      setHint("Enlace creado correctamente.", "success");
    } catch {
      setHint("Error de red creando el enlace.", "error");
    } finally {
      setLoading(false);
    }
  });

  copyBtn?.addEventListener("click", async () => {
    const value = shortUrlInput.value;
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      setHint("Enlace copiado al portapapeles.", "success");
    } catch {
      setHint("No se pudo copiar automáticamente.", "error");
    }
  });
})();
