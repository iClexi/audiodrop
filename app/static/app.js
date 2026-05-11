/* AudioDrop frontend — UX state machine.
   Mantén este archivo simple: sólo orquesta el DOM y habla con la API. */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const form = $('form');
  const urlInput = $('url');
  const pasteBtn = $('paste');
  const submitBtn = $('submit');
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
  };

  const formatDuration = (seconds) => {
    if (!seconds) return '—';
    const m = Math.floor(seconds / 60);
    const s = String(seconds % 60).padStart(2, '0');
    return `${m}:${s}`;
  };

  const resetPreview = () => {
    preview.classList.add('hidden');
    preview.classList.remove('active');
    bar.style.width = '0%';
    statusEl.textContent = 'Preparando…';
    downloadEl.classList.add('hidden');
    downloadEl.removeAttribute('href');
    badge.textContent = 'Listo';
    badge.className = 'badge';
  };

  const showPreview = (data) => {
    preview.classList.remove('hidden');
    if (data.thumbnail) {
      thumb.src = data.thumbnail;
      thumb.alt = data.title || '';
    }
    titleEl.textContent = data.title || 'Video';
    uploaderEl.textContent = data.uploader || '';
    durationEl.textContent = formatDuration(data.duration);
  };

  const safelyParse = async (res) => {
    try {
      const data = await res.json();
      return data;
    } catch {
      return {};
    }
  };

  const fetchMetadata = async (url) => {
    const res = await fetch('/api/metadata', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const data = await safelyParse(res);
    if (!res.ok) throw new Error(data.detail || 'No se pudo leer el video');
    return data;
  };

  const startConversion = async (url) => {
    const res = await fetch('/api/convert', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const data = await safelyParse(res);
    if (!res.ok) throw new Error(data.detail || 'No se pudo iniciar la conversión');
    return data.job_id;
  };

  const listenProgress = (jobId) => new Promise((resolve, reject) => {
    if (eventSource) eventSource.close();
    eventSource = new EventSource(`/api/progress/${jobId}`);
    eventSource.onmessage = (ev) => {
      let event;
      try { event = JSON.parse(ev.data); } catch { return; }
      bar.style.width = `${Math.min(100, event.progress || 0)}%`;
      preview.classList.add('active');
      if (event.status === 'downloading') {
        badge.textContent = 'Descargando';
        badge.className = 'badge';
        statusEl.textContent = event.message || 'Descargando audio…';
      } else if (event.status === 'converting') {
        badge.textContent = 'Convirtiendo';
        badge.className = 'badge';
        statusEl.textContent = event.message || 'Convirtiendo a MP3…';
      } else if (event.status === 'done') {
        badge.textContent = 'Listo';
        badge.className = 'badge success';
        statusEl.textContent = '¡Tu MP3 está listo!';
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
    if (!url) {
      resetPreview();
      setHint('');
      return;
    }
    if (!YOUTUBE_RE.test(url)) {
      resetPreview();
      setHint('Esa URL no parece de YouTube.', 'error');
      return;
    }
    setHint('Leyendo video…');
    fetchMetadata(url)
      .then((data) => {
        showPreview(data);
        setHint('Listo para convertir.', 'success');
      })
      .catch((err) => {
        resetPreview();
        setHint(err.message, 'error');
      });
  };

  urlInput.addEventListener('input', () => {
    clearTimeout(debounceId);
    debounceId = setTimeout(previewFromInput, 450);
  });

  pasteBtn?.addEventListener('click', async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text) {
        urlInput.value = text.trim();
        previewFromInput();
      }
    } catch {
      setHint('Tu navegador no permitió pegar. Pega manualmente.', 'error');
    }
  });

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const url = urlInput.value.trim();
    if (!YOUTUBE_RE.test(url)) {
      setHint('Esa URL no parece de YouTube.', 'error');
      return;
    }
    setLoading(true);
    setHint('Iniciando conversión…');
    try {
      const meta = await fetchMetadata(url);
      showPreview(meta);
      const jobId = await startConversion(url);
      const final = await listenProgress(jobId);
      downloadEl.href = `/api/download/${jobId}`;
      downloadEl.setAttribute('download', final.filename || 'audio.mp3');
      downloadEl.classList.remove('hidden');
      setHint('Pulsa "Descargar MP3" para guardarlo.', 'success');
    } catch (err) {
      setHint(err.message || 'Algo salió mal.', 'error');
    } finally {
      setLoading(false);
    }
  });

  // Limpia el EventSource si el usuario se va.
  window.addEventListener('beforeunload', () => {
    if (eventSource) eventSource.close();
  });
})();
