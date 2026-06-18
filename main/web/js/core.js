(function () {
  const app = window.DeepEMApp || (window.DeepEMApp = {});

  app.state = {
    latestInsights: [],
    allSignalHistory: [],
    allAnomalyInsights: [],
    openTimelineIndexes: new Set(),
    startingCapture: false,
    startingDemoCapture: false,
    stoppingCapture: false,
    lastSpectrogramKey: '',
    waterfallRows: [],
    liveFftSpec: null,
    usrpWebSocket: null,
    usrpWebSocketUrl: '',
    usrpWsStatus: 'closed',
  };

  window.reviewStatus = window.reviewStatus || {};

  app.api = async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    const body = options.body;
    if (!(body instanceof FormData) && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }
    const resp = await fetch(path, { ...options, headers });
    const contentType = resp.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
      const payload = await resp.json();
      if (!resp.ok) throw new Error(payload.detail || `请求失败：${resp.status}`);
      return payload;
    }
    const text = await resp.text();
    if (!resp.ok) throw new Error(text || `请求失败：${resp.status}`);
    return text;
  };

  app.escapeHtml = function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  };

  app.statusToClass = function statusToClass(status) {
    if (status === '待研判') return 'pending';
    if (status === '研判中') return 'investigating';
    if (status === '研判完成' || status === '已关闭') return 'closed';
    return 'warning';
  };

  app.formatTime = function formatTime(value) {
    return value ? new Date(value).toLocaleTimeString('zh-CN', { hour12: false }) : '-';
  };

  app.formatDateTime = function formatDateTime(value) {
    return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '-';
  };

  app.assetUrl = function assetUrl(assetId) {
    return assetId ? `/api/assets/${encodeURIComponent(String(assetId))}` : '';
  };

  app.renderMarkdown = function renderMarkdown(markdown) {
    const source = String(markdown || '');
    if (window.marked && typeof window.marked.parse === 'function') {
      window.marked.setOptions({
        gfm: true,
        breaks: false,
        async: false,
      });
      const rendered = window.marked.parse(source);
      if (window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
        return window.DOMPurify.sanitize(rendered, {
          USE_PROFILES: { html: true },
          ADD_ATTR: ['target', 'rel'],
        });
      }
      return rendered;
    }
    return app.escapeHtml(source).replace(/\n/g, '<br>');
  };

  function powerColor(v) {
    const t = Math.max(0, Math.min(1, Number(v || 0) / 255));
    const stops = [
      [0.0, [15, 22, 36]],
      [0.2, [39, 53, 92]],
      [0.45, [53, 127, 168]],
      [0.7, [93, 200, 156]],
      [0.88, [248, 213, 93]],
      [1.0, [239, 96, 67]],
    ];
    for (let i = 1; i < stops.length; i += 1) {
      const [p2, c2] = stops[i];
      const [p1, c1] = stops[i - 1];
      if (t <= p2) {
        const r = (t - p1) / (p2 - p1 || 1);
        return [
          Math.round(c1[0] + (c2[0] - c1[0]) * r),
          Math.round(c1[1] + (c2[1] - c1[1]) * r),
          Math.round(c1[2] + (c2[2] - c1[2]) * r),
        ];
      }
    }
    return stops[stops.length - 1][1];
  }


  function normalizePowerRow(values) {
    const nums = (Array.isArray(values) ? values : []).map(Number).filter(Number.isFinite);
    if (!nums.length) return [];
    const minValue = Math.min(...nums);
    const maxValue = Math.max(...nums);
    const span = Math.max(maxValue - minValue, 1e-6);
    return nums.map(value => Math.round(((value - minValue) / span) * 255));
  }
