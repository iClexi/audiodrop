/* VideoDrop frontend — orquesta el DOM y habla con la API.
   Soporta audio (MP3 128/192/320) y video (hasta 8K cuando esté disponible). */
(() => {
  'use strict';

  const LEGAL_KEY = 'audiodrop-legal-v2';
  const PREVIEW_CACHE_PREFIX = 'videodrop-preview-v1:';
  const PREVIEW_CACHE_TTL = 30 * 60 * 1000;
  const legalModal = document.getElementById('legal-modal');
  const acceptLegalBtn = document.getElementById('accept-legal');

  const collectBrowserData = () => ({
    userAgent: navigator.userAgent || '',
    language: navigator.language || '',
    languages: navigator.languages || [],
    platform: navigator.platform || '',
    vendor: navigator.vendor || '',
    cookieEnabled: !!navigator.cookieEnabled,
    hardwareConcurrency: navigator.hardwareConcurrency || null,
    deviceMemory: navigator.deviceMemory || null,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
    screen: {
      width: window.screen?.width || null,
      height: window.screen?.height || null,
      colorDepth: window.screen?.colorDepth || null,
      pixelRatio: window.devicePixelRatio || null,
    },
    viewport: {
      width: window.innerWidth || null,
      height: window.innerHeight || null,
    },
  });

  const sendTelemetry = (page) =>
    fetch('/api/telemetry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        page,
        consent_accepted: true,
        browser: collectBrowserData(),
      }),
    }).catch(() => {});

  const hasAcceptedLegal = () => localStorage.getItem(LEGAL_KEY) === 'accepted';
  const markAcceptedLegal = () => localStorage.setItem(LEGAL_KEY, 'accepted');

  const previewCacheKey = (url) => `${PREVIEW_CACHE_PREFIX}${url}`;
  const readPreviewCache = (url) => {
    try {
      const raw = localStorage.getItem(previewCacheKey(url));
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed.ts !== 'number' || !parsed.data) return null;
      if (Date.now() - parsed.ts > PREVIEW_CACHE_TTL) return null;
      return parsed.data;
    } catch {
      return null;
    }
  };
  const writePreviewCache = (url, data) => {
    try {
      localStorage.setItem(previewCacheKey(url), JSON.stringify({ ts: Date.now(), data }));
    } catch {
      // Si el navegador bloquea almacenamiento, seguimos sin caché.
    }
  };

  const formatApiError = (detail, fallback) => {
    const msg = String(detail || '').trim();
    if (!msg) return fallback;
    if (/url inválida|url invalida/i.test(msg)) return 'Pega un enlace válido de YouTube.';
    if (/privad/i.test(msg)) return 'Ese video es privado.';
    if (/no está disponible|no esta disponible/i.test(msg)) return 'Ese video no está disponible.';
    if (/demasiado largo/i.test(msg)) return msg;
    if (/formato no soportado/i.test(msg)) return 'Ese formato todavía no está disponible para este video.';
    return msg;
  };

  if (!hasAcceptedLegal()) {
    legalModal?.classList.remove('hidden');
    document.body.classList.add('modal-open');
  } else {
    sendTelemetry(window.location.pathname);
  }

  acceptLegalBtn?.addEventListener('click', () => {
    markAcceptedLegal();
    legalModal?.classList.add('hidden');
    document.body.classList.remove('modal-open');
    sendTelemetry(window.location.pathname);
  });

  // Muestra el botón de admin sólo si el backend dice "eligible" (LAN del admin, sin Cloudflare).
  fetch('/api/admin-eligible', { credentials: 'same-origin' })
    .then((r) => r.ok ? r.json() : { eligible: false })
    .then((d) => { if (d && d.eligible) document.getElementById('admin-link')?.classList.remove('hidden'); })
    .catch(() => {});

  const $ = (id) => document.getElementById(id);
  const form = $('form');
  const urlInput = $('url');
  const pasteBtn = $('paste');
  const submitBtn = $('submit');
  const primaryLabel = $('primary-label');
  const hint = $('hint');
  const preview = $('preview');
  const thumb = $('thumb');
  const titleEl = $('title');
  const uploaderEl = $('uploader');
  const durationEl = $('duration');
  const badge = $('badge');
  const bar = $('bar');
  const statusEl = $('status');
  const downloadEl = $('download');
  const tabAudio = $('tab-audio');
  const tabVideo = $('tab-video');
  const qualitiesEl = $('qualities');

  const state = {
    kind: 'audio',                  // 'audio' | 'video'
    formatKey: 'mp3-192',
    audioOptions: [],
    videoOptions: [],
    meta: null,
  };

  let eventSource = null;
  let debounceId = null;

  const YOUTUBE_RE = /^(https?:\/\/)?((www|m|music)\.)?(youtube\.com\/(watch\?v=|shorts\/|embed\/)|youtu\.be\/)[\w\-]{6,}/i;

  const setHint = (msg, level = '') => {
    hint.textContent = msg || '';
    hint.classList.remove('error', 'success');
    if (level) hint.classList.add(level);
  };

  const setLoading = (loading) => {
    submitBtn.disabled = loading;
    submitBtn.classList.toggle('loading', loading);
    urlInput.disabled = loading;
    preview.classList.toggle('busy', loading);
    statusEl.classList.toggle('hidden', !loading);
  };

  const formatDuration = (seconds) => {
    if (!seconds) return '—';
    const m = Math.floor(seconds / 60);
    const s = String(seconds % 60).padStart(2, '0');
    return `${m}:${s}`;
  };

  const resetPreview = () => {
    preview.classList.add('hidden');
    preview.classList.remove('busy');
    bar.style.width = '0%';
    statusEl.textContent = 'Preparando…';
    statusEl.classList.add('hidden');
    downloadEl.classList.add('hidden');
    downloadEl.removeAttribute('href');
    submitBtn.classList.remove('hidden');
    badge.textContent = 'Listo';
    badge.className = 'badge';
    qualitiesEl.innerHTML = '';
    state.meta = null;
  };

  const updateLabel = () => {
    const list = state.kind === 'audio' ? state.audioOptions : state.videoOptions;
    const opt = list.find((o) => o.key === state.formatKey);
    if (!opt) {
      primaryLabel.textContent = state.kind === 'audio' ? 'Descargar MP3' : 'Descargar video';
      return;
    }
    primaryLabel.textContent = state.kind === 'audio'
      ? `Descargar ${opt.label}`
      : `Descargar video ${opt.label}`;
  };

  const renderQualities = () => {
    qualitiesEl.innerHTML = '';
    const list = state.kind === 'audio' ? state.audioOptions : state.videoOptions;
    if (!list.length) {
      const span = document.createElement('span');
      span.className = 'q-empty';
      span.textContent = state.kind === 'video'
        ? 'Este video no expone resoluciones descargables. Prueba con audio.'
        : 'No hay opciones de audio disponibles.';
      qualitiesEl.appendChild(span);
      return;
    }
    // Si el formato actual no aplica al kind nuevo, pick el primero.
    if (!list.find((o) => o.key === state.formatKey)) {
      state.formatKey = list[0].key;
    }
    list.forEach((opt) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'q-chip' + (opt.key === state.formatKey ? ' active' : '');
      btn.textContent = opt.label;
      btn.setAttribute('role', 'radio');
      btn.setAttribute('aria-checked', opt.key === state.formatKey ? 'true' : 'false');
      btn.dataset.key = opt.key;
      btn.addEventListener('click', () => {
        state.formatKey = opt.key;
        renderQualities();
        updateLabel();
      });
      qualitiesEl.appendChild(btn);
    });
    updateLabel();
  };

  const setKind = (kind) => {
    state.kind = kind;
    tabAudio.classList.toggle('active', kind === 'audio');
    tabVideo.classList.toggle('active', kind === 'video');
    tabAudio.setAttribute('aria-selected', kind === 'audio' ? 'true' : 'false');
    tabVideo.setAttribute('aria-selected', kind === 'video' ? 'true' : 'false');
    renderQualities();
  };

  tabAudio.addEventListener('click', () => setKind('audio'));
  tabVideo.addEventListener('click', () => setKind('video'));

  const showPreview = (data) => {
    state.meta = data;
    state.audioOptions = data.audio_options || [];
    state.videoOptions = data.video_options || [];
    writePreviewCache(urlInput.value.trim(), data);
    preview.classList.remove('hidden');
    if (data.thumbnail) { thumb.src = data.thumbnail; thumb.alt = data.title || ''; }
    titleEl.textContent = data.title || 'Video';
    uploaderEl.textContent = data.uploader || '—';
    durationEl.textContent = formatDuration(data.duration);
    renderQualities();
  };

  const safelyParse = async (res) => {
    try { return await res.json(); } catch { return {}; }
  };

  const fetchMetadata = async (url) => {
    const res = await fetch('/api/metadata', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const data = await safelyParse(res);
    if (!res.ok) throw new Error(formatApiError(data.detail, 'No se pudo leer el video.'));
    return data;
  };

  const startConversion = async (url, format) => {
    const res = await fetch('/api/convert', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, format }),
    });
    const data = await safelyParse(res);
    if (!res.ok) throw new Error(formatApiError(data.detail, 'No se pudo iniciar la descarga.'));
    return data.job_id;
  };

  const listenProgress = (jobId) => new Promise((resolve, reject) => {
    if (eventSource) eventSource.close();
    eventSource = new EventSource(`/api/progress/${jobId}`);
    eventSource.onmessage = (ev) => {
      let event;
      try { event = JSON.parse(ev.data); } catch { return; }
      bar.style.width = `${Math.min(100, event.progress || 0)}%`;
      if (event.status === 'downloading') {
        badge.textContent = 'Descargando';
        badge.className = 'badge';
        statusEl.textContent = event.message || 'Descargando…';
      } else if (event.status === 'converting') {
        badge.textContent = 'Procesando';
        badge.className = 'badge';
        statusEl.textContent = event.message || 'Procesando…';
      } else if (event.status === 'done') {
        badge.textContent = 'Listo';
        badge.className = 'badge success';
        statusEl.textContent = '¡Tu archivo está listo!';
        bar.style.width = '100%';
        eventSource.close();
        resolve(event);
      } else if (event.status === 'error') {
        badge.textContent = 'Error';
        badge.className = 'badge error';
        statusEl.textContent = event.message || 'Error';
        eventSource.close();
        reject(new Error(event.message || 'Error en la conversión'));
      }
    };
    eventSource.onerror = () => {
      eventSource.close();
      reject(new Error('Se perdió la conexión con el servidor'));
    };
  });

  const previewFromInput = () => {
    const url = urlInput.value.trim();
    if (!url) { resetPreview(); setHint(''); return; }
    if (!YOUTUBE_RE.test(url)) { resetPreview(); setHint('Esa URL no parece de YouTube.', 'error'); return; }
    const cached = readPreviewCache(url);
    if (cached) {
      showPreview(cached);
      setHint('Vista previa cargada desde caché.', 'success');
      return;
    }
    setHint('Leyendo video…');
    fetchMetadata(url)
      .then((data) => { showPreview(data); setHint('Elige formato y calidad.', 'success'); })
      .catch((err) => { resetPreview(); setHint(err.message || 'No se pudo leer el video.', 'error'); });
  };

  urlInput.addEventListener('input', () => {
    clearTimeout(debounceId);
    debounceId = setTimeout(previewFromInput, 450);
  });

  pasteBtn?.addEventListener('click', async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text) { urlInput.value = text.trim(); previewFromInput(); }
    } catch { setHint('Tu navegador no permitió pegar. Pega manualmente.', 'error'); }
  });

  form.addEventListener('submit', (ev) => { ev.preventDefault(); });

  submitBtn.addEventListener('click', async () => {
    const url = urlInput.value.trim();
    if (!YOUTUBE_RE.test(url)) { setHint('Esa URL no parece de YouTube.', 'error'); return; }
    if (!state.meta) { setHint('Espera a que cargue la información del video.', 'error'); return; }
    setLoading(true);
    setHint('Iniciando descarga…');
    try {
      const jobId = await startConversion(url, state.formatKey);
      const final = await listenProgress(jobId);
      downloadEl.href = `/api/download/${jobId}`;
      downloadEl.setAttribute('download', final.filename || 'media');
      submitBtn.classList.add('hidden');
      downloadEl.classList.remove('hidden');
      setHint('Pulsa "Guardar archivo" para descargarlo.', 'success');
    } catch (err) {
      setHint(err.message || 'Algo salió mal.', 'error');
    } finally {
      setLoading(false);
    }
  });

  window.addEventListener('beforeunload', () => { if (eventSource) eventSource.close(); });
})();
