(() => {
  'use strict';

  const state = {
    templates: [], tasks: [], currentTask: null, selectedNodeId: null, selectedTemplateIds: new Set(),
    eventSource: null, eventSeq: 0, refreshTimer: null, isSending: false, runtime: null,
    approvalModalTaskId: null, approvalDirty: false, usrpSocket: null, usrpSocketUrl: '', waterfallRows: [],
    lastFftMessage: null, liveFrameCount: 0, spectrumObjectUrl: '', baselines: [], selectedBaselineId: '',
  };

  const el = id => document.getElementById(id);
  const dom = {
    currentTaskTitle: el('currentTaskTitle'), plannerModel: el('plannerModel'), planVersion: el('planVersion'), taskStatus: el('taskStatus'),
    overallProgressText: el('overallProgressText'), overallProgressBar: el('overallProgressBar'),
    baselineManagerBtn: el('baselineManagerBtn'), approveTaskBtn: el('approveTaskBtn'), pauseTaskBtn: el('pauseTaskBtn'), resumeTaskBtn: el('resumeTaskBtn'), cancelTaskBtn: el('cancelTaskBtn'),
    newTaskBtn: el('newTaskBtn'), taskPicker: el('taskPicker'), renameTaskBtn: el('renameTaskBtn'), deleteTaskBtn: el('deleteTaskBtn'), planNodeList: el('planNodeList'),
    detailEmpty: el('detailEmpty'), detailContent: el('detailContent'), detailTitle: el('detailTitle'), detailSubtitle: el('detailSubtitle'), detailStatus: el('detailStatus'),
    nodeProgress: el('nodeProgress'), nodeAttempts: el('nodeAttempts'), nodeStarted: el('nodeStarted'), nodeEnded: el('nodeEnded'),
    nodeSummary: el('nodeSummary'), nodeSummaryTime: el('nodeSummaryTime'), nodeDescription: el('nodeDescription'), nodeCriteria: el('nodeCriteria'),
    nodeDependencies: el('nodeDependencies'), nodeTool: el('nodeTool'), nodeLogs: el('nodeLogs'), logCount: el('logCount'),
    nodeInputs: el('nodeInputs'), nodeOutputs: el('nodeOutputs'), codeCard: el('codeCard'), generatedCode: el('generatedCode'), copyCodeBtn: el('copyCodeBtn'),
    nodeNote: el('nodeNote'), saveNodeNoteBtn: el('saveNodeNoteBtn'), noteSaveStatus: el('noteSaveStatus'),
    reasoningEmpty: el('reasoningEmpty'), reasoningStream: el('reasoningStream'), structuredStream: el('structuredStream'), connectionStatus: el('connectionStatus'),
    templateUpload: el('templateUpload'), templateList: el('templateList'), templateDetailBtn: el('templateDetailBtn'),
    templateModal: el('templateModal'), templateManagerList: el('templateManagerList'), closeTemplateModalBtn: el('closeTemplateModalBtn'),
    docxPreviewModal: el('docxPreviewModal'), docxPreviewTitle: el('docxPreviewTitle'), docxPreviewFile: el('docxPreviewFile'),
    docxPreviewText: el('docxPreviewText'), closeDocxPreviewBtn: el('closeDocxPreviewBtn'), uploadStatus: el('uploadStatus'),
    instructionInput: el('instructionInput'), sendInstructionBtn: el('sendInstructionBtn'), artifactList: el('artifactList'), toast: el('toast'),
    approvalModal: el('approvalModal'), approvalParamGrid: el('approvalParamGrid'), approvalFlow: el('approvalFlow'), approvalParamStatus: el('approvalParamStatus'),
    approvalModalStatus: el('approvalModalStatus'), approvePlanModalBtn: el('approvePlanModalBtn'), rejectPlanBtn: el('rejectPlanBtn'), saveApprovalParamsBtn: el('saveApprovalParamsBtn'),
    liveSpectrumCard: el('liveSpectrumCard'), liveSpectrumStatus: el('liveSpectrumStatus'), liveSpectrumCanvas: el('liveSpectrumCanvas'),
    liveWaterfallCanvas: el('liveWaterfallCanvas'), liveSpectrumMeta: el('liveSpectrumMeta'),
    spectrumModal: el('spectrumModal'), spectrumModalTitle: el('spectrumModalTitle'), spectrumModalFile: el('spectrumModalFile'),
    spectrumModalLoading: el('spectrumModalLoading'), spectrumModalImage: el('spectrumModalImage'), closeSpectrumModalBtn: el('closeSpectrumModalBtn'),
    baselineModal: el('baselineModal'), baselineList: el('baselineList'), closeBaselineModalBtn: el('closeBaselineModalBtn'),
    baselineThresholdInput: el('baselineThresholdInput'), baselineDcBinsInput: el('baselineDcBinsInput'), baselineEdgeBinsInput: el('baselineEdgeBinsInput'),
    refreshBaselineListBtn: el('refreshBaselineListBtn'), saveBaselineBtn: el('saveBaselineBtn'), baselineSaveStatus: el('baselineSaveStatus'),
  };

  const STATUS = {
    planning: '规划中', awaiting_approval: '待批准', running: '执行中', paused: '已暂停', completed: '已完成', failed: '失败', cancelled: '已取消',
    pending: '等待', ready: '就绪', in_progress: '执行中', blocked: '待批准', skipped: '跳过', idle: '空闲',
  };

  const APPROVAL_FIELDS = [
    { key: 'room_name', label: '会议室/区域', type: 'text' },
    { key: 'freq_start_mhz', label: '起始频率 MHz', type: 'number', step: '0.001' },
    { key: 'freq_stop_mhz', label: '终止频率 MHz', type: 'number', step: '0.001' },
    { key: 'freq_step_mhz', label: '频率步长 MHz', type: 'number', step: '0.001' },
    { key: 'dwell_ms', label: '每频点驻留 ms', type: 'number', step: '1' },
    { key: 'repeat_count', label: '重复次数', type: 'number', step: '1' },
    { key: 'sample_rate', label: '采样率 MHz', type: 'number', step: '0.001' },
    { key: 'bandwidth', label: '带宽 MHz', type: 'number', step: '0.001' },
    { key: 'gain', label: '增益 dB', type: 'number', step: '0.1' },
    { key: 'antenna', label: '天线', type: 'text' },
  ];

  const statusLabel = value => STATUS[value] || value || '-';
  const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  const safeJson = value => { try { return JSON.stringify(value ?? {}, null, 2); } catch (_) { return String(value); } };
  const fmtTime = value => {
    if (!value) return '-';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  };
  const fmtSize = bytes => {
    const value = Number(bytes || 0);
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / 1024 / 1024).toFixed(1)} MB`;
  };
  const fmtFreq = value => {
    const number = Number(value);
    if (!Number.isFinite(number)) return '-';
    const mhz = Math.abs(number) > 100000 ? number / 1e6 : number;
    return `${mhz.toLocaleString('zh-CN', { maximumFractionDigits: 6 })} MHz`;
  };

  const taskDisplayTitle = task => task?.display_title || (task?.task_number ? `任务${task.task_number}：${task.title || task.id}` : (task?.title || task?.id || '新建采集任务'));

  function syncTemplateSelections(defaultAll = false) {
    const known = new Set((state.templates || []).map(item => item.id));
    state.selectedTemplateIds = new Set([...state.selectedTemplateIds].filter(id => known.has(id)));
    if (defaultAll && !state.selectedTemplateIds.size) {
      state.templates.forEach(item => state.selectedTemplateIds.add(item.id));
    }
  }

  function isCaptureNode(node) {
    if (!node) return false;
    const tool = String(node.tool_name || node.allowed_tool || '');
    return node.executor === 'collection_execution'
      || ['execute_spectrum_collection', 'execute_usrp_task_code', 'run_autonomous_usrp_task'].includes(tool);
  }

  function powerColor(value) {
    const t = Math.max(0, Math.min(1, Number(value || 0) / 255));
    const stops = [
      [0.0, [15, 22, 36]], [0.2, [39, 53, 92]], [0.45, [53, 127, 168]],
      [0.7, [93, 200, 156]], [0.88, [248, 213, 93]], [1.0, [239, 96, 67]],
    ];
    for (let index = 1; index < stops.length; index += 1) {
      const [p2, c2] = stops[index];
      const [p1, c1] = stops[index - 1];
      if (t <= p2) {
        const ratio = (t - p1) / (p2 - p1 || 1);
        return c1.map((channel, channelIndex) => Math.round(channel + (c2[channelIndex] - channel) * ratio));
      }
    }
    return stops.at(-1)[1];
  }

  function setLiveSpectrumStatus(text, mode = '') {
    dom.liveSpectrumStatus.textContent = text;
    dom.liveSpectrumStatus.className = `live-spectrum-status${mode ? ` ${mode}` : ''}`;
  }

  function sizeCanvas(canvas, fallbackWidth, fallbackHeight) {
    const width = Math.max(320, Math.round(canvas.clientWidth || fallbackWidth));
    const height = Math.max(60, Math.round(canvas.clientHeight || fallbackHeight));
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
    return { width, height };
  }

  function drawLivePlaceholder(message = '等待 USRP 实时 FFT 数据') {
    [
      [dom.liveSpectrumCanvas, '等待当前频谱'],
      [dom.liveWaterfallCanvas, message],
    ].forEach(([canvas, label], index) => {
      if (!canvas) return;
      const { width, height } = sizeCanvas(canvas, 640, index ? 150 : 92);
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = '#07111a';
      ctx.fillRect(0, 0, width, height);
      ctx.fillStyle = '#7893a3';
      ctx.font = '12px sans-serif';
      ctx.fillText(label, 14, 24);
      ctx.strokeStyle = 'rgba(255,255,255,.08)';
      ctx.strokeRect(.5, .5, width - 1, height - 1);
    });
  }

  function resetLiveSpectrum() {
    state.waterfallRows = [];
    state.lastFftMessage = null;
    state.liveFrameCount = 0;
    dom.liveSpectrumMeta.textContent = '等待采集节点提供实时 FFT 数据';
    setLiveSpectrumStatus('等待采集');
    drawLivePlaceholder();
  }

  function renderLiveFftMessage(message, appendRow = true) {
    if (!message || message.type !== 'fft') return;
    const fft = (Array.isArray(message.fft_data) ? message.fft_data : []).map(Number).filter(Number.isFinite);
    if (!fft.length) return;
    state.lastFftMessage = message;
    if (appendRow) state.liveFrameCount += 1;

    const minValue = Math.min(...fft);
    const maxValue = Math.max(...fft);
    const span = Math.max(maxValue - minValue, 1e-6);
    const normalized = fft.map(value => Math.round(((value - minValue) / span) * 255));
    if (appendRow) {
      state.waterfallRows.push(normalized);
      state.waterfallRows = state.waterfallRows.slice(-80);
    }

    const spectrum = dom.liveSpectrumCanvas;
    const spectrumSize = sizeCanvas(spectrum, 640, 92);
    const sctx = spectrum.getContext('2d');
    sctx.clearRect(0, 0, spectrumSize.width, spectrumSize.height);
    sctx.fillStyle = '#07111a';
    sctx.fillRect(0, 0, spectrumSize.width, spectrumSize.height);
    sctx.strokeStyle = 'rgba(255,255,255,.08)';
    sctx.lineWidth = 1;
    for (let line = 1; line < 4; line += 1) {
      const y = spectrumSize.height * line / 4;
      sctx.beginPath(); sctx.moveTo(0, y); sctx.lineTo(spectrumSize.width, y); sctx.stroke();
    }
    sctx.strokeStyle = '#67e5be';
    sctx.lineWidth = 1.8;
    sctx.beginPath();
    fft.forEach((value, index) => {
      const x = fft.length <= 1 ? 0 : index / (fft.length - 1) * spectrumSize.width;
      const y = spectrumSize.height - 8 - (value - minValue) / span * (spectrumSize.height - 18);
      if (index === 0) sctx.moveTo(x, y); else sctx.lineTo(x, y);
    });
    sctx.stroke();
    sctx.fillStyle = '#8da6b4';
    sctx.font = '10px sans-serif';
    sctx.fillText(`${maxValue.toFixed(1)} dB`, 8, 13);
    sctx.fillText(`${minValue.toFixed(1)} dB`, 8, spectrumSize.height - 6);

    const waterfall = dom.liveWaterfallCanvas;
    const waterfallSize = sizeCanvas(waterfall, 640, 150);
    const wctx = waterfall.getContext('2d');
    wctx.clearRect(0, 0, waterfallSize.width, waterfallSize.height);
    wctx.fillStyle = '#07111a';
    wctx.fillRect(0, 0, waterfallSize.width, waterfallSize.height);
    state.waterfallRows.forEach((row, rowIndex) => {
      const y = rowIndex / Math.max(state.waterfallRows.length, 1) * waterfallSize.height;
      const h = Math.ceil(waterfallSize.height / Math.max(state.waterfallRows.length, 1));
      row.forEach((value, colIndex) => {
        const [r, g, b] = powerColor(value);
        const x = colIndex / Math.max(row.length, 1) * waterfallSize.width;
        const width = Math.ceil(waterfallSize.width / Math.max(row.length, 1));
        wctx.fillStyle = `rgb(${r},${g},${b})`;
        wctx.fillRect(x, y, width, h);
      });
    });
    wctx.strokeStyle = 'rgba(255,255,255,.1)';
    wctx.strokeRect(.5, .5, waterfallSize.width - 1, waterfallSize.height - 1);

    const center = Number(message.freq || 0);
    const sampleRate = Number(message.sample_rate || 0);
    const low = sampleRate ? center - sampleRate / 2 : center;
    const high = sampleRate ? center + sampleRate / 2 : center;
    dom.liveSpectrumMeta.textContent = `中心 ${fmtFreq(center)} ｜ 范围 ${fmtFreq(low)} - ${fmtFreq(high)} ｜ FFT ${message.fft_size || fft.length} ｜ 已接收 ${state.liveFrameCount} 帧 ｜ 功率 ${minValue.toFixed(2)} ~ ${maxValue.toFixed(2)} dB`;
    setLiveSpectrumStatus('实时数据', 'connected');
  }

  function disconnectLiveSpectrum() {
    const socket = state.usrpSocket;
    state.usrpSocket = null;
    state.usrpSocketUrl = '';
    if (socket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(socket.readyState)) {
      try { socket.close(); } catch (_) { /* ignore */ }
    }
  }

  function connectLiveSpectrum(wsUrl) {
    const url = String(wsUrl || '').trim();
    if (!url || typeof WebSocket === 'undefined') return;
    const current = state.usrpSocket;
    if (current && state.usrpSocketUrl === url && [WebSocket.OPEN, WebSocket.CONNECTING].includes(current.readyState)) return;
    disconnectLiveSpectrum();
    state.usrpSocketUrl = url;
    setLiveSpectrumStatus('连接中', 'connecting');
    dom.liveSpectrumMeta.textContent = `正在连接实时 FFT：${url}`;
    try {
      const socket = new WebSocket(url);
      state.usrpSocket = socket;
      socket.onopen = () => {
        if (state.usrpSocket !== socket) return;
        setLiveSpectrumStatus('已连接', 'connected');
        dom.liveSpectrumMeta.textContent = '已连接 USRP，等待 FFT 帧';
      };
      socket.onmessage = raw => {
        try {
          const message = JSON.parse(raw.data);
          if (message.type === 'fft') renderLiveFftMessage(message);
          else if (message.type === 'status') {
            const status = String(message.status || '已连接');
            setLiveSpectrumStatus(status === 'RUNNING' ? '采集中' : status, 'connected');
          }
        } catch (error) {
          console.warn('解析采集页面 USRP FFT 消息失败', error);
        }
      };
      socket.onerror = () => {
        if (state.usrpSocket !== socket) return;
        setLiveSpectrumStatus('连接失败', 'error');
        dom.liveSpectrumMeta.textContent = '无法订阅 USRP 实时流；采集任务仍会继续执行，请检查浏览器到 USRP WebSocket 的网络与多客户端支持。';
      };
      socket.onclose = () => {
        if (state.usrpSocket !== socket) return;
        state.usrpSocket = null;
        state.usrpSocketUrl = '';
        if (!state.lastFftMessage) setLiveSpectrumStatus('连接已断开', 'error');
        else setLiveSpectrumStatus('保留最后一帧');
      };
    } catch (error) {
      state.usrpSocket = null;
      state.usrpSocketUrl = '';
      setLiveSpectrumStatus('连接失败', 'error');
      dom.liveSpectrumMeta.textContent = `实时 FFT 连接失败：${error.message}`;
    }
  }

  function updateLiveSpectrumCard(node) {
    const visible = isCaptureNode(node);
    dom.liveSpectrumCard.classList.toggle('hidden', !visible);
    if (visible && !state.lastFftMessage) drawLivePlaceholder();
  }

  function latestLiveWsUrl(task) {
    const events = Array.isArray(task?.events) ? task.events : [];
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const url = events[index]?.data?.ws_url;
      if (url) return String(url);
    }
    return '';
  }

  function syncLiveSpectrumFromTask() {
    const task = state.currentTask;
    const activeNode = task?.nodes?.find(node => isCaptureNode(node) && node.status === 'in_progress');
    if (!task || task.status !== 'running' || !activeNode) {
      disconnectLiveSpectrum();
      if (state.lastFftMessage) setLiveSpectrumStatus('保留最后一帧');
      return;
    }
    const wsUrl = latestLiveWsUrl(task);
    if (wsUrl) connectLiveSpectrum(wsUrl);
    else {
      setLiveSpectrumStatus('等待实时地址', 'connecting');
      dom.liveSpectrumMeta.textContent = '采集节点已启动，正在等待 USRP WebSocket 地址';
    }
  }

  function handleLiveSpectrumEvent(event) {
    const wsUrl = event?.data?.ws_url;
    if (wsUrl) connectLiveSpectrum(wsUrl);
    if (event?.data?.freq || event?.data?.freq_mhz || event?.data?.["freq_" + "hz"]) {
      const freq = event.data.freq_mhz ?? event.data.freq ?? event.data["freq_" + "hz"];
      dom.liveSpectrumMeta.textContent = `正在采集 ${fmtFreq(freq)}，等待实时 FFT 帧`;
    }
  }

  async function openSpectrumModal(url, fileName) {
    if (!url) return;
    if (state.spectrumObjectUrl) URL.revokeObjectURL(state.spectrumObjectUrl);
    state.spectrumObjectUrl = '';
    dom.spectrumModal.classList.remove('hidden');
    dom.spectrumModalTitle.textContent = '频谱可视化';
    dom.spectrumModalFile.textContent = fileName || '';
    dom.spectrumModalImage.classList.add('hidden');
    dom.spectrumModalImage.removeAttribute('src');
    dom.spectrumModalLoading.classList.remove('hidden');
    dom.spectrumModalLoading.textContent = '正在生成频谱图片……';
    try {
      const response = await fetch(url, { headers: { Accept: 'image/png' } });
      if (!response.ok) {
        let detail = `请求失败 (${response.status})`;
        try { detail = (await response.json()).detail || detail; } catch (_) { /* ignore */ }
        throw new Error(detail);
      }
      const blob = await response.blob();
      state.spectrumObjectUrl = URL.createObjectURL(blob);
      dom.spectrumModalImage.src = state.spectrumObjectUrl;
      dom.spectrumModalImage.classList.remove('hidden');
      dom.spectrumModalLoading.classList.add('hidden');
    } catch (error) {
      dom.spectrumModalLoading.textContent = `频谱图片生成失败：${error.message}`;
      showToast(error.message, true);
    }
  }

  function closeSpectrumModal() {
    dom.spectrumModal.classList.add('hidden');
    dom.spectrumModalImage.classList.add('hidden');
    dom.spectrumModalImage.removeAttribute('src');
    if (state.spectrumObjectUrl) URL.revokeObjectURL(state.spectrumObjectUrl);
    state.spectrumObjectUrl = '';
  }

  function showToast(message, isError = false) {
    dom.toast.textContent = message;
    dom.toast.className = `toast${isError ? ' error' : ''}`;
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => dom.toast.classList.add('hidden'), 3200);
  }

  async function api(url, options = {}) {
    const config = { ...options, headers: { ...(options.headers || {}) } };
    if (config.body && !(config.body instanceof FormData) && typeof config.body !== 'string') {
      config.headers['Content-Type'] = 'application/json';
      config.body = JSON.stringify(config.body);
    }
    const response = await fetch(url, config);
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* no body */ }
    if (!response.ok) throw new Error(payload.detail || payload.message || `请求失败 (${response.status})`);
    return payload;
  }

  function setStatus(element, status) {
    element.textContent = statusLabel(status);
    element.className = `status-pill ${status || 'idle'}`;
  }

  function renderRuntime() {
    const runtime = state.runtime || {};
    dom.plannerModel.textContent = runtime.available ? `${runtime.model} · ${runtime.endpoint}` : '规划服务未配置';
    dom.sendInstructionBtn.disabled = !runtime.available || state.isSending;
    if (!runtime.available) setConnection('请先配置规划服务', false);
  }

  function renderTaskPicker() {
    if (!state.tasks.length) {
      dom.taskPicker.innerHTML = '<option value="">暂无任务</option>';
      if (dom.renameTaskBtn) dom.renameTaskBtn.disabled = true;
      if (dom.deleteTaskBtn) dom.deleteTaskBtn.disabled = true;
      return;
    }
    dom.taskPicker.innerHTML = state.tasks.map(task =>
      `<option value="${escapeHtml(task.id)}">${escapeHtml(taskDisplayTitle(task))} · ${escapeHtml(statusLabel(task.status))}</option>`
    ).join('');
    if (state.currentTask) dom.taskPicker.value = state.currentTask.id;
    const hasCurrent = Boolean(state.currentTask?.id || dom.taskPicker.value);
    if (dom.renameTaskBtn) dom.renameTaskBtn.disabled = !hasCurrent;
    if (dom.deleteTaskBtn) dom.deleteTaskBtn.disabled = !hasCurrent;
  }

  function renderTemplates() {
    syncTemplateSelections(false);
    if (!state.templates.length) {
      dom.templateList.innerHTML = '<span class="muted">暂无 DOCX 模板</span>';
      renderTemplateManager();
      return;
    }
    const selected = state.templates.filter(item => state.selectedTemplateIds.has(item.id));
    dom.templateList.innerHTML = selected.length ? selected.map(item => `
      <label class="template-item" title="${escapeHtml(item.file_name)}">
        <input type="checkbox" value="${escapeHtml(item.id)}" checked>
        <span>${escapeHtml(item.file_name)}</span>
      </label>`).join('') : '<span class="muted">未勾选模板</span>';
    dom.templateList.querySelectorAll('input[type="checkbox"]').forEach(input => {
      input.addEventListener('change', () => {
        if (input.checked) state.selectedTemplateIds.add(input.value); else state.selectedTemplateIds.delete(input.value);
        renderTemplates();
      });
    });
    renderTemplateManager();
  }

  function renderTemplateManager() {
    if (!dom.templateManagerList) return;
    if (!state.templates.length) {
      dom.templateManagerList.innerHTML = '<div class="empty compact">暂无已上传 DOCX</div>';
      return;
    }
    dom.templateManagerList.innerHTML = state.templates.map(item => {
      const checked = state.selectedTemplateIds.has(item.id);
      return `
        <div class="template-manager-item" data-template-id="${escapeHtml(item.id)}">
          <div class="template-manager-main">
            <strong title="${escapeHtml(item.file_name)}">${escapeHtml(item.file_name)}</strong>
            <span>${fmtSize(item.size_bytes)} · ${fmtTime(item.updated_at || item.created_at)}</span>
          </div>
          <div class="template-manager-actions">
            <button class="mini-btn ${checked ? 'primary' : ''}" type="button" data-template-toggle="${escapeHtml(item.id)}">${checked ? '已勾选' : '勾选'}</button>
            <button class="mini-btn" type="button" data-template-preview="${escapeHtml(item.id)}">预览</button>
            <button class="mini-btn" type="button" data-template-rename="${escapeHtml(item.id)}">重命名</button>
            <button class="mini-btn danger" type="button" data-template-delete="${escapeHtml(item.id)}">删除</button>
          </div>
        </div>`;
    }).join('');
  }

  function openTemplateModal() {
    renderTemplateManager();
    dom.templateModal.classList.remove('hidden');
  }

  function closeTemplateModal() {
    dom.templateModal.classList.add('hidden');
  }

  function closeDocxPreviewModal() {
    dom.docxPreviewModal.classList.add('hidden');
  }

  async function previewTemplate(templateId) {
    const item = state.templates.find(template => template.id === templateId);
    dom.docxPreviewTitle.textContent = 'DOCX 文字预览';
    dom.docxPreviewFile.textContent = item?.file_name || templateId;
    dom.docxPreviewText.textContent = '正在读取……';
    dom.docxPreviewModal.classList.remove('hidden');
    const payload = await api(`/api/capture-agent/templates/${encodeURIComponent(templateId)}`);
    const detail = payload.item || {};
    dom.docxPreviewFile.textContent = detail.file_name || item?.file_name || templateId;
    dom.docxPreviewText.textContent = detail.text || detail.markdown || '该 DOCX 暂无可预览文字';
  }

  async function renameTemplate(templateId) {
    const item = state.templates.find(template => template.id === templateId);
    const name = window.prompt('输入新的模板名称', item?.file_name || '');
    if (name === null) return;
    const payload = await api(`/api/capture-agent/templates/${encodeURIComponent(templateId)}/rename`, { method: 'PUT', body: { name } });
    state.templates = state.templates.map(template => template.id === templateId ? payload.item : template);
    state.selectedTemplateIds.add(templateId);
    renderTemplates();
    showToast('模板已重命名');
  }

  async function deleteTemplate(templateId) {
    const item = state.templates.find(template => template.id === templateId);
    if (!window.confirm(`确认删除模板“${item?.file_name || templateId}”？`)) return;
    await api(`/api/capture-agent/templates/${encodeURIComponent(templateId)}`, { method: 'DELETE' });
    state.templates = state.templates.filter(template => template.id !== templateId);
    state.selectedTemplateIds.delete(templateId);
    renderTemplates();
    showToast('模板已删除');
  }

  function renderEmptyTask() {
    state.currentTask = null;
    state.selectedNodeId = null;
    dom.currentTaskTitle.textContent = '新建采集任务';
    dom.planVersion.textContent = '-';
    setStatus(dom.taskStatus, 'idle');
    dom.overallProgressText.textContent = '0%';
    dom.overallProgressBar.style.width = '0%';
    dom.planNodeList.innerHTML = '<div class="empty compact">创建任务后显示执行节点</div>';
    dom.detailEmpty.classList.remove('hidden');
    dom.detailContent.classList.add('hidden');
    clearReasoning();
    dom.artifactList.innerHTML = '<span class="muted">完成后显示结果与报告</span>';
    disconnectLiveSpectrum();
    resetLiveSpectrum();
    dom.liveSpectrumCard.classList.add('hidden');
    hideApprovalModal();
    updateActions();
  }

  function renderTask() {
    const task = state.currentTask;
    if (!task) return renderEmptyTask();
    dom.currentTaskTitle.textContent = taskDisplayTitle(task);
    dom.planVersion.textContent = `v${task.plan_version || 1}${task.plan_locked ? ' · 已锁定' : ''}`;
    const planner = task.planner || state.runtime || {};
    dom.plannerModel.textContent = `${planner.model || '规划服务'} · ${planner.endpoint || ''}`;
    setStatus(dom.taskStatus, task.status);
    const progress = Math.max(0, Math.min(100, Number(task.progress || 0)));
    dom.overallProgressText.textContent = `${progress}%`;
    dom.overallProgressBar.style.width = `${progress}%`;
    renderNodes();
    renderSelectedNode();
    renderArtifacts();
    syncLiveSpectrumFromTask();
    renderApprovalModal();
    updateActions();
    dom.taskPicker.value = task.id;
  }

  function renderApprovalModal() {
    const task = state.currentTask;
    if (!task || task.status !== 'awaiting_approval') {
      hideApprovalModal();
      return;
    }
    if (state.approvalDirty && state.approvalModalTaskId === task.id && !dom.approvalModal.classList.contains('hidden')) {
      return;
    }
    const prepare = task.plan_candidate?.prepare_result || {};
    const constraints = task.constraints || {};
    const values = {
      room_name: prepare.room_name ?? constraints.room_name ?? '',
      freq_start_mhz: prepare.freq_start_mhz ?? constraints.freq_start_mhz ?? '',
      freq_stop_mhz: prepare.freq_stop_mhz ?? constraints.freq_stop_mhz ?? '',
      freq_step_mhz: prepare.freq_step_mhz ?? constraints.freq_step_mhz ?? '',
      dwell_ms: prepare.dwell_ms ?? (constraints.dwell_time_sec ? Number(constraints.dwell_time_sec) * 1000 : ''),
      repeat_count: prepare.repeat_count ?? constraints.repeat_count ?? '',
      sample_rate: prepare.sample_rate ?? constraints.sample_rate ?? '',
      bandwidth: prepare.bandwidth ?? constraints.bandwidth ?? '',
      gain: prepare.gain ?? constraints.gain ?? '',
      antenna: prepare.antenna ?? constraints.antenna ?? '',
    };
    dom.approvalParamGrid.innerHTML = [
      ...APPROVAL_FIELDS.map(field => `
        <label class="param-item editable-param">
          <span>${escapeHtml(field.label)}</span>
          <input data-approval-param="${escapeHtml(field.key)}" type="${field.type}" step="${field.step || ''}" value="${escapeHtml(values[field.key] ?? '')}">
        </label>`),
      `<div class="param-item readonly-param"><span>计划 ID</span><strong title="${escapeHtml(prepare.plan_id || '-')}">${escapeHtml(prepare.plan_id || '-')}</strong></div>`,
      `<div class="param-item readonly-param"><span>聚合方式</span><strong>${escapeHtml(prepare.aggregation || '-')}</strong></div>`,
      `<div class="param-item readonly-param"><span>预计耗时</span><strong>${escapeHtml(prepare.estimated_total_sec ?? '-')} s</strong></div>`,
    ].join('');
    dom.approvalParamGrid.querySelectorAll('[data-approval-param]').forEach(input => {
      input.addEventListener('input', () => {
        state.approvalDirty = true;
        dom.approvalParamStatus.textContent = '参数已修改，批准前请保存';
      });
    });
    const steps = (task.nodes || [])
      .filter(node => !['template_analysis', 'instruction_analysis', 'plan_generation', 'approval'].includes(node.id))
      .map(node => node.title || node.id);
    dom.approvalFlow.innerHTML = steps.map((step, index) => `
      <span class="flow-step"><b>${index + 1}</b>${escapeHtml(step)}</span>${index < steps.length - 1 ? '<span class="flow-arrow">→</span>' : ''}
    `).join('') || '<span class="muted">等待生成固定工具流程</span>';
    dom.approvalParamStatus.textContent = '';
    setStatus(dom.approvalModalStatus, task.status);
    dom.approvalModal.classList.remove('hidden');
    state.approvalModalTaskId = task.id;
    state.approvalDirty = false;
  }

  function hideApprovalModal() {
    dom.approvalModal.classList.add('hidden');
    state.approvalModalTaskId = null;
    state.approvalDirty = false;
    if (dom.approvalParamStatus) dom.approvalParamStatus.textContent = '';
  }

  function renderNodes() {
    const task = state.currentTask;
    if (!task?.nodes?.length) {
      dom.planNodeList.innerHTML = '<div class="empty compact">正在生成计划</div>';
      return;
    }
    dom.planNodeList.innerHTML = task.nodes.map((node, index) => `
      <div class="plan-node ${escapeHtml(node.status)} ${node.id === state.selectedNodeId ? 'selected' : ''}" data-node-id="${escapeHtml(node.id)}">
        <span class="node-index">${node.status === 'completed' ? '✓' : String(index + 1).padStart(2, '0')}</span>
        <div class="node-main">
          <strong>${escapeHtml(node.title)}</strong>
          <p>${escapeHtml(node.summary || node.description || '')}</p>
          <div class="node-mini-progress"><b style="width:${Number(node.progress || 0)}%"></b></div>
        </div>
        <span class="node-state">${escapeHtml(statusLabel(node.status))}</span>
      </div>`).join('');
    dom.planNodeList.querySelectorAll('[data-node-id]').forEach(item => item.addEventListener('click', () => {
      state.selectedNodeId = item.dataset.nodeId;
      renderNodes();
      renderSelectedNode();
    }));
  }

  function renderSelectedNode() {
    const task = state.currentTask;
    const node = task?.nodes?.find(item => item.id === state.selectedNodeId) || task?.nodes?.find(item => item.id === task.current_node_id) || task?.nodes?.[0];
    if (!node) {
      dom.detailEmpty.classList.remove('hidden');
      dom.detailContent.classList.add('hidden');
      dom.liveSpectrumCard.classList.add('hidden');
      clearReasoning();
      return;
    }
    state.selectedNodeId = node.id;
    dom.detailEmpty.classList.add('hidden');
    dom.detailContent.classList.remove('hidden');
    dom.detailTitle.textContent = node.title;
    dom.detailSubtitle.textContent = node.description || '';
    setStatus(dom.detailStatus, node.status);
    dom.nodeProgress.textContent = `${Number(node.progress || 0)}%`;
    dom.nodeAttempts.textContent = String(node.attempts || 0);
    dom.nodeStarted.textContent = fmtTime(node.started_at);
    dom.nodeEnded.textContent = fmtTime(node.ended_at);
    dom.nodeSummary.textContent = node.summary || '等待执行';
    dom.nodeSummaryTime.textContent = fmtTime(node.updated_at);
    dom.nodeDescription.textContent = node.description || '-';
    dom.nodeCriteria.innerHTML = (node.success_criteria || []).map(item => `<div class="criteria-item">${escapeHtml(item)}</div>`).join('') || '<span class="muted">未设置</span>';
    const deps = node.dependencies || [];
    dom.nodeDependencies.innerHTML = deps.length ? deps.map(dep => `<span class="chip">${escapeHtml(task.nodes.find(n => n.id === dep)?.title || dep)}</span>`).join('') : '<span class="chip">无前置依赖</span>';
    dom.nodeTool.textContent = node.tool_name || node.allowed_tool || (node.kind ? `分析节点 · ${node.kind}` : '系统节点');
    dom.nodeInputs.textContent = safeJson(node.inputs || {});
    dom.nodeOutputs.textContent = safeJson(node.outputs || {});

    const logs = node.logs || [];
    dom.logCount.textContent = `${logs.length} 条`;
    dom.nodeLogs.innerHTML = logs.length ? logs.slice(-30).reverse().map(entry => `
      <div class="log-entry ${escapeHtml(entry.level || 'info')}">
        <time>${escapeHtml(fmtTime(entry.timestamp))}</time>
        <span class="log-level">${escapeHtml(entry.level || 'info')}</span>
        <span>${escapeHtml(entry.message || '')}</span>
      </div>`).join('') : '<span class="muted">暂无日志</span>';

    dom.nodeNote.value = node.notes || '';
    dom.noteSaveStatus.textContent = '';
    if ((node.tool_name === 'generate_usrp_task_code' || node.tool_name === 'run_autonomous_usrp_task') && task.generated_code) {
      dom.codeCard.classList.remove('hidden');
      dom.generatedCode.textContent = task.generated_code;
    } else {
      dom.codeCard.classList.add('hidden');
      dom.generatedCode.textContent = '';
    }
    updateLiveSpectrumCard(node);
    renderReasoning(node);
  }

  function renderReasoning(node) {
    const reasoning = String(node?.outputs?.llm_reasoning || '');
    const structured = String(node?.outputs?.llm_live_output || '');
    dom.structuredStream.textContent = structured || '尚无输出';
    if (!reasoning) {
      dom.reasoningEmpty.classList.remove('hidden');
      dom.reasoningStream.classList.add('hidden');
      dom.reasoningStream.textContent = '';
      return;
    }
    dom.reasoningEmpty.classList.add('hidden');
    dom.reasoningStream.classList.remove('hidden');
    dom.reasoningStream.textContent = reasoning;
    dom.reasoningStream.classList.toggle('streaming', node.status === 'in_progress');
    dom.reasoningStream.scrollTop = dom.reasoningStream.scrollHeight;
  }

  function clearReasoning() {
    dom.reasoningEmpty.classList.remove('hidden');
    dom.reasoningStream.classList.add('hidden');
    dom.reasoningStream.classList.remove('streaming');
    dom.reasoningStream.textContent = '';
    dom.structuredStream.textContent = '尚无输出';
  }

  function appendLiveDelta(event) {
    const task = state.currentTask;
    if (!task || !event.node_id) return;
    const node = task.nodes?.find(item => item.id === event.node_id);
    if (!node) return;
    node.outputs ||= {};
    const delta = String(event.data?.delta || '');
    if (event.type === 'llm_reasoning_delta' && delta) {
      node.outputs.llm_reasoning = String(node.outputs.llm_reasoning || '') + delta;
      if (state.selectedNodeId === node.id) {
        dom.reasoningEmpty.classList.add('hidden');
        dom.reasoningStream.classList.remove('hidden');
        dom.reasoningStream.classList.add('streaming');
        dom.reasoningStream.textContent += delta;
        dom.reasoningStream.scrollTop = dom.reasoningStream.scrollHeight;
      }
    }
    if (event.type === 'llm_content_delta' && delta) {
      node.outputs.llm_live_output = String(node.outputs.llm_live_output || '') + delta;
      if (state.selectedNodeId === node.id) {
        if (dom.structuredStream.textContent === '尚无输出') dom.structuredStream.textContent = '';
        dom.structuredStream.textContent += delta;
        dom.structuredStream.scrollTop = dom.structuredStream.scrollHeight;
      }
    }
    if (event.type === 'llm_reasoning_done' && state.selectedNodeId === node.id) {
      dom.reasoningStream.classList.remove('streaming');
    }
  }

  function renderArtifacts() {
    const artifacts = state.currentTask?.artifacts || [];
    if (!artifacts.length) {
      dom.artifactList.innerHTML = '<span class="muted">完成后显示结果与报告</span>';
      return;
    }
    dom.artifactList.innerHTML = artifacts.map(item => {
      const isNpz = String(item.file_name || '').toLowerCase().endsWith('.npz');
      return `
      <div class="artifact-item">
        <div><strong title="${escapeHtml(item.file_name)}">${escapeHtml(item.file_name)}</strong><span>${escapeHtml(item.kind)} · ${fmtSize(item.size_bytes)}</span></div>
        <div class="artifact-actions">
          ${item.spectrum_url ? `<button class="artifact-spectrum-btn" type="button" data-spectrum-url="${escapeHtml(item.spectrum_url)}" data-spectrum-name="${escapeHtml(item.file_name)}">频谱可视化</button>` : ''}
          ${isNpz ? `<button class="artifact-judge-btn" type="button" data-judge-artifact="${escapeHtml(item.id)}">研判</button>` : ''}
          <a href="${escapeHtml(item.download_url)}">下载</a>
        </div>
      </div>`;
    }).join('');
  }

  async function loadBaselines() {
    const payload = await api('/api/capture-agent/baselines');
    state.baselines = payload.items || [];
    const selected = payload.selected || {};
    const settings = payload.settings || {};
    state.selectedBaselineId = selected.npz_id || (state.baselines.find(item => item.selected) || {}).id || '';
    if (dom.baselineThresholdInput) dom.baselineThresholdInput.value = settings.threshold_db ?? selected.threshold_db ?? 8;
    if (dom.baselineDcBinsInput) dom.baselineDcBinsInput.value = settings.dc_exclusion_bins ?? selected.dc_exclusion_bins ?? 5;
    if (dom.baselineEdgeBinsInput) dom.baselineEdgeBinsInput.value = settings.edge_exclusion_bins ?? selected.edge_exclusion_bins ?? 2;
    renderBaselineList();
  }

  function renderBaselineList() {
    const items = state.baselines || [];
    if (!items.length) {
      dom.baselineList.innerHTML = '<div class="empty compact">暂无可用 .npz 文件。完成一次采集后，产物会出现在这里。</div>';
      return;
    }
    dom.baselineList.innerHTML = items.map(item => {
      const checked = item.id === state.selectedBaselineId || item.selected;
      return `
        <label class="baseline-item ${checked ? 'selected' : ''}">
          <input type="radio" name="baselineNpz" value="${escapeHtml(item.id)}" ${checked ? 'checked' : ''}>
          <div class="baseline-main">
            <strong title="${escapeHtml(item.file_name)}">${escapeHtml(item.file_name)}</strong>
            <span>${escapeHtml(item.task_display_title || item.task_title || item.source || '')} · ${fmtSize(item.size_bytes)} · ${escapeHtml(item.updated_at || item.created_at || '')}</span>
          </div>
          ${checked ? '<span class="baseline-current-badge">当前基线</span>' : '<span class="baseline-meta">可选</span>'}
        </label>`;
    }).join('');
    dom.baselineList.querySelectorAll('input[name="baselineNpz"]').forEach(input => {
      input.addEventListener('change', () => { state.selectedBaselineId = input.value; renderBaselineList(); });
    });
  }

  async function openBaselineModal() {
    dom.baselineModal.classList.remove('hidden');
    dom.baselineList.innerHTML = '<div class="empty compact">正在读取 .npz 文件……</div>';
    dom.baselineSaveStatus.textContent = '';
    await loadBaselines();
  }

  function closeBaselineModal() {
    dom.baselineModal.classList.add('hidden');
  }

  async function saveSelectedBaseline() {
    const selected = dom.baselineList.querySelector('input[name="baselineNpz"]:checked');
    if (!selected) return showToast('请先选择一个 .npz 文件作为基线', true);
    const payload = {
      npz_id: selected.value,
      threshold_db: Number(dom.baselineThresholdInput.value || 8),
      dc_exclusion_bins: Number(dom.baselineDcBinsInput.value || 5),
      edge_exclusion_bins: Number(dom.baselineEdgeBinsInput.value || 2),
    };
    dom.baselineSaveStatus.textContent = '正在保存……';
    await api('/api/capture-agent/baselines/current', { method: 'PUT', body: payload });
    dom.baselineSaveStatus.textContent = '已保存当前场所基线';
    showToast('当前场所基线已更新');
    await loadBaselines();
  }

  async function launchArtifactJudgement(artifactId) {
    if (!state.currentTask?.id || !artifactId) return;
    const data = await api(`/api/capture-agent/tasks/${encodeURIComponent(state.currentTask.id)}/artifacts/${encodeURIComponent(artifactId)}/judge`, { method: 'POST', body: {} });
    showToast(data.message || '已创建研判任务');
    window.location.href = data.redirect_url || '/chat?auto_judge=1';
  }

  function updateActions() {
    const status = state.currentTask?.status;
    dom.approveTaskBtn.disabled = status !== 'awaiting_approval';
    dom.pauseTaskBtn.disabled = !['planning', 'running'].includes(status);
    dom.resumeTaskBtn.disabled = status !== 'paused';
    dom.cancelTaskBtn.disabled = !status || ['completed', 'failed', 'cancelled'].includes(status);
  }

  function selectedTemplateIds() {
    syncTemplateSelections(false);
    return [...state.selectedTemplateIds];
  }

  async function refreshTaskList() {
    const payload = await api('/api/capture-agent/tasks?limit=50');
    state.tasks = payload.items || [];
    renderTaskPicker();
  }

  async function selectTask(taskId) {
    if (!taskId) return renderEmptyTask();
    closeEventStream();
    disconnectLiveSpectrum();
    resetLiveSpectrum();
    const payload = await api(`/api/capture-agent/tasks/${encodeURIComponent(taskId)}`);
    state.currentTask = payload.item;
    state.selectedNodeId = state.currentTask.current_node_id || state.currentTask.nodes?.[0]?.id || null;
    state.eventSeq = Number(state.currentTask.events?.at(-1)?.seq || 0);
    renderTask();
    openEventStream();
  }

  async function createOrReviseTask() {
    const instruction = dom.instructionInput.value.trim();
    if (!instruction || state.isSending) return;
    if (!state.runtime?.available) return showToast('规划服务未配置，无法生成计划', true);
    state.isSending = true;
    dom.sendInstructionBtn.disabled = true;
    try {
      const current = state.currentTask;
      let payload;
      if (current && ['awaiting_approval', 'failed', 'completed', 'cancelled'].includes(current.status) && current.id === dom.taskPicker.value) {
        payload = await api(`/api/capture-agent/tasks/${encodeURIComponent(current.id)}/messages`, { method: 'POST', body: { content: instruction } });
        showToast(`已创建计划版本 v${payload.item.plan_version}`);
      } else {
        payload = await api('/api/capture-agent/tasks', { method: 'POST', body: { instruction, template_ids: selectedTemplateIds() } });
        showToast('任务已创建，正在生成计划');
      }
      dom.instructionInput.value = '';
      await refreshTaskList();
      await selectTask(payload.item.id);
    } catch (error) {
      showToast(error.message, true);
    } finally {
      state.isSending = false;
      dom.sendInstructionBtn.disabled = !state.runtime?.available;
    }
  }

  async function performAction(action) {
    const task = state.currentTask;
    if (!task) return;
    try {
      const payload = await api(`/api/capture-agent/tasks/${encodeURIComponent(task.id)}/${action}`, { method: 'POST' });
      state.currentTask = payload.item;
      if (action === 'approve' || action === 'resume') {
        state.selectedNodeId = state.currentTask.current_node_id || state.selectedNodeId;
      }
      renderTask();
      await refreshTaskList();
      showToast(action === 'approve' ? '计划已锁定，开始执行' : `任务已${{pause:'暂停', resume:'继续', cancel:'停止'}[action] || action}`);
      if (action === 'approve' || action === 'cancel') hideApprovalModal();
      if (action === 'resume' || action === 'approve') openEventStream();
    } catch (error) { showToast(error.message, true); }
  }

  function collectApprovalParameters() {
    const params = {};
    dom.approvalParamGrid.querySelectorAll('[data-approval-param]').forEach(input => {
      params[input.dataset.approvalParam] = input.value;
    });
    return params;
  }

  async function saveApprovalParameters(force = false) {
    const task = state.currentTask;
    if (!task || task.status !== 'awaiting_approval') return false;
    if (!force && !state.approvalDirty) return true;
    dom.saveApprovalParamsBtn.disabled = true;
    dom.approvePlanModalBtn.disabled = true;
    dom.approvalParamStatus.textContent = '正在保存参数并重新生成流程……';
    try {
      const payload = await api(`/api/capture-agent/tasks/${encodeURIComponent(task.id)}/approval-parameters`, {
        method: 'PUT', body: { parameters: collectApprovalParameters() },
      });
      state.currentTask = payload.item;
      state.selectedNodeId = state.currentTask.current_node_id || state.selectedNodeId;
      state.approvalDirty = false;
      renderTask();
      dom.approvalParamStatus.textContent = '参数已保存';
      await refreshTaskList();
      showToast('审批参数已更新');
      return true;
    } catch (error) {
      dom.approvalParamStatus.textContent = '';
      showToast(error.message, true);
      return false;
    } finally {
      dom.saveApprovalParamsBtn.disabled = false;
      dom.approvePlanModalBtn.disabled = false;
    }
  }

  async function approveCurrentPlan() {
    if (state.approvalDirty) {
      const saved = await saveApprovalParameters(true);
      if (!saved) return;
    }
    await performAction('approve');
  }

  async function renameCurrentTask() {
    const taskId = state.currentTask?.id || dom.taskPicker.value;
    if (!taskId) return;
    const current = state.currentTask || state.tasks.find(task => task.id === taskId);
    const title = window.prompt('输入新的任务名称', current?.title || current?.instruction || '');
    if (title === null) return;
    const payload = await api(`/api/capture-agent/tasks/${encodeURIComponent(taskId)}/rename`, { method: 'PUT', body: { title } });
    state.currentTask = payload.item;
    await refreshTaskList();
    renderTask();
    showToast('任务已重命名');
  }

  async function deleteCurrentTask() {
    const taskId = state.currentTask?.id || dom.taskPicker.value;
    if (!taskId) return;
    const current = state.currentTask || state.tasks.find(task => task.id === taskId);
    if (!window.confirm(`确认删除“${taskDisplayTitle(current)}”？`)) return;
    await api(`/api/capture-agent/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' });
    closeEventStream();
    disconnectLiveSpectrum();
    await refreshTaskList();
    if (state.tasks.length) await selectTask(state.tasks[0].id); else renderEmptyTask();
    showToast('任务已删除');
  }

  async function rejectCurrentPlan() {
    const task = state.currentTask;
    if (!task || task.status !== 'awaiting_approval') return;
    await performAction('cancel');
  }

  async function uploadTemplates(files) {
    if (!files?.length) return;
    const invalid = [...files].find(file => !file.name.toLowerCase().endsWith('.docx'));
    if (invalid) return showToast('仅支持 DOCX 模板', true);
    const form = new FormData();
    [...files].forEach(file => form.append('files', file));
    dom.uploadStatus.textContent = `正在解析 ${files.length} 个模板`;
    try {
      const payload = await api('/api/capture-agent/templates', { method: 'POST', body: form });
      const uploaded = payload.items || [];
      uploaded.forEach(item => state.selectedTemplateIds.add(item.id));
      state.templates = [...uploaded, ...state.templates.filter(old => !uploaded.some(item => item.id === old.id))];
      renderTemplates();
      dom.uploadStatus.textContent = `已上传：${(payload.items || []).map(item => item.file_name).join('、')}`;
      showToast('模板解析完成');
    } catch (error) {
      dom.uploadStatus.textContent = '';
      showToast(error.message, true);
    } finally { dom.templateUpload.value = ''; }
  }

  async function saveNote() {
    const task = state.currentTask;
    if (!task || !state.selectedNodeId) return;
    dom.saveNodeNoteBtn.disabled = true;
    try {
      const payload = await api(`/api/capture-agent/tasks/${encodeURIComponent(task.id)}/nodes/${encodeURIComponent(state.selectedNodeId)}/note`, {
        method: 'PUT', body: { note: dom.nodeNote.value },
      });
      state.currentTask = payload.item;
      dom.noteSaveStatus.textContent = '已保存';
    } catch (error) { showToast(error.message, true); }
    finally { dom.saveNodeNoteBtn.disabled = false; }
  }

  function setConnection(text, connected = false) {
    dom.connectionStatus.textContent = text;
    dom.connectionStatus.className = `live-state${connected ? ' connected' : ''}`;
  }

  function openEventStream() {
    closeEventStream();
    const task = state.currentTask;
    if (!task || ['completed', 'failed', 'cancelled'].includes(task.status)) {
      setConnection('通道已结束');
      return;
    }
    const source = new EventSource(`/api/capture-agent/tasks/${encodeURIComponent(task.id)}/events?after=${state.eventSeq}`);
    state.eventSource = source;
    setConnection('连接中');
    source.onopen = () => setConnection('实时连接', true);
    source.addEventListener('update', raw => {
      try {
        const event = JSON.parse(raw.data);
        state.eventSeq = Math.max(state.eventSeq, Number(event.seq || 0));
        appendLiveDelta(event);
        handleLiveSpectrumEvent(event);
        scheduleTaskRefresh();
      } catch (_) { /* ignore malformed event */ }
    });
    source.addEventListener('heartbeat', () => setConnection('实时连接', true));
    source.addEventListener('terminal', () => { scheduleTaskRefresh(true); closeEventStream(); });
    source.onerror = () => {
      setConnection('正在重连');
      if (state.currentTask && ['completed', 'failed', 'cancelled'].includes(state.currentTask.status)) closeEventStream();
    };
  }

  function closeEventStream() {
    if (state.eventSource) state.eventSource.close();
    state.eventSource = null;
  }

  function scheduleTaskRefresh(immediate = false) {
    clearTimeout(state.refreshTimer);
    state.refreshTimer = setTimeout(async () => {
      if (!state.currentTask) return;
      try {
        const payload = await api(`/api/capture-agent/tasks/${encodeURIComponent(state.currentTask.id)}`);
        const previousCurrent = state.currentTask.current_node_id;
        state.currentTask = payload.item;
        if (!state.selectedNodeId || state.selectedNodeId === previousCurrent) state.selectedNodeId = state.currentTask.current_node_id;
        state.eventSeq = Math.max(state.eventSeq, Number(state.currentTask.events?.at(-1)?.seq || 0));
        renderTask();
        if (['completed', 'failed', 'cancelled'].includes(state.currentTask.status)) {
          closeEventStream();
          await refreshTaskList();
        }
      } catch (error) { setConnection(`刷新失败`); }
    }, immediate ? 0 : 140);
  }

  function newTaskMode() {
    closeEventStream();
    closeSpectrumModal();
    dom.taskPicker.value = '';
    renderEmptyTask();
    dom.instructionInput.focus();
  }

  function getPendingDetectTask() {
    try {
      const raw = localStorage.getItem('deepem.captureAgent.pendingFromDetect');
      return raw ? JSON.parse(raw) : null;
    } catch (_) {
      return null;
    }
  }

  async function consumePendingDetectTask() {
    const pending = getPendingDetectTask();
    if (!pending || pending.source !== 'detect_flow') return false;
    if (Array.isArray(pending.template_ids)) {
      state.selectedTemplateIds = new Set(pending.template_ids.filter(id => (state.templates || []).some(t => t.id === id)));
      renderTemplates();
    }
    renderEmptyTask();
    dom.instructionInput.value = String(pending.instruction || '').trim();
    if (!dom.instructionInput.value) return false;
    if (!state.runtime?.available) {
      showToast('已带入智能检测任务，请配置规划服务后点击生成计划', true);
      return true;
    }
    localStorage.removeItem('deepem.captureAgent.pendingFromDetect');
    await createOrReviseTask();
    return true;
  }

  async function loadInitial() {
    try {
      const [runtime, templates, tasks] = await Promise.all([
        api('/api/capture-agent/runtime'), api('/api/capture-agent/templates'), api('/api/capture-agent/tasks?limit=50'),
      ]);
      state.runtime = runtime.item || null;
      state.templates = templates.items || [];
      syncTemplateSelections(true);
      state.tasks = tasks.items || [];
      renderRuntime(); renderTemplates(); renderTaskPicker();
      const consumedPending = await consumePendingDetectTask();
      if (!consumedPending) {
        if (state.tasks.length) await selectTask(state.tasks[0].id); else renderEmptyTask();
      }
    } catch (error) { showToast(error.message, true); }
  }

  dom.taskPicker.addEventListener('change', () => selectTask(dom.taskPicker.value).catch(error => showToast(error.message, true)));
  dom.newTaskBtn.addEventListener('click', newTaskMode);
  dom.renameTaskBtn.addEventListener('click', () => renameCurrentTask().catch(error => showToast(error.message, true)));
  dom.deleteTaskBtn.addEventListener('click', () => deleteCurrentTask().catch(error => showToast(error.message, true)));
  dom.baselineManagerBtn.addEventListener('click', () => openBaselineModal().catch(error => showToast(error.message, true)));
  dom.closeBaselineModalBtn.addEventListener('click', closeBaselineModal);
  dom.baselineModal.addEventListener('click', event => { if (event.target === dom.baselineModal) closeBaselineModal(); });
  dom.refreshBaselineListBtn.addEventListener('click', () => loadBaselines().catch(error => showToast(error.message, true)));
  dom.saveBaselineBtn.addEventListener('click', () => saveSelectedBaseline().catch(error => showToast(error.message, true)));
  dom.approveTaskBtn.addEventListener('click', () => renderApprovalModal());
  dom.pauseTaskBtn.addEventListener('click', () => performAction('pause'));
  dom.resumeTaskBtn.addEventListener('click', () => performAction('resume'));
  dom.cancelTaskBtn.addEventListener('click', () => performAction('cancel'));
  dom.approvePlanModalBtn.addEventListener('click', () => approveCurrentPlan().catch(error => showToast(error.message, true)));
  dom.saveApprovalParamsBtn.addEventListener('click', () => saveApprovalParameters(true).catch(error => showToast(error.message, true)));
  dom.rejectPlanBtn.addEventListener('click', () => rejectCurrentPlan().catch(error => showToast(error.message, true)));
  dom.templateDetailBtn.addEventListener('click', openTemplateModal);
  dom.closeTemplateModalBtn.addEventListener('click', closeTemplateModal);
  dom.templateModal.addEventListener('click', event => { if (event.target === dom.templateModal) closeTemplateModal(); });
  dom.closeDocxPreviewBtn.addEventListener('click', closeDocxPreviewModal);
  dom.docxPreviewModal.addEventListener('click', event => { if (event.target === dom.docxPreviewModal) closeDocxPreviewModal(); });
  dom.templateManagerList.addEventListener('click', event => {
    const toggle = event.target.closest('[data-template-toggle]');
    const preview = event.target.closest('[data-template-preview]');
    const rename = event.target.closest('[data-template-rename]');
    const del = event.target.closest('[data-template-delete]');
    const id = toggle?.dataset.templateToggle || preview?.dataset.templatePreview || rename?.dataset.templateRename || del?.dataset.templateDelete;
    if (!id) return;
    if (toggle) {
      if (state.selectedTemplateIds.has(id)) state.selectedTemplateIds.delete(id); else state.selectedTemplateIds.add(id);
      renderTemplates();
      return;
    }
    if (preview) previewTemplate(id).catch(error => showToast(error.message, true));
    if (rename) renameTemplate(id).catch(error => showToast(error.message, true));
    if (del) deleteTemplate(id).catch(error => showToast(error.message, true));
  });
  dom.templateUpload.addEventListener('change', event => uploadTemplates(event.target.files));
  dom.sendInstructionBtn.addEventListener('click', createOrReviseTask);
  dom.instructionInput.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); createOrReviseTask(); }
  });
  dom.saveNodeNoteBtn.addEventListener('click', saveNote);
  dom.artifactList.addEventListener('click', event => {
    const spectrumButton = event.target.closest('[data-spectrum-url]');
    if (spectrumButton) {
      openSpectrumModal(spectrumButton.dataset.spectrumUrl, spectrumButton.dataset.spectrumName);
      return;
    }
    const judgeButton = event.target.closest('[data-judge-artifact]');
    if (judgeButton) {
      launchArtifactJudgement(judgeButton.dataset.judgeArtifact).catch(error => showToast(error.message, true));
    }
  });
  dom.closeSpectrumModalBtn.addEventListener('click', closeSpectrumModal);
  dom.spectrumModal.addEventListener('click', event => { if (event.target === dom.spectrumModal) closeSpectrumModal(); });
  window.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    if (!dom.spectrumModal.classList.contains('hidden')) closeSpectrumModal();
    else if (!dom.baselineModal.classList.contains('hidden')) closeBaselineModal();
    else if (!dom.docxPreviewModal.classList.contains('hidden')) closeDocxPreviewModal();
    else if (!dom.templateModal.classList.contains('hidden')) closeTemplateModal();
  });
  window.addEventListener('resize', () => { if (state.lastFftMessage) renderLiveFftMessage(state.lastFftMessage, false); });
  dom.copyCodeBtn.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(dom.generatedCode.textContent || ''); showToast('代码已复制'); }
    catch (_) { showToast('复制失败', true); }
  });
  window.addEventListener('beforeunload', () => { closeEventStream(); disconnectLiveSpectrum(); closeSpectrumModal(); });
  loadInitial();
})();