function formatHzLabel(value) {
  const hz = Number(value);
  if (!Number.isFinite(hz)) return '-';
  const abs = Math.abs(hz);
  if (abs >= 1e9) return `${(hz / 1e9).toFixed(3)} GHz`;
  if (abs >= 1e6) return `${(hz / 1e6).toFixed(1)} MHz`;
  if (abs >= 1e3) return `${(hz / 1e3).toFixed(1)} kHz`;
  return `${hz.toFixed(0)} Hz`;
}

function buildFreqTicks(spec, count = 5) {
  const minHz = Number(spec?.freq_min_hz);
  const maxHz = Number(spec?.freq_max_hz);

  if (!Number.isFinite(minHz) || !Number.isFinite(maxHz) || minHz === maxHz) {
    return [
      { pos: 0, label: '低频' },
      { pos: 1, label: '高频' },
    ];
  }

  return Array.from({ length: count }, (_, index) => {
    const pos = count <= 1 ? 0 : index / (count - 1);
    return {
      pos,
      label: formatHzLabel(minHz + (maxHz - minHz) * pos),
    };
  });
}

function drawChartAxes(ctx, plot, options = {}) {
  const xTicks = options.xTicks || [];
  const yTicks = options.yTicks || [];
  const xLabel = options.xLabel || '';
  const yLabel = options.yLabel || '';

  ctx.save();
  ctx.strokeStyle = 'rgba(255,255,255,0.18)';
  ctx.fillStyle = '#9fb2d4';
  ctx.lineWidth = 1;
  ctx.font = '11px sans-serif';
  ctx.textBaseline = 'middle';

  ctx.strokeRect(plot.x + 0.5, plot.y + 0.5, plot.w - 1, plot.h - 1);

  xTicks.forEach(tick => {
    const x = plot.x + tick.pos * plot.w;

    ctx.strokeStyle = 'rgba(255,255,255,0.12)';
    ctx.beginPath();
    ctx.moveTo(x, plot.y);
    ctx.lineTo(x, plot.y + plot.h);
    ctx.stroke();

    ctx.strokeStyle = 'rgba(255,255,255,0.28)';
    ctx.beginPath();
    ctx.moveTo(x, plot.y + plot.h);
    ctx.lineTo(x, plot.y + plot.h + 4);
    ctx.stroke();

    ctx.textAlign = 'center';
    ctx.fillText(tick.label, x, plot.y + plot.h + 14);
  });

  yTicks.forEach(tick => {
    const y = plot.y + (1 - tick.pos) * plot.h;

    ctx.strokeStyle = 'rgba(255,255,255,0.12)';
    ctx.beginPath();
    ctx.moveTo(plot.x, y);
    ctx.lineTo(plot.x + plot.w, y);
    ctx.stroke();

    ctx.strokeStyle = 'rgba(255,255,255,0.28)';
    ctx.beginPath();
    ctx.moveTo(plot.x - 4, y);
    ctx.lineTo(plot.x, y);
    ctx.stroke();

    ctx.textAlign = 'right';
    ctx.fillText(tick.label, plot.x - 8, y);
  });

  if (xLabel) {
    ctx.textAlign = 'right';
    ctx.fillText(xLabel, plot.x + plot.w, plot.y + plot.h + 28);
  }

  if (yLabel) {
    ctx.save();
    ctx.translate(12, plot.y + plot.h / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = 'center';
    ctx.fillText(yLabel, 0, 0);
    ctx.restore();
  }

  ctx.restore();
}


  app.renderUsrpFftMessage = function renderUsrpFftMessage(message) {
    if (!message || message.type !== 'fft') return;
    const fft = (Array.isArray(message.fft_data) ? message.fft_data : []).map(Number).filter(Number.isFinite);
    if (!fft.length) return;
    const fftSize = Number(message.fft_size || fft.length);
    const sampleRate = Number(message.sample_rate || 0);
    const centerFreq = Number(message.freq || 0);
    const minValue = Math.min(...fft);
    const maxValue = Math.max(...fft);
    const spec = {
      source: 'usrp_ws',
      signal_id: `usrp-ws:${message.dev_id || ''}`,
      file_name: '实时 FFT',
      timestamp: message.timestamp || new Date().toISOString(),
      channel: '实时',
      freq_hz: centerFreq,
      freq_min_hz: sampleRate ? centerFreq - sampleRate / 2 : centerFreq,
      freq_max_hz: sampleRate ? centerFreq + sampleRate / 2 : centerFreq,
      sample_rate_hz: sampleRate,
      fft_size: fftSize,
      width: fft.length,
      height: 1,
      pixels: [normalizePowerRow(fft)],
      spectrum_db: fft,
      value_min_db: Number(minValue.toFixed(2)),
      value_max_db: Number(maxValue.toFixed(2)),
      duration_ms: 100,
    };
    app.state.liveFftSpec = spec;
    app.renderSpectrogram(spec);
  };

  app.disconnectUsrpWebSocket = function disconnectUsrpWebSocket() {
    const ws = app.state.usrpWebSocket;
    app.state.usrpWebSocket = null;
    app.state.usrpWebSocketUrl = '';
    app.state.usrpWsStatus = 'closed';
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      try { ws.close(); } catch (error) { console.warn('关闭 USRP WebSocket 失败', error); }
    }
  };

  app.syncUsrpWebSocket = function syncUsrpWebSocket(snapshot) {
    const activeSession = snapshot?.collector?.active_session || null;
    const active = Boolean(activeSession && activeSession.source_mode !== 'demo' && ['starting', 'running'].includes(activeSession.status));
    const streamInfo = activeSession?.usrp_stream || snapshot?.collector?.stream_info || null;
    const wsUrl = streamInfo?.ws_url || activeSession?.usrp_stream_url || '';
    if (!active || !wsUrl || typeof WebSocket === 'undefined') {
      app.disconnectUsrpWebSocket();
      if (!active) app.state.liveFftSpec = null;
      return;
    }
    const current = app.state.usrpWebSocket;
    if (current && app.state.usrpWebSocketUrl === wsUrl && [WebSocket.OPEN, WebSocket.CONNECTING].includes(current.readyState)) {
      return;
    }
    app.disconnectUsrpWebSocket();
    try {
      const ws = new WebSocket(wsUrl);
      app.state.usrpWebSocket = ws;
      app.state.usrpWebSocketUrl = wsUrl;
      app.state.usrpWsStatus = 'connecting';
      ws.onopen = () => { app.state.usrpWsStatus = 'open'; };
      ws.onmessage = event => {
        try {
          const message = JSON.parse(event.data);
          if (message.type === 'status') {
            app.state.usrpWsStatus = `status:${message.status || 'UNKNOWN'}`;
            return;
          }
          app.renderUsrpFftMessage(message);
        } catch (error) {
          console.warn('解析 USRP WebSocket 消息失败', error);
        }
      };
      ws.onerror = () => { app.state.usrpWsStatus = 'error'; };
      ws.onclose = () => {
        if (app.state.usrpWebSocket === ws) {
          app.state.usrpWebSocket = null;
          app.state.usrpWsStatus = 'closed';
        }
      };
    } catch (error) {
      app.state.usrpWsStatus = 'error';
      console.warn('连接 USRP WebSocket 失败', error);
    }
  };

  app.renderSpectrogram = function renderSpectrogram(spec) {
    const spectrumCanvas = document.getElementById('spectrumCanvas');
    const canvas = document.getElementById('spectrogramCanvas');
    const metaEl = document.getElementById('spectrogramMeta');
    if (!canvas || !metaEl) return;

    function drawHeatmapOnly() {
      const ctx = canvas.getContext('2d');
      const cssWidth = canvas.clientWidth || 600;
      const cssHeight = canvas.clientHeight || 260;
      canvas.width = cssWidth;
      canvas.height = cssHeight;
      ctx.clearRect(0, 0, canvas.width, canvas.height);

      if (!spec || !spec.pixels || !spec.width || !spec.height) {
        ctx.fillStyle = '#0b1422';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = '#9fb2d4';
        ctx.font = '14px sans-serif';
        ctx.fillText('等待信号上传后显示 Spectrogram', 22, 32);
        metaEl.textContent = '横轴时间，纵轴频率，颜色表示功率';
        return;
      }

      const imageCanvas = document.createElement('canvas');
      imageCanvas.width = spec.width;
      imageCanvas.height = spec.height;
      const imageCtx = imageCanvas.getContext('2d');
      const image = imageCtx.createImageData(spec.width, spec.height);
      for (let y = 0; y < spec.height; y += 1) {
        for (let x = 0; x < spec.width; x += 1) {
          const value = spec.pixels[y][x];
          const [r, g, b] = powerColor(value);
          const drawY = spec.height - 1 - y;
          const idx = (drawY * spec.width + x) * 4;
          image.data[idx] = r;
          image.data[idx + 1] = g;
          image.data[idx + 2] = b;
          image.data[idx + 3] = 255;
        }
      }
      imageCtx.putImageData(image, 0, 0);
      ctx.fillStyle = '#0b1422';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(imageCanvas, 0, 0, canvas.width, canvas.height);
      ctx.strokeStyle = 'rgba(255,255,255,0.12)';
      ctx.strokeRect(0.5, 0.5, canvas.width - 1, canvas.height - 1);
      metaEl.textContent = `${spec.file_name || '-'} ｜ channel ${spec.channel ?? '-'} ｜ 时长 ${spec.duration_ms ?? '-'} ms ｜ 频率 ${spec.freq_min_hz ?? '-'} ~ ${spec.freq_max_hz ?? '-'} Hz ｜ 功率 ${spec.value_min_db ?? '-'} ~ ${spec.value_max_db ?? '-'} dB`;
    }

    if (!spectrumCanvas) {
      drawHeatmapOnly();
      return;
    }

    const spectrumCtx = spectrumCanvas.getContext('2d');
    const ctx = canvas.getContext('2d');
    const spectrumWidth = spectrumCanvas.clientWidth || 600;
    const spectrumHeight = spectrumCanvas.clientHeight || 92;
    const cssWidth = canvas.clientWidth || 600;
    const cssHeight = canvas.clientHeight || 172;
    spectrumCanvas.width = spectrumWidth;
    spectrumCanvas.height = spectrumHeight;
    canvas.width = cssWidth;
    canvas.height = cssHeight;
    const spectrumPlot = {
      x: 58,
      y: 14,
      w: Math.max(spectrumCanvas.width - 72, 20),
      h: Math.max(spectrumCanvas.height - 52, 20),
    };

    const waterfallPlot = {
      x: 58,
      y: 14,
      w: Math.max(canvas.width - 72, 20),
      h: Math.max(canvas.height - 52, 20),
    };
    spectrumCtx.clearRect(0, 0, spectrumCanvas.width, spectrumCanvas.height);
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    if (!spec || !spec.pixels || !spec.width || !spec.height) {
      spectrumCtx.fillStyle = '#0b1422';
      spectrumCtx.fillRect(0, 0, spectrumCanvas.width, spectrumCanvas.height);
      spectrumCtx.fillStyle = '#9fb2d4';
      spectrumCtx.font = '13px sans-serif';
      spectrumCtx.fillText('等待 USRP 频谱基线', 18, 28);
      ctx.fillStyle = '#0b1422';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = '#9fb2d4';
      ctx.font = '14px sans-serif';
      ctx.fillText('等待 USRP 瀑布图', 22, 32);
      metaEl.textContent = '上方为当前频谱基线，下方为随采集分片滚动的功率瀑布图';
      return;
    }

    const spectrum = Array.isArray(spec.spectrum_db) ? spec.spectrum_db.map(Number).filter(Number.isFinite) : [];
    const values = spectrum.length ? spectrum : (spec.pixels || []).map(row => {
      const nums = Array.isArray(row) ? row.map(Number).filter(Number.isFinite) : [];
      return nums.length ? nums.reduce((a, b) => a + b, 0) / nums.length : 0;
    });
    const minValue = Math.min(...values);
    const maxValue = Math.max(...values);
    const span = Math.max(maxValue - minValue, 1e-6);

    const freqTicks = buildFreqTicks(spec, 5);

    spectrumCtx.fillStyle = '#0b1422';
    spectrumCtx.fillRect(0, 0, spectrumCanvas.width, spectrumCanvas.height);

    drawChartAxes(spectrumCtx, spectrumPlot, {
      xTicks: freqTicks,
      yTicks: [
        { pos: 0, label: `${minValue.toFixed(1)} dB` },
        { pos: 0.5, label: `${((minValue + maxValue) / 2).toFixed(1)} dB` },
        { pos: 1, label: `${maxValue.toFixed(1)} dB` },
      ],
      xLabel: 'Frequency',
      yLabel: 'Power',
    });

    spectrumCtx.strokeStyle = '#7ce7c6';
    spectrumCtx.lineWidth = 2;
    spectrumCtx.beginPath();

    values.forEach((value, index) => {
      const x = spectrumPlot.x + (
        values.length <= 1 ? 0 : (index / (values.length - 1)) * spectrumPlot.w
      );
      const y = spectrumPlot.y + spectrumPlot.h - ((value - minValue) / span) * spectrumPlot.h;

      if (index === 0) spectrumCtx.moveTo(x, y);
      else spectrumCtx.lineTo(x, y);
    });

    spectrumCtx.stroke();

    spectrumCtx.fillStyle = '#9fb2d4';
    spectrumCtx.font = '12px sans-serif';
    spectrumCtx.fillText('当前频谱基线', spectrumPlot.x + 8, spectrumPlot.y + 16);

    const key = `${spec.signal_id || ''}:${spec.file_name || ''}:${spec.timestamp || ''}`;
    if (key && key !== app.state.lastSpectrogramKey && values.length) {
      app.state.lastSpectrogramKey = key;
      const row = values.map(value => Math.round(((value - minValue) / span) * 255));
      app.state.waterfallRows.push(row);
      app.state.waterfallRows = app.state.waterfallRows.slice(-80);
    }

    ctx.fillStyle = '#0b1422';
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    const rows = app.state.waterfallRows.length ? app.state.waterfallRows : spec.pixels;

    rows.forEach((row, rowIndex) => {
      const y = waterfallPlot.y + (rowIndex / Math.max(rows.length, 1)) * waterfallPlot.h;
      const h = Math.ceil(waterfallPlot.h / Math.max(rows.length, 1));

      row.forEach((value, colIndex) => {
        const [r, g, b] = powerColor(value);
        const x = waterfallPlot.x + (colIndex / Math.max(row.length, 1)) * waterfallPlot.w;
        const w = Math.ceil(waterfallPlot.w / Math.max(row.length, 1));

        ctx.fillStyle = `rgb(${r}, ${g}, ${b})`;
        ctx.fillRect(x, y, w, h);
      });
    });

    drawChartAxes(ctx, waterfallPlot, {
      xTicks: freqTicks,
      yTicks: [
        { pos: 0, label: '新' },
        { pos: 0.5, label: `${rows.length} 帧` },
        { pos: 1, label: '旧' },
      ],
      xLabel: 'Frequency',
      yLabel: 'Time',
    });

    metaEl.textContent = `${spec.file_name || '-'} ｜ channel ${spec.channel ?? '-'} ｜ center ${spec.freq_hz ?? '-'} Hz ｜ 时长 ${spec.duration_ms ?? '-'} ms ｜ 功率 ${spec.value_min_db ?? '-'} ~ ${spec.value_max_db ?? '-'} dB`;
  };
})();
