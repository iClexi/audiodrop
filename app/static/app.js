/* VideoDrop frontend — orquesta el DOM y habla con la API.
   Soporta audio (MP3 128/192/320) y video (hasta 8K cuando esté disponible). */
(() => {
  'use strict';

  const LEGAL_KEY = 'audiodrop-legal-v2';
  const PREVIEW_CACHE_PREFIX = 'videodrop-preview-v1:';
  const PREVIEW_CACHE_TTL = 30 * 60 * 1000;
  const legalModal = document.getElementById('legal-modal');
  const acceptLegalBtn = document.getElementById('accept-legal');
  // allow forcing acceptance via query param ?_accept_legal=1 (for headless captures / dev)
  const FORCE_ACCEPT_LEGAL = new URLSearchParams(window.location.search).has('_accept_legal') || new URLSearchParams(window.location.search).get('accept_legal') === '1';
  const captchaSiteKey = window.AUDIODROP_CAPTCHA_SITE_KEY || '';

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

  const getCaptchaToken = async (action) => {
    if (!captchaSiteKey || !window.grecaptcha?.execute) return '';
    try {
      await new Promise((resolve) => window.grecaptcha.ready(resolve));
      return await window.grecaptcha.execute(captchaSiteKey, { action });
    } catch {
      return '';
    }
  };

  const withCaptcha = async (body, action) => {
    const token = await getCaptchaToken(action);
    return token ? { ...body, captcha_token: token } : body;
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
    if (FORCE_ACCEPT_LEGAL) {
      markAcceptedLegal();
      sendTelemetry(window.location.pathname);
    } else {
      legalModal?.classList.remove('hidden');
      document.body.classList.add('modal-open');
    }
  } else {
    sendTelemetry(window.location.pathname);
  }

  acceptLegalBtn?.addEventListener('click', () => {
    markAcceptedLegal();
    legalModal?.classList.add('hidden');
    document.body.classList.remove('modal-open');
    sendTelemetry(window.location.pathname);
  });

  const $ = (id) => document.getElementById(id);
  const form = $('form');
  const urlInput = $('url');
  const pasteBtn = $('paste');
  const submitBtn = $('submit');
  const primaryLabel = $('primary-label');
  const hint = $('hint');
  const idlePanel = $('idle-panel');
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
  const segmentPanel = $('segment-panel');
  const segmentSelect = $('segment-select');
  const segmentCopy = $('segment-copy');
  const transcribeBtn = $('transcribe');
  const transcriptPanel = $('transcript-panel');
  const transcriptMeta = $('transcript-meta');
  const transcriptText = $('transcript-text');
  const copyTranscriptBtn = $('copy-transcript');
  const adminLink = $('admin-link');
  const authModal = $('auth-modal');
  const authOpen = $('auth-open');
  const logoutButton = $('logout-button');
  const accountLink = $('account-link');
  const sessionUser = $('session-user');
  const authMessage = $('auth-message');
  const loginForm = $('login-form');
  const registerForm = $('register-form');
  const loginTab = $('login-tab');
  const registerTab = $('register-tab');

  const state = {
    kind: 'audio',                  // 'audio' | 'video'
    formatKey: 'mp3-192',
    audioOptions: [],
    videoOptions: [],
    segments: [],
    segmentIndex: null,
    meta: null,
  };

  let eventSource = null;
  let debounceId = null;
  let transcriptRequestId = 0;
  let activeTranscriptUrl = '';
  let transcriptInFlight = false;
  let currentUser = null;

  const YOUTUBE_RE = /^(https?:\/\/)?((www|m|music)\.)?(youtube\.com\/(watch\?v=|shorts\/|embed\/)|youtu\.be\/)[\w\-]{6,}/i;

  const setHint = (msg, level = '') => {
    hint.textContent = msg || '';
    hint.classList.remove('error', 'success');
    if (level) hint.classList.add(level);
  };

  const setAuthMessage = (message, level = '') => {
    if (!authMessage) return;
    authMessage.textContent = message || '';
    authMessage.classList.remove('error', 'success');
    if (level) authMessage.classList.add(level);
  };

  const setAuthMode = (mode) => {
    const isRegister = mode === 'register';
    loginForm?.classList.toggle('hidden', isRegister);
    registerForm?.classList.toggle('hidden', !isRegister);
    loginTab?.classList.toggle('active', !isRegister);
    registerTab?.classList.toggle('active', isRegister);
    setAuthMessage('');
  };

  const openAuth = (mode = 'login') => {
    setAuthMode(mode);
    authModal?.classList.remove('hidden');
    document.body.classList.add('modal-open');
  };

  const closeAuth = () => {
    authModal?.classList.add('hidden');
    document.body.classList.remove('modal-open');
  };

  const safeJson = async (res) => {
    try { return await res.json(); } catch { return {}; }
  };

  const authFetch = async (url, body) => {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body || {}),
    });
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data.detail || data.error || 'No se pudo completar la acción.');
    return data;
  };

  const refreshAdminEligibility = () => {
    fetch('/api/admin-eligible', { credentials: 'same-origin' })
      .then((r) => r.ok ? r.json() : { eligible: false })
      .then((d) => {
        adminLink?.classList.toggle('hidden', !(d && d.eligible));
      })
      .catch(() => adminLink?.classList.add('hidden'));
  };

  const renderAuthState = (user) => {
    currentUser = user || null;
    authOpen?.classList.toggle('hidden', !!currentUser);
    logoutButton?.classList.toggle('hidden', !currentUser);
    accountLink?.classList.toggle('hidden', !currentUser);
    sessionUser?.classList.toggle('hidden', !currentUser);
    if (sessionUser) sessionUser.textContent = currentUser ? currentUser.username : '';
    refreshAdminEligibility();
  };

  const refreshAuth = () => {
    fetch('/api/auth/me', { credentials: 'same-origin' })
      .then((r) => r.ok ? r.json() : { user: null })
      .then((data) => renderAuthState(data.user || null))
      .catch(() => renderAuthState(null));
  };

  const setLoading = (loading) => {
    submitBtn.disabled = loading;
    submitBtn.classList.toggle('loading', loading);
    urlInput.disabled = loading;
    preview.classList.toggle('busy', loading);
    statusEl.classList.toggle('hidden', !loading);
  };

  const formatDuration = (seconds) => {
    if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return '—';
    seconds = Number(seconds);
    const m = Math.floor(seconds / 60);
    const s = String(seconds % 60).padStart(2, '0');
    return `${m}:${s}`;
  };

  const resetPreview = () => {
    transcriptRequestId += 1;
    activeTranscriptUrl = '';
    transcriptInFlight = false;
    idlePanel?.classList.remove('hidden');
    preview.classList.add('hidden');
    preview.classList.remove('busy');
    bar.style.width = '0%';
    statusEl.textContent = 'Preparando…';
    statusEl.classList.add('hidden');
    downloadEl.classList.add('hidden');
    downloadEl.removeAttribute('href');
    transcribeBtn?.classList.add('hidden');
    if (transcribeBtn) {
      transcribeBtn.disabled = false;
      transcribeBtn.textContent = 'Desgrabar texto';
    }
    segmentPanel?.classList.add('hidden');
    transcriptPanel?.classList.add('hidden');
    if (transcriptText) transcriptText.textContent = '';
    if (transcriptMeta) transcriptMeta.textContent = '—';
    if (copyTranscriptBtn) copyTranscriptBtn.disabled = true;
    submitBtn.classList.remove('hidden');
    badge.textContent = 'Listo';
    badge.className = 'badge';
    qualitiesEl.innerHTML = '';
    state.segments = [];
    state.segmentIndex = null;
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

  const renderSegments = () => {
    if (!segmentPanel || !segmentSelect || !segmentCopy) return;
    segmentSelect.innerHTML = '';
    if (!state.segments.length) {
      state.segmentIndex = null;
      segmentPanel.classList.add('hidden');
      return;
    }
    state.segmentIndex = state.segmentIndex ?? 0;
    const maxMinutes = Math.round((state.meta?.max_duration || 0) / 60);
    const totalMinutes = Math.round((state.meta?.duration || 0) / 60);
    segmentCopy.textContent = `Duración aproximada: ${totalMinutes} min. Descarga en bloques de hasta ${maxMinutes} min para evitar cortes.`;
    for (const segment of state.segments) {
      const option = document.createElement('option');
      option.value = String(segment.index);
      option.textContent = `${segment.label} · ${formatDuration(segment.start)} a ${formatDuration(segment.end)}`;
      segmentSelect.appendChild(option);
    }
    segmentSelect.value = String(state.segmentIndex);
    segmentPanel.classList.remove('hidden');
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
  segmentSelect?.addEventListener('change', () => {
    state.segmentIndex = Number(segmentSelect.value);
  });

  const showPreview = (data) => {
    state.meta = data;
    state.audioOptions = data.audio_options || [];
    state.videoOptions = data.video_options || [];
    state.segments = data.segments || [];
    state.segmentIndex = state.segments.length ? 0 : null;
    writePreviewCache(urlInput.value.trim(), data);
    idlePanel?.classList.add('hidden');
    preview.classList.remove('hidden');
    transcribeBtn?.classList.remove('hidden');
    if (data.thumbnail) { thumb.src = data.thumbnail; thumb.alt = data.title || ''; }
    titleEl.textContent = data.title || 'Video';
    uploaderEl.textContent = data.uploader || '—';
    durationEl.textContent = formatDuration(data.duration);
    renderSegments();
    renderQualities();
    loadTranscript(urlInput.value.trim(), { passive: true });
  };

  const safelyParse = async (res) => {
    try { return await res.json(); } catch { return {}; }
  };

  const fetchMetadata = async (url) => {
    const body = await withCaptcha({ url }, 'metadata');
    const res = await fetch('/api/metadata', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await safelyParse(res);
    if (!res.ok) throw new Error(formatApiError(data.detail, 'No se pudo leer el video.'));
    return data;
  };

  const startConversion = async (url, format) => {
    const body = { url, format };
    if (state.segmentIndex !== null && state.segmentIndex !== undefined) {
      body.segment_index = state.segmentIndex;
    }
    const protectedBody = await withCaptcha(body, 'convert');
    const res = await fetch('/api/convert', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(protectedBody),
    });
    const data = await safelyParse(res);
    if (!res.ok) throw new Error(formatApiError(data.detail, 'No se pudo iniciar la descarga.'));
    return data.job_id;
  };

  const fetchTranscript = async (url) => {
    const body = await withCaptcha({ url }, 'transcript');
    const res = await fetch('/api/transcript', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await safelyParse(res);
    if (!res.ok) throw new Error(formatApiError(data.detail, 'No se pudo extraer la transcripción.'));
    return data;
  };

  const sourceLabel = (source) => (
    source === 'automatic_captions' ? 'captions automáticos' : 'subtítulos'
  );

  const renderTranscriptLoading = () => {
    transcriptPanel?.classList.remove('hidden');
    if (copyTranscriptBtn) copyTranscriptBtn.disabled = true;
    if (transcriptMeta) transcriptMeta.textContent = 'Buscando captions disponibles…';
    if (transcriptText) transcriptText.textContent = 'Preparando desgrabado del video…';
    if (transcribeBtn) {
      transcribeBtn.classList.remove('hidden');
      transcribeBtn.disabled = true;
      transcribeBtn.textContent = 'Desgrabando…';
    }
  };

  const renderTranscriptResult = (data) => {
    transcriptPanel?.classList.remove('hidden');
    if (transcriptMeta) {
      transcriptMeta.textContent = `${data.language || 'idioma desconocido'} · ${sourceLabel(data.source)} · ${data.characters || 0} caracteres`;
    }
    if (transcriptText) transcriptText.textContent = data.text || '';
    if (copyTranscriptBtn) copyTranscriptBtn.disabled = !(data.text || '').trim();
    if (transcribeBtn) {
      transcribeBtn.disabled = false;
      transcribeBtn.textContent = 'Actualizar texto';
    }
  };

  const renderTranscriptError = (err) => {
    transcriptPanel?.classList.remove('hidden');
    if (copyTranscriptBtn) copyTranscriptBtn.disabled = true;
    if (transcriptMeta) transcriptMeta.textContent = 'Texto no disponible';
    if (transcriptText) {
      transcriptText.textContent = err.message || 'Este video no expone subtítulos descargables.';
    }
    if (transcribeBtn) {
      transcribeBtn.disabled = false;
      transcribeBtn.textContent = 'Reintentar desgrabado';
    }
  };

  const loadTranscript = async (url, { passive = false } = {}) => {
    if (!YOUTUBE_RE.test(url)) return;
    if (transcriptInFlight && activeTranscriptUrl === url) return;
    const requestId = ++transcriptRequestId;
    activeTranscriptUrl = url;
    transcriptInFlight = true;
    renderTranscriptLoading();
    if (!passive) setHint('Buscando subtítulos disponibles…');
    try {
      const data = await fetchTranscript(url);
      if (requestId !== transcriptRequestId) return;
      renderTranscriptResult(data);
      setHint(passive ? 'Preview y desgrabador listos.' : 'Transcripción lista.', 'success');
    } catch (err) {
      if (requestId !== transcriptRequestId) return;
      renderTranscriptError(err);
      if (!passive) setHint(err.message || 'No se pudo extraer la transcripción.', 'error');
    } finally {
      if (requestId === transcriptRequestId) transcriptInFlight = false;
    }
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

  authOpen?.addEventListener('click', () => openAuth('login'));
  authModal?.querySelectorAll('[data-auth-close]').forEach((el) => {
    el.addEventListener('click', closeAuth);
  });
  loginTab?.addEventListener('click', () => setAuthMode('login'));
  registerTab?.addEventListener('click', () => setAuthMode('register'));

  loginForm?.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const formData = new FormData(loginForm);
    setAuthMessage('Entrando...');
    try {
      const data = await authFetch('/api/auth/login', {
        email: formData.get('email'),
        password: formData.get('password'),
      });
      renderAuthState(data.user || null);
      setAuthMessage('Sesión iniciada.', 'success');
      closeAuth();
    } catch (err) {
      setAuthMessage(err.message || 'No se pudo iniciar sesión.', 'error');
    }
  });

  registerForm?.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const formData = new FormData(registerForm);
    setAuthMessage('Creando cuenta...');
    try {
      const data = await authFetch('/api/auth/register', {
        username: formData.get('username'),
        email: formData.get('email'),
        password: formData.get('password'),
      });
      renderAuthState(data.user || null);
      setAuthMessage('Cuenta creada.', 'success');
      closeAuth();
    } catch (err) {
      setAuthMessage(err.message || 'No se pudo crear la cuenta.', 'error');
    }
  });

  logoutButton?.addEventListener('click', async () => {
    await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' }).catch(() => {});
    renderAuthState(null);
  });

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    previewFromInput();
  });

  transcribeBtn?.addEventListener('click', async () => {
    const url = urlInput.value.trim();
    if (!YOUTUBE_RE.test(url)) { setHint('Esa URL no parece de YouTube.', 'error'); return; }
    loadTranscript(url, { passive: false });
  });

  copyTranscriptBtn?.addEventListener('click', async () => {
    const text = transcriptText?.textContent || '';
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setHint('Transcripción copiada.', 'success');
    } catch {
      setHint('No se pudo copiar automáticamente.', 'error');
    }
  });

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
  refreshAuth();
})();
