(() => {
  'use strict';

  const state = {
    templates: [], tasks: [], currentTask: null, selectedNodeId: null, selectedTemplateIds: new Set(),
    eventSource: null, eventSeq: 0, refreshTimer: null, isSending: false, runtime: null,
    approvalModalTaskId: null, approvalDirty: false, usrpSocket: null, usrpSocketUrl: '', waterfallRows: [],
    lastFftMessage: null, liveFrameCount: 0, spectrumObjectUrl: '', baselines: [], selectedBaselineId: '',
    probeAutoOpenedKey: '', probeModalDismissed: false, artifactDetailId: '',
    probeDetailData: null, probeDetailTab: 'wifi_ap', probeDetailPage: 1, probeDetailPageSize: 25,
    deviceDashboard: null, deviceSockets: {}, deviceWaterfalls: {}, probeLive: {}, probeTabs: {}, deviceRefreshBusy: false, probePollTimer: null, previewNodeId: '', previewDotsTimer: null,
    reasoningModePreference: (() => { try { return localStorage.getItem('deepem.captureAgent.reasoningMode') === 'deep' ? 'deep' : 'fast'; } catch (_) { return 'fast'; } })(),
  };

  const el = id => document.getElementById(id);
  const dom = {
    currentTaskTitle: el('currentTaskTitle'), plannerModel: el('plannerModel'), planVersion: el('planVersion'), taskStatus: el('taskStatus'),
    overallProgressText: el('overallProgressText'), overallProgressBar: el('overallProgressBar'),
    modelModeBadge: el('modelModeBadge'), probeAssistBtn: el('probeAssistBtn'), baselineManagerBtn: el('baselineManagerBtn'), approveTaskBtn: el('approveTaskBtn'), pauseTaskBtn: el('pauseTaskBtn'), resumeTaskBtn: el('resumeTaskBtn'), cancelTaskBtn: el('cancelTaskBtn'),
    newTaskBtn: el('newTaskBtn'), taskPicker: el('taskPicker'), renameTaskBtn: el('renameTaskBtn'), deleteTaskBtn: el('deleteTaskBtn'), planNodeList: el('planNodeList'),
    detailEmpty: el('detailEmpty'), detailContent: el('detailContent'), detailTitle: el('detailTitle'), detailSubtitle: el('detailSubtitle'), detailStatus: el('detailStatus'),
    nodeProgress: el('nodeProgress'), nodeAttempts: el('nodeAttempts'), nodeStarted: el('nodeStarted'), nodeEnded: el('nodeEnded'),
    nodeSummary: el('nodeSummary'), nodeSummaryTime: el('nodeSummaryTime'), nodeDescription: el('nodeDescription'), nodeCriteria: el('nodeCriteria'),
    nodeDependencies: el('nodeDependencies'), nodeTool: el('nodeTool'), nodeLogs: el('nodeLogs'), logCount: el('logCount'),
    nodeInputs: el('nodeInputs'), nodeOutputs: el('nodeOutputs'), codeCard: el('codeCard'), generatedCode: el('generatedCode'), copyCodeBtn: el('copyCodeBtn'),
    nodeNote: el('nodeNote'), saveNodeNoteBtn: el('saveNodeNoteBtn'), noteSaveStatus: el('noteSaveStatus'),
    reasoningEmpty: el('reasoningEmpty'), reasoningSummary: el('reasoningSummary'), reasoningSummaryMeta: el('reasoningSummaryMeta'),
    reasoningSummaryTitle: el('reasoningSummaryTitle'), reasoningDetails: el('reasoningDetails'), reasoningDetailsTitle: el('reasoningDetailsTitle'),
    reasoningStream: el('reasoningStream'), structuredStream: el('structuredStream'), connectionStatus: el('connectionStatus'),
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
    probeAssistModal: el('probeAssistModal'), probeAssistStatus: el('probeAssistStatus'), probeAssistPrompt: el('probeAssistPrompt'),
    probeAssistDevices: el('probeAssistDevices'), probeAssistTimeline: el('probeAssistTimeline'), probeAssistSummary: el('probeAssistSummary'),
    probeAssistReasoningDetails: el('probeAssistReasoningDetails'), probeAssistReasoning: el('probeAssistReasoning'),
    openProbeDetailsBtn: el('openProbeDetailsBtn'), probeAssistDetailsHint: el('probeAssistDetailsHint'),
    probeAssistError: el('probeAssistError'), closeProbeAssistBtn: el('closeProbeAssistBtn'), declineProbeAssistBtn: el('declineProbeAssistBtn'), confirmProbeAssistBtn: el('confirmProbeAssistBtn'),
    probeDetailModal: el('probeDetailModal'), probeDetailTitle: el('probeDetailTitle'), probeDetailSubtitle: el('probeDetailSubtitle'),
    probeDetailStats: el('probeDetailStats'), probeDetailSummary: el('probeDetailSummary'), probeDetailTabs: el('probeDetailTabs'),
    probeDetailSearch: el('probeDetailSearch'), probeDetailTableMeta: el('probeDetailTableMeta'), probeDetailTable: el('probeDetailTable'),
    probeDetailPrev: el('probeDetailPrev'), probeDetailNext: el('probeDetailNext'), probeDetailPage: el('probeDetailPage'),
    probeDetailIntegrity: el('probeDetailIntegrity'), probeDetailDownload: el('probeDetailDownload'), closeProbeDetailBtn: el('closeProbeDetailBtn'),
    artifactDetailModal: el('artifactDetailModal'), artifactDetailTitle: el('artifactDetailTitle'), artifactDetailSubtitle: el('artifactDetailSubtitle'),
    artifactDetailMeta: el('artifactDetailMeta'), artifactDetailPreview: el('artifactDetailPreview'), artifactDetailDownload: el('artifactDetailDownload'),
    closeArtifactDetailBtn: el('closeArtifactDetailBtn'),
    evidenceChainBtn: el('evidenceChainBtn'), queryDevicesBtn: el('queryDevicesBtn'), deviceQueryStatus: el('deviceQueryStatus'),
    deviceSummaryStrip: el('deviceSummaryStrip'), deviceCarousel: el('deviceCarousel'),
    deviceHistoryModal: el('deviceHistoryModal'), deviceHistoryTitle: el('deviceHistoryTitle'), deviceHistorySubtitle: el('deviceHistorySubtitle'),
    deviceHistoryBody: el('deviceHistoryBody'), closeDeviceHistoryBtn: el('closeDeviceHistoryBtn'),
    evidenceChainModal: el('evidenceChainModal'), evidenceChainSubtitle: el('evidenceChainSubtitle'), evidenceChainPolicy: el('evidenceChainPolicy'),
    evidenceChainBody: el('evidenceChainBody'), closeEvidenceChainBtn: el('closeEvidenceChainBtn'),
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
  const reasoningMode = task => String(task?.reasoning_mode || 'fast').toLowerCase() === 'deep' ? 'deep' : 'fast';
  function renderReasoningModeControl() {
    if (!dom.modelModeBadge) return;
    const mode = state.reasoningModePreference === 'deep' ? 'deep' : 'fast';
    dom.modelModeBadge.textContent = mode === 'deep' ? '深度思考模式' : '快速模式';
    dom.modelModeBadge.className = `capture-mode-badge ${mode}`;
    dom.modelModeBadge.title = `当前新任务使用${mode === 'deep' ? '深度思考模式' : '快速模式'}，点击切换`;
    dom.modelModeBadge.setAttribute('aria-pressed', mode === 'deep' ? 'true' : 'false');
  }

  function toggleReasoningMode() {
    state.reasoningModePreference = state.reasoningModePreference === 'deep' ? 'fast' : 'deep';
    try { localStorage.setItem('deepem.captureAgent.reasoningMode', state.reasoningModePreference); } catch (_) { /* ignore storage errors */ }
    renderReasoningModeControl();
    showToast(`新任务已切换为${state.reasoningModePreference === 'deep' ? '深度思考模式' : '快速模式'}`);
  }
  const probeAssistance = task => (task?.probe_assistance && typeof task.probe_assistance === 'object') ? task.probe_assistance : null;
  const probeAssistanceActive = task => ['awaiting_confirmation', 'queued', 'running'].includes(String(probeAssistance(task)?.status || ''));
  const taskStreamActive = task => Boolean(task) && (!['completed', 'failed', 'cancelled'].includes(task.status) || probeAssistanceActive(task));

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
    if (!element) return;
    element.textContent = statusLabel(status);
    element.className = `status-pill ${status || 'idle'}`;
  }

  function renderRuntime() {
    const runtime = state.runtime || {};
    if (dom.plannerModel) dom.plannerModel.textContent = runtime.available ? `${runtime.model} · ${runtime.endpoint}` : '规划服务未配置';
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

  function closeProbeAssistModal() {
    dom.probeAssistModal?.classList.add('hidden');
  }

  function openProbeAssistModal() {
    if (!probeAssistance(state.currentTask)?.enabled) return;
    renderProbeAssistance(false);
    dom.probeAssistModal?.classList.remove('hidden');
  }

  function renderProbeAssistance(allowAutoOpen = true) {
    const task = state.currentTask;
    const auxiliary = probeAssistance(task);
    const enabled = Boolean(auxiliary?.enabled);
    dom.probeAssistBtn?.classList.toggle('hidden', !enabled);
    if (!enabled) {
      closeProbeAssistModal();
      return;
    }

    const status = String(auxiliary.status || 'waiting_for_usrp');
    const labels = {
      waiting_for_usrp: '等待 USRP', awaiting_confirmation: '等待确认', queued: '准备中', running: '采集中',
      completed: '采集完成', partial_failed: '辅助证据部分失败', declined: '已取消', not_selected: '未选择',
    };
    if (dom.probeAssistStatus) {
      dom.probeAssistStatus.textContent = auxiliary.status_label || labels[status] || status;
      dom.probeAssistStatus.className = `status-pill probe-${status}`;
    }

    const probes = Array.isArray(auxiliary.selected_probes) ? auxiliary.selected_probes : [];
    if (dom.probeAssistDevices) {
      dom.probeAssistDevices.innerHTML = probes.length
        ? probes.map(item => `<span class="probe-device-chip"><b>${escapeHtml(item.probe_id || item.device_ip || '探针')}</b><small>${escapeHtml(String(item.status || 'UNKNOWN').toUpperCase())}</small></span>`).join('')
        : '<span class="muted">未记录探针设备</span>';
    }

    const steps = Array.isArray(auxiliary.steps) ? auxiliary.steps : [];
    if (dom.probeAssistTimeline) {
      dom.probeAssistTimeline.innerHTML = steps.map((step, index) => `<div class="probe-assist-step ${escapeHtml(step.status || 'pending')}">
        <span class="probe-step-index">${index + 1}</span>
        <div><strong>${escapeHtml(step.title || step.id || '步骤')}</strong><p>${escapeHtml(step.message || '等待执行')}</p></div>
        <em>${escapeHtml(statusLabel(step.status || 'pending'))}</em>
      </div>`).join('') || '<div class="empty compact">暂无探针流程步骤</div>';
    }

    const mode = reasoningMode(task);
    const summary = String(auxiliary.summary_stream || auxiliary.summary || '').trim();
    if (dom.probeAssistSummary) {
      if (summary) dom.probeAssistSummary.textContent = summary;
      else if (status === 'awaiting_confirmation') dom.probeAssistSummary.textContent = 'USRP 采集结果已经成功完成。确认后，智能体会依次调用三个探针接口，并在此实时显示模型输出摘要。';
      else if (['queued', 'running'].includes(status)) dom.probeAssistSummary.textContent = '正在采集并生成辅助证据摘要…';
      else if (status === 'declined') dom.probeAssistSummary.textContent = '用户已选择暂不调用探针；已成功完成的 USRP 结果不受影响。';
      else dom.probeAssistSummary.textContent = '等待探针辅助证据流程。';
    }
    if (dom.probeAssistReasoningDetails) dom.probeAssistReasoningDetails.classList.toggle('hidden', mode === 'fast');
    if (dom.probeAssistReasoning) dom.probeAssistReasoning.textContent = mode === 'fast' ? '快速模式不输出详细思考过程。' : (auxiliary.reasoning || '尚无详细模型过程');
    const detailCounts = auxiliary.details?.counts || {};
    const detailsReady = ['completed', 'partial_failed'].includes(status);
    if (dom.openProbeDetailsBtn) dom.openProbeDetailsBtn.disabled = !detailsReady;
    if (dom.probeAssistDetailsHint) {
      dom.probeAssistDetailsHint.textContent = detailsReady
        ? `${Number(detailCounts.targets || 0)} 个目标 · ${Number(detailCounts.observations || 0)} 条当前观测 · 表格化查看`
        : '采集完成后可查看设备、目标与观测表格';
    }

    const errors = Array.isArray(auxiliary.errors) ? auxiliary.errors : [];
    if (dom.probeAssistError) {
      dom.probeAssistError.classList.toggle('hidden', errors.length === 0);
      dom.probeAssistError.innerHTML = errors.length
        ? `<strong>辅助证据部分失败</strong><ul>${errors.map(item => `<li>${escapeHtml(item.message || safeJson(item))}</li>`).join('')}</ul><p>USRP 主采集结果仍保持成功完成。</p>`
        : '';
    }

    if (dom.probeAssistPrompt) {
      dom.probeAssistPrompt.textContent = status === 'awaiting_confirmation'
        ? 'USRP 采集已完成。是否确认调用 WiFi/蓝牙探针以获取更多信息？'
        : (auxiliary.status_label || 'WiFi/蓝牙探针辅助证据流程');
    }
    const awaiting = status === 'awaiting_confirmation';
    const working = ['queued', 'running'].includes(status);
    dom.confirmProbeAssistBtn?.classList.toggle('hidden', !awaiting && !working);
    dom.declineProbeAssistBtn?.classList.toggle('hidden', !awaiting);
    if (dom.confirmProbeAssistBtn) {
      dom.confirmProbeAssistBtn.disabled = working;
      dom.confirmProbeAssistBtn.textContent = working ? '正在采集…' : '确认调用探针';
    }
    if (dom.declineProbeAssistBtn) dom.declineProbeAssistBtn.disabled = false;

    const autoOpenKey = `${task.id}:${status}`;
    if (allowAutoOpen && ['awaiting_confirmation', 'queued', 'running'].includes(status) && state.probeAutoOpenedKey !== autoOpenKey) {
      state.probeAutoOpenedKey = autoOpenKey;
      dom.probeAssistModal?.classList.remove('hidden');
    }
  }

  const PROBE_KIND_LABELS = { wifi_ap: 'WiFi 热点', wifi_client: 'WiFi 客户端', bluetooth: '蓝牙设备' };

  function closeProbeDetailModal() {
    dom.probeDetailModal?.classList.add('hidden');
  }

  async function openProbeDetailModal() {
    if (!state.currentTask?.id) return;
    state.probeDetailData = null;
    state.probeDetailPage = 1;
    dom.probeDetailModal?.classList.remove('hidden');
    if (dom.probeDetailSubtitle) dom.probeDetailSubtitle.textContent = '正在读取完整证据文件……';
    if (dom.probeDetailTable) dom.probeDetailTable.innerHTML = '<div class="probe-detail-loading">正在加载完整证据……</div>';
    try {
      state.probeDetailData = await api(`/api/capture-agent/tasks/${encodeURIComponent(state.currentTask.id)}/probe-assistance/details`);
      renderProbeDetailModal();
    } catch (error) {
      if (dom.probeDetailTable) dom.probeDetailTable.innerHTML = `<div class="probe-detail-empty">${escapeHtml(error.message)}</div>`;
      showToast(error.message, true);
    }
  }

  function probeDetailRows(tab) {
    const result = state.probeDetailData?.result || {};
    if (tab === 'devices') return Array.isArray(result.devices) ? result.devices : [];
    if (tab === 'observations') return Array.isArray(result.observations) ? result.observations : [];
    return (Array.isArray(result.targets) ? result.targets : []).filter(item => item.kind === tab);
  }

  function probeDetailColumns(tab) {
    if (tab === 'devices') return [
      ['探针 ID', row => row.probe_id || row.device_ip || '-'], ['状态', row => String(row.status || '-').toUpperCase()],
      ['最近接收', row => fmtTime(row.last_received_at)], ['连接次数', row => row.connection_count ?? '-'],
      ['接收消息', row => row.received_message_count ?? '-'], ['解析消息', row => row.parsed_message_count ?? '-'],
    ];
    if (tab === 'observations') return [
      ['类型', row => PROBE_KIND_LABELS[row.kind] || row.kind || '-'], ['名称 / SSID', row => row.name || row.ssid || row.connected_ssid || '--'],
      ['MAC', row => row.mac || row.bssid || '-'], ['当前 RSSI', row => row.rssi_latest == null ? '-' : `${row.rssi_latest} dBm`],
      ['距离', row => row.distance == null ? '-' : `${row.distance} m`], ['观测次数', row => row.observation_count ?? '-'],
      ['信道', row => row.channel || '-'], ['最近出现', row => fmtTime(row.last_seen)],
    ];
    return [
      ['名称 / SSID', row => row.name || row.ssid || '--'], ['MAC / BSSID', row => row.mac || row.bssid || '-'],
      ['当前 RSSI', row => row.rssi_latest == null ? '-' : `${row.rssi_latest} dBm`], ['距离', row => row.distance == null ? '-' : `${row.distance} m`],
      ['信道', row => row.channel || '-'], ['厂商 / 类型', row => row.vendor || row.device_type || '-'],
      ['加密 / 连接', row => row.encryption || row.connected_ssid || (row.hidden ? '隐藏网络' : '-')], ['最近出现', row => fmtTime(row.last_seen)],
    ];
  }

  function renderProbeDetailModal() {
    const payload = state.probeDetailData || {};
    const result = payload.result || {};
    const counts = result.counts || payload.overview?.counts || {};
    const byKind = counts.by_kind || {};
    if (dom.probeDetailSubtitle) dom.probeDetailSubtitle.textContent = `${payload.status_label || '证据已读取'} · 活跃窗口 ${Number(result.active_minutes || 0)} 分钟`;
    if (dom.probeDetailSummary) dom.probeDetailSummary.textContent = payload.summary || '暂无模型摘要，完整证据仍可按表格查看。';
    if (dom.probeDetailStats) dom.probeDetailStats.innerHTML = [
      ['在线探针', `${Number(counts.online_devices || 0)} / ${Number(counts.devices || 0)}`],
      ['WiFi 热点', Number(byKind.wifi_ap?.targets || 0)], ['WiFi 客户端', Number(byKind.wifi_client?.targets || 0)],
      ['蓝牙设备', Number(byKind.bluetooth?.targets || 0)], ['当前观测', Number(counts.observations || 0)],
    ].map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join('');
    if (dom.probeDetailDownload) {
      dom.probeDetailDownload.classList.toggle('hidden', !payload.artifact?.download_url);
      dom.probeDetailDownload.href = payload.artifact?.download_url || '#';
    }
    dom.probeDetailTabs?.querySelectorAll('[data-probe-detail-tab]').forEach(button => button.classList.toggle('active', button.dataset.probeDetailTab === state.probeDetailTab));

    const query = String(dom.probeDetailSearch?.value || '').trim().toLowerCase();
    const rows = probeDetailRows(state.probeDetailTab).filter(row => !query || Object.values(row || {}).some(value => String(value ?? '').toLowerCase().includes(query)));
    const pageSize = state.probeDetailPageSize;
    const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
    state.probeDetailPage = Math.max(1, Math.min(pageCount, state.probeDetailPage));
    const pageRows = rows.slice((state.probeDetailPage - 1) * pageSize, state.probeDetailPage * pageSize);
    const columns = probeDetailColumns(state.probeDetailTab);
    if (dom.probeDetailTableMeta) dom.probeDetailTableMeta.textContent = `${rows.length} 条 · 每页 ${pageSize} 条`;
    if (dom.probeDetailPage) dom.probeDetailPage.textContent = `第 ${state.probeDetailPage} / ${pageCount} 页`;
    if (dom.probeDetailPrev) dom.probeDetailPrev.disabled = state.probeDetailPage <= 1;
    if (dom.probeDetailNext) dom.probeDetailNext.disabled = state.probeDetailPage >= pageCount;
    if (dom.probeDetailTable) {
      dom.probeDetailTable.innerHTML = pageRows.length ? `<table class="probe-detail-table"><thead><tr>${columns.map(([label]) => `<th>${escapeHtml(label)}</th>`).join('')}</tr></thead><tbody>${pageRows.map(row => `<tr>${columns.map(([, getter]) => `<td title="${escapeHtml(getter(row))}">${escapeHtml(getter(row))}</td>`).join('')}</tr>`).join('')}</tbody></table>` : '<div class="probe-detail-empty">没有匹配的数据</div>';
    }
    const overview = payload.overview || {};
    const hash = overview.integrity?.full_result_sha256 || overview.integrity?.full_result_sha256 || '';
    const budget = overview.llm_budget || {};
    const coverage = overview.coverage || {};
    if (dom.probeDetailIntegrity) dom.probeDetailIntegrity.innerHTML = `
      <strong>证据完整性与上下文控制</strong>
      <span>原始证据 SHA-256：${escapeHtml(hash ? `${hash.slice(0, 20)}…${hash.slice(-12)}` : '未记录')}</span>
      <span>送模证据包约 ${escapeHtml(budget.estimated_input_tokens ?? '-')} tokens；原始目标/观测保留 ${escapeHtml(coverage.targets_total ?? counts.targets ?? 0)} / ${escapeHtml(coverage.observations_total ?? counts.observations ?? 0)} 条。</span>`;
  }

  async function decideProbeAssistance(approved) {
    const task = state.currentTask;
    if (!task) return;
    dom.confirmProbeAssistBtn && (dom.confirmProbeAssistBtn.disabled = true);
    dom.declineProbeAssistBtn && (dom.declineProbeAssistBtn.disabled = true);
    try {
      const payload = await api(`/api/capture-agent/tasks/${encodeURIComponent(task.id)}/probe-assistance/confirm`, {
        method: 'POST', body: { approved: Boolean(approved) },
      });
      state.currentTask = payload.item;
      renderTask();
      if (approved) {
        showToast('已确认调用 WiFi/蓝牙探针，正在采集辅助证据');
        openEventStream();
      } else {
        showToast('已取消探针辅助采集，USRP 结果保持完成');
        closeProbeAssistModal();
      }
      await refreshTaskList();
    } catch (error) {
      showToast(error.message, true);
      renderProbeAssistance(false);
    }
  }

  function safeDomId(value) {
    return String(value || 'device').replace(/[^0-9A-Za-z_-]+/g, '_');
  }

  function deviceStatusMode(device) {
    const status = String(device?.status || '').toUpperCase();
    if (!device?.online) return 'offline';
    if (['BUSY', 'RUNNING', 'CAPTURING'].includes(status)) return 'busy';
    if (['ERROR', 'FAILED'].includes(status)) return 'error';
    return 'online';
  }

  function disconnectDeviceSockets() {
    Object.values(state.deviceSockets || {}).forEach(socket => {
      if (socket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(socket.readyState)) {
        try { socket.close(); } catch (_) { /* ignore */ }
      }
    });
    state.deviceSockets = {};
  }

  function stopProbePolling() {
    if (state.probePollTimer) clearTimeout(state.probePollTimer);
    state.probePollTimer = null;
  }

  function deviceHistoryHtml(device) {
    const history = Array.isArray(device.history) ? device.history : [];
    if (!history.length) return '<div class="muted">暂无调用记录</div>';
    return `<div class="device-history-list">${history.slice(0, 12).map((item, index) => `
      <div class="device-history-item" data-device-history="${escapeHtml(device.device_id)}" data-device-type="${escapeHtml(device.device_type)}" data-history-index="${index}">
        <div><strong>${escapeHtml(item.task_display_title || (item.task_number ? `任务${item.task_number}：${item.task_title || item.task_id || '采集任务'}` : (item.task_title || item.task_id || '采集任务')))}</strong><p>${escapeHtml(item.summary || item.instruction || '')}</p></div>
        <time>${escapeHtml(fmtTime(item.ended_at || item.started_at))}</time>
      </div>`).join('')}</div>`;
  }

  function usrpDeviceCard(device) {
    const detail = device.details || {};
    const config = detail.current_config || {};
    const devConfig = detail.dev_config || {};
    const id = safeDomId(device.device_id);
    const mode = deviceStatusMode(device);
    const freq = config.freq ?? config.center_freq ?? '-';
    const rate = config.sample_rate ?? '-';
    return `<article class="device-card usrp-device-card" data-device-card="${escapeHtml(device.device_id)}">
      <div class="device-card-head">
        <div class="device-card-name"><i class="device-dot ${mode}"></i><div><strong>${escapeHtml(device.display_name || 'USRP')}</strong><span>${escapeHtml(device.device_id)}</span></div></div>
        <span class="device-status-chip ${mode}">${escapeHtml(device.status || 'UNKNOWN')}</span>
      </div>
      <div class="device-card-body">
        <div class="device-card-section">
          <div class="device-card-section-title"><strong>实时频谱 / 瀑布图</strong><span id="deviceWsState-${id}">${device.stream_info?.ws_url ? '准备订阅 FFT' : '暂无实时流地址'}</span></div>
          <div class="device-live-chart-label"><span>频谱图</span><small>当前 FFT 功率谱</small></div>
          <div class="device-spectrum-wrap"><canvas id="deviceSpectrum-${id}" aria-label="USRP 实时频谱图"></canvas></div>
          <div class="device-live-chart-label waterfall-label"><span>瀑布图</span><small>时间 × 频率</small></div>
          <div class="device-waterfall-wrap"><canvas id="deviceWaterfall-${id}" aria-label="USRP 实时瀑布图"></canvas><div class="device-waterfall-overlay"><span>FFT POWER</span><span>${escapeHtml(device.device_id)}</span></div></div>
          <div id="deviceWaterfallMeta-${id}" class="device-live-caption">等待 USRP 实时 FFT 数据</div>
        </div>
        <div class="device-card-section">
          <div class="device-card-section-title"><strong>设备状态</strong><span>${device.available_for_new_task ? '可被新任务选择' : '当前不可占用'}</span></div>
          <div class="device-meta-grid">
            <div><span>当前频率</span><strong>${escapeHtml(freq)}</strong></div>
            <div><span>采样率</span><strong>${escapeHtml(rate)}</strong></div>
            <div><span>任务 ID</span><strong>${escapeHtml(detail.task_id || '-')}</strong></div>
            <div><span>能力</span><strong>${escapeHtml(Object.keys(devConfig).slice(0,3).join(' / ') || 'USRP')}</strong></div>
          </div>
        </div>
        <div class="device-card-section">
          <div class="device-card-section-title"><strong>调用记录</strong><span>${(device.history || []).length} 条</span></div>
          ${deviceHistoryHtml(device)}
        </div>
      </div>
    </article>`;
  }

  function probeDeviceCard(device) {
    const detail = device.details || {};
    const id = safeDomId(device.device_id);
    const mode = deviceStatusMode(device);
    const tab = state.probeTabs[device.device_id] || 'wifi_ap';
    return `<article class="device-card probe-device-card" data-device-card="${escapeHtml(device.device_id)}">
      <div class="device-card-head">
        <div class="device-card-name"><i class="device-dot ${mode}"></i><div><strong>${escapeHtml(device.display_name || 'WiFi/蓝牙探针')}</strong><span>${escapeHtml(device.device_id)}</span></div></div>
        <span class="device-status-chip ${mode}">${escapeHtml(device.status || 'UNKNOWN')}</span>
      </div>
      <div class="device-card-body">
        <div class="device-card-section">
          <div class="device-card-section-title"><strong>实时探针采集</strong><span id="probeLiveStamp-${id}">正在读取…</span></div>
          <div class="probe-live-tabs">
            ${[['wifi_ap','WiFi 热点'],['wifi_client','WiFi 客户端'],['bluetooth','蓝牙设备']].map(([key,label]) => `<button class="probe-live-tab ${tab === key ? 'active' : ''}" data-probe-tab="${key}" data-probe-id="${escapeHtml(device.device_id)}" type="button">${label}</button>`).join('')}
          </div>
          <div id="probeLive-${id}" class="probe-live-list"><div class="muted">等待探针实时数据…</div></div>
        </div>
        <div class="device-card-section">
          <div class="device-card-section-title"><strong>探针状态</strong><span>${device.available_for_new_task ? '在线可用' : '不可用'}</span></div>
          <div class="device-meta-grid">
            <div><span>探针 ID</span><strong>${escapeHtml(device.device_id)}</strong></div>
            <div><span>位置</span><strong>${escapeHtml(detail.location || detail.area || '-')}</strong></div>
            <div><span>IP/接口</span><strong>${escapeHtml(detail.ip || detail.address || detail.interface || '-')}</strong></div>
            <div><span>最近在线</span><strong>${escapeHtml(detail.last_seen || detail.updated_at || '-')}</strong></div>
          </div>
        </div>
        <div class="device-card-section">
          <div class="device-card-section-title"><strong>调用记录</strong><span>${(device.history || []).length} 条</span></div>
          ${deviceHistoryHtml(device)}
        </div>
      </div>
    </article>`;
  }

  function waitingDeviceCard() {
    return `<article class="waiting-device-card"><div class="waiting-device-inner"><div class="waiting-device-icon">＋</div><strong>等待新设备接入</strong><span>新 USRP、WiFi/蓝牙探针或后续其他采集设备上线后，会在下一次查询时自动加入此工作台。</span></div></article>`;
  }

  function drawDeviceWaterfallPlaceholder(canvas, message = '等待实时 FFT 数据') {
    if (!canvas) return;
    const { width, height } = sizeCanvas(canvas, 420, 176);
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#041019'; ctx.fillRect(0,0,width,height);
    ctx.fillStyle = '#6e8996'; ctx.font = '10px sans-serif'; ctx.fillText(message, 12, 24);
    ctx.strokeStyle = 'rgba(255,255,255,.07)'; ctx.strokeRect(.5,.5,width-1,height-1);
  }

  function drawDeviceSpectrumPlaceholder(canvas, message = '等待实时 FFT 数据') {
    if (!canvas) return;
    const { width, height } = sizeCanvas(canvas, 420, 96);
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#041019'; ctx.fillRect(0,0,width,height);
    ctx.fillStyle = '#6e8996'; ctx.font = '10px sans-serif'; ctx.fillText(message, 12, 22);
    ctx.strokeStyle = 'rgba(255,255,255,.07)'; ctx.strokeRect(.5,.5,width-1,height-1);
  }

  function renderDeviceWaterfall(deviceId, message) {
    if (!message || message.type !== 'fft') return;
    const fft = (Array.isArray(message.fft_data) ? message.fft_data : []).map(Number).filter(Number.isFinite);
    if (!fft.length) return;
    const id = safeDomId(deviceId);
    const canvas = el(`deviceWaterfall-${id}`);
    const spectrum = el(`deviceSpectrum-${id}`);
    if (!canvas) return;
    const minValue = Math.min(...fft), maxValue = Math.max(...fft), span = Math.max(maxValue - minValue, 1e-6);

    if (spectrum) {
      const { width: spectrumWidth, height: spectrumHeight } = sizeCanvas(spectrum, 420, 96);
      const sctx = spectrum.getContext('2d');
      sctx.fillStyle = '#041019'; sctx.fillRect(0,0,spectrumWidth,spectrumHeight);
      sctx.strokeStyle = 'rgba(255,255,255,.065)'; sctx.lineWidth = 1;
      for (let line = 1; line < 4; line += 1) {
        const y = spectrumHeight * line / 4;
        sctx.beginPath(); sctx.moveTo(0, y); sctx.lineTo(spectrumWidth, y); sctx.stroke();
      }
      sctx.strokeStyle = '#67e5be'; sctx.lineWidth = 1.6; sctx.beginPath();
      fft.forEach((value, index) => {
        const x = fft.length <= 1 ? 0 : index / (fft.length - 1) * spectrumWidth;
        const y = spectrumHeight - 7 - (value - minValue) / span * (spectrumHeight - 16);
        if (index === 0) sctx.moveTo(x, y); else sctx.lineTo(x, y);
      });
      sctx.stroke();
      sctx.fillStyle = '#83a1ae'; sctx.font = '8px sans-serif';
      sctx.fillText(`${maxValue.toFixed(1)} dB`, 7, 11);
      sctx.fillText(`${minValue.toFixed(1)} dB`, 7, spectrumHeight - 4);
      sctx.strokeStyle='rgba(255,255,255,.08)'; sctx.strokeRect(.5,.5,spectrumWidth-1,spectrumHeight-1);
    }

    const row = fft.map(value => Math.round((value - minValue) / span * 255));
    const rows = state.deviceWaterfalls[deviceId] || [];
    rows.push(row); state.deviceWaterfalls[deviceId] = rows.slice(-90);
    const { width, height } = sizeCanvas(canvas, 420, 176);
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#041019'; ctx.fillRect(0,0,width,height);
    const visibleRows = state.deviceWaterfalls[deviceId];
    visibleRows.forEach((values, rowIndex) => {
      const y = rowIndex / Math.max(visibleRows.length,1) * height;
      const h = Math.ceil(height / Math.max(visibleRows.length,1));
      values.forEach((value, colIndex) => {
        const [r,g,b] = powerColor(value);
        ctx.fillStyle = `rgb(${r},${g},${b})`;
        const x = colIndex / values.length * width;
        ctx.fillRect(x, y, Math.ceil(width / values.length), h);
      });
    });
    ctx.strokeStyle='rgba(255,255,255,.08)'; ctx.strokeRect(.5,.5,width-1,height-1);
    const center = Number(message.freq || 0), sampleRate = Number(message.sample_rate || 0);
    const meta = el(`deviceWaterfallMeta-${id}`);
    if (meta) meta.textContent = `中心 ${fmtFreq(center)} ｜ FFT ${message.fft_size || fft.length} ｜ ${minValue.toFixed(1)} ~ ${maxValue.toFixed(1)} dB`;
  }

  function connectDeviceWaterfall(device) {
    const url = String(device?.stream_info?.ws_url || '').trim();
    const id = safeDomId(device.device_id);
    const canvas = el(`deviceWaterfall-${id}`);
    const spectrum = el(`deviceSpectrum-${id}`);
    if (!url || !device.online || typeof WebSocket === 'undefined') {
      drawDeviceWaterfallPlaceholder(canvas, url ? '设备当前离线' : '暂无实时 FFT 地址');
      drawDeviceSpectrumPlaceholder(spectrum, url ? '设备当前离线' : '暂无实时 FFT 地址');
      return;
    }
    drawDeviceWaterfallPlaceholder(canvas, '正在连接实时 FFT…');
    drawDeviceSpectrumPlaceholder(spectrum, '正在连接实时 FFT…');
    try {
      const socket = new WebSocket(url);
      state.deviceSockets[device.device_id] = socket;
      socket.onopen = () => { const target = el(`deviceWsState-${id}`); if (target) target.textContent='实时流已连接'; };
      socket.onmessage = event => {
        try {
          const message = JSON.parse(event.data);
          if (message.type === 'fft') renderDeviceWaterfall(device.device_id, message);
          else if (message.type === 'status') { const target=el(`deviceWsState-${id}`); if (target) target.textContent=String(message.status || '已连接'); }
        } catch (_) { /* ignore malformed frame */ }
      };
      socket.onerror = () => { const target=el(`deviceWsState-${id}`); if (target) target.textContent='实时流连接失败'; };
      socket.onclose = () => { if (state.deviceSockets[device.device_id] === socket) delete state.deviceSockets[device.device_id]; };
    } catch (error) {
      drawDeviceWaterfallPlaceholder(canvas, `实时流连接失败：${error.message}`);
      drawDeviceSpectrumPlaceholder(spectrum, `实时流连接失败：${error.message}`);
    }
  }

  function renderDeviceDashboard() {
    if (!dom.deviceCarousel) return;
    const dashboard = state.deviceDashboard;
    if (!dashboard) {
      dom.deviceCarousel.className = 'device-carousel';
      dom.deviceCarousel.innerHTML = '<div class="device-empty-state"><div class="empty-icon">⌁</div><strong>尚未查询设备</strong><span>点击“查询可用设备”自动探测 USRP 与 WiFi/蓝牙探针。</span></div>';
      return;
    }
    disconnectDeviceSockets();
    const devices = Array.isArray(dashboard.devices) ? dashboard.devices : [];
    const onlineCount = devices.filter(item => item.online).length;
    dom.deviceCarousel.className = `device-carousel${onlineCount > 2 ? ' scrolling' : ''}`;
    dom.deviceCarousel.innerHTML = devices.map(device => device.device_type === 'usrp' ? usrpDeviceCard(device) : probeDeviceCard(device)).join('') + waitingDeviceCard();
    if (dom.deviceQueryStatus) dom.deviceQueryStatus.textContent = `上次查询 ${fmtTime(dashboard.queried_at)}`;
    const usrpCount = devices.filter(item => item.device_type === 'usrp' && item.online).length;
    const probeCount = devices.filter(item => item.device_type === 'wifi_bluetooth_probe' && item.online).length;
    const errorCount = (dashboard.errors || []).length;
    dom.deviceSummaryStrip.innerHTML = `<span><i class="device-dot online"></i> 在线 ${onlineCount}</span><span>USRP ${usrpCount}</span><span>WiFi/蓝牙探针 ${probeCount}</span>${errorCount ? `<span><i class="device-dot error"></i> ${errorCount} 个接口异常</span>` : ''}`;
    dom.deviceCarousel.querySelectorAll('[data-device-history]').forEach(row => row.addEventListener('click', () => openDeviceHistory(row.dataset.deviceType, row.dataset.deviceHistory, Number(row.dataset.historyIndex || 0))));
    dom.deviceCarousel.querySelectorAll('[data-probe-tab]').forEach(button => button.addEventListener('click', () => {
      state.probeTabs[button.dataset.probeId] = button.dataset.probeTab;
      const card = button.closest('.probe-device-card');
      card?.querySelectorAll('[data-probe-tab]').forEach(item => item.classList.toggle('active', item === button));
      renderProbeLiveIntoCard(button.dataset.probeId);
    }));
    devices.filter(item => item.device_type === 'usrp').forEach(connectDeviceWaterfall);
    devices.filter(item => item.device_type === 'wifi_bluetooth_probe' && item.online).forEach(device => fetchProbeLive(device.device_id));
    scheduleProbePolling();
  }

  async function refreshDeviceDashboard(refresh = false) {
    if (state.deviceRefreshBusy) return;
    state.deviceRefreshBusy = true;
    stopProbePolling();
    if (dom.queryDevicesBtn) dom.queryDevicesBtn.disabled = true;
    if (dom.deviceQueryStatus) dom.deviceQueryStatus.textContent = refresh ? '正在扫描设备…' : '正在读取设备状态…';
    try {
      const payload = await api(`/api/capture-agent/devices?refresh=${refresh ? 'true' : 'false'}`);
      state.deviceDashboard = payload.item || { devices: [], errors: [] };
      renderDeviceDashboard();
      if (refresh) showToast(`设备查询完成，在线 ${state.deviceDashboard.online_count || 0} 台`);
    } catch (error) {
      if (dom.deviceQueryStatus) dom.deviceQueryStatus.textContent = '设备查询失败';
      if (refresh) showToast(error.message, true);
    } finally {
      state.deviceRefreshBusy = false;
      if (dom.queryDevicesBtn) dom.queryDevicesBtn.disabled = false;
    }
  }

  async function fetchProbeLive(probeId) {
    try {
      const payload = await api(`/api/capture-agent/devices/probes/${encodeURIComponent(probeId)}/live?page_size=40`);
      state.probeLive[probeId] = payload.item || {};
      renderProbeLiveIntoCard(probeId);
    } catch (error) {
      state.probeLive[probeId] = { error: error.message, items: {} };
      renderProbeLiveIntoCard(probeId);
    }
  }

  function renderProbeLiveIntoCard(probeId) {
    const id = safeDomId(probeId), target = el(`probeLive-${id}`), stamp = el(`probeLiveStamp-${id}`);
    if (!target) return;
    const live = state.probeLive[probeId] || {};
    const tab = state.probeTabs[probeId] || 'wifi_ap';
    if (live.error) { target.innerHTML = `<div class="muted">${escapeHtml(live.error)}</div>`; if (stamp) stamp.textContent='读取失败'; return; }
    const block = live.items?.[tab] || {};
    const items = (block.targets || []).slice(0, 35);
    if (stamp) stamp.textContent = live.queried_at ? `更新 ${fmtTime(live.queried_at)} · ${block.target_total ?? items.length} 个` : '等待数据';
    if (!items.length) { target.innerHTML = '<div class="muted">当前未发现此类设备</div>'; return; }
    target.innerHTML = items.map(row => {
      const name = row.name || row.ssid || row.device_name || row.mac || row.bssid || '未命名设备';
      const identity = row.mac || row.bssid || row.vendor || row.connected_ssid || '-';
      const rssi = row.rssi_latest ?? row.rssi ?? '-';
      return `<div class="probe-live-row"><strong title="${escapeHtml(name)}">${escapeHtml(name)}</strong><span title="${escapeHtml(identity)}">${escapeHtml(identity)}</span><em>${escapeHtml(rssi === '-' ? '-' : `${rssi} dBm`)}</em></div>`;
    }).join('');
  }

  function scheduleProbePolling() {
    stopProbePolling();
    const probes = (state.deviceDashboard?.devices || []).filter(item => item.device_type === 'wifi_bluetooth_probe' && item.online);
    if (!probes.length) return;
    state.probePollTimer = setTimeout(async () => {
      for (const device of probes) await fetchProbeLive(device.device_id);
      scheduleProbePolling();
    }, 6000);
  }

  function findDashboardDevice(deviceType, deviceId) {
    return (state.deviceDashboard?.devices || []).find(item => item.device_type === deviceType && item.device_id === deviceId);
  }

  function openDeviceHistory(deviceType, deviceId, historyIndex) {
    const device = findDashboardDevice(deviceType, deviceId);
    const item = device?.history?.[historyIndex];
    if (!item || !dom.deviceHistoryModal) return;
    dom.deviceHistoryTitle.textContent = `${device.display_name || deviceId} · 调用详情`;
    dom.deviceHistorySubtitle.textContent = item.task_display_title || (item.task_number ? `任务${item.task_number}：${item.task_title || item.task_id || ''}` : (item.task_title || item.task_id || ''));
    const summary = deviceType === 'wifi_bluetooth_probe' ? (item.intelligent_summary || item.summary || '暂无智能研判摘要') : (item.summary || item.instruction || '暂无任务摘要');
    const artifacts = (item.artifacts || []).map(artifact => `<div class="device-history-artifact"><strong title="${escapeHtml(artifact.file_name || '')}">${escapeHtml(artifact.file_name || artifact.kind || '任务产物')}</strong><a href="${escapeHtml(artifact.download_url || '#')}" ${artifact.download_url ? 'target="_blank"' : ''}>查看</a></div>`).join('') || '<span class="muted">暂无任务产物</span>';
    dom.deviceHistoryBody.innerHTML = `
      <div class="device-history-overview">
        <div><span>任务</span><strong>${escapeHtml(item.task_title || item.task_id || '-')}</strong></div>
        <div><span>节点状态</span><strong>${escapeHtml(statusLabel(item.node_status))}</strong></div>
        <div><span>开始</span><strong>${escapeHtml(fmtTime(item.started_at))}</strong></div>
        <div><span>结束</span><strong>${escapeHtml(fmtTime(item.ended_at))}</strong></div>
      </div>
      <div class="device-history-summary">${escapeHtml(summary)}</div>
      ${item.evidence_set_id ? `<div class="device-history-summary">证据集：${escapeHtml(item.evidence_set_id)} · 详细探针记录已落库，可按需检索。</div>` : ''}
      <h3>${deviceType === 'usrp' ? '任务产物' : '相关产物'}</h3><div class="device-history-artifacts">${artifacts}</div>`;
    dom.deviceHistoryModal.classList.remove('hidden');
  }

  function closeDeviceHistory() { dom.deviceHistoryModal?.classList.add('hidden'); }

  async function openEvidenceChain() {
    const task = state.currentTask;
    if (!task || !dom.evidenceChainModal) return;
    dom.evidenceChainModal.classList.remove('hidden');
    dom.evidenceChainBody.innerHTML = '<div class="empty compact">正在构建设备—证据链…</div>';
    try {
      const payload = await api(`/api/capture-agent/tasks/${encodeURIComponent(task.id)}/device-evidence-chain`);
      renderEvidenceChain(payload.item || {});
    } catch (error) {
      dom.evidenceChainBody.innerHTML = `<div class="empty compact">${escapeHtml(error.message)}</div>`;
    }
  }

  function renderEvidenceChain(chain) {
    const policy = chain.association_policy || {};
    dom.evidenceChainPolicy.innerHTML = `<strong>关联规则：</strong> ${escapeHtml(policy.warning || '设备证据链用于汇聚线索，不自动把共现解释为同一物理设备。')}`;
    if (dom.evidenceChainSubtitle) dom.evidenceChainSubtitle.textContent = `${chain.device_count || 0} 个逻辑设备 · ${chain.generated_from_evidence_sets?.length || 0} 个探针证据集`;
    const devices = chain.devices || [];
    if (!devices.length) {
      dom.evidenceChainBody.innerHTML = '<div class="empty compact"><strong>暂未形成设备—证据链</strong><span>完成包含 WiFi/蓝牙探针的采集任务后，这里会按设备名称、MAC/BSSID 和跨模态任务证据统一陈列。</span></div>';
      return;
    }
    dom.evidenceChainBody.innerHTML = devices.map(device => {
      const evidence = (device.evidence || []).slice(0, 120);
      return `<details class="evidence-device-card">
        <summary class="evidence-device-head">
          <div><h3>${escapeHtml(device.display_name || device.device_id)}</h3><p>${escapeHtml((device.identifiers || []).join(' · ') || '无强标识')} · IQ/频谱 ${Number(device.iq_spectrum_evidence_count || 0)} 条 · 总证据 ${Number(device.evidence_count || 0)} 条 · 身份置信 ${escapeHtml(device.identity_confidence || '-')}</p></div>
          <div class="evidence-modality-chips">${(device.modalities || []).map(item => `<span>${escapeHtml(item)}</span>`).join('')}</div>
        </summary>
        <div class="evidence-list">${evidence.map(item => {
          if (item.evidence_type === 'task_artifact') {
            const artifact = item.artifact || {};
            return `<div class="evidence-row"><span>${escapeHtml(item.modality || '任务产物')}</span><div><strong>${escapeHtml(artifact.file_name || artifact.kind || '任务产物')}</strong><br><span class="evidence-association-note">${escapeHtml(item.note || '')}</span></div><small>${artifact.download_url ? `<a href="${escapeHtml(artifact.download_url)}" target="_blank">查看产物</a>` : '任务级共现'}</small></div>`;
          }
          const identity = [item.name, item.mac, item.ssid, item.bssid, item.vendor].filter(Boolean).join(' · ');
          const tail = [item.probe_id, item.last_seen, item.rssi !== null && item.rssi !== undefined ? `${item.rssi} dBm` : ''].filter(Boolean).join(' · ');
          return `<div class="evidence-row"><span>${escapeHtml(item.modality || item.kind || '探针')}</span><div>${escapeHtml(identity || '探针记录')}</div><small>${escapeHtml(tail || item.evidence_set_id || '')}</small></div>`;
        }).join('')}</div>
      </details>`;
    }).join('');
  }

  function closeEvidenceChain() { dom.evidenceChainModal?.classList.add('hidden'); }

  function stopNodePreviewDots() {
    if (state.previewDotsTimer) clearInterval(state.previewDotsTimer);
    state.previewDotsTimer = null;
  }

  function removeNodePreview(clearState = true) {
    stopNodePreviewDots();
    document.querySelector('.node-preview-popover')?.remove();
    if (clearState) state.previewNodeId = '';
  }

  function nodePreviewSummary(node) {
    const text = String(node?.outputs?.llm_status_summary || node?.summary || node?.description || '等待执行').trim();
    return node?.status === 'in_progress' ? text.replace(/[。.!！?？…]+$/g, '') : text;
  }

  function startNodePreviewDots(pop, node) {
    stopNodePreviewDots();
    const target = pop?.querySelector('[data-node-preview-dots]');
    if (!target || node?.status !== 'in_progress') {
      if (target) target.textContent = '';
      return;
    }
    let count = 1;
    target.textContent = '.';
    state.previewDotsTimer = setInterval(() => {
      count = count >= 3 ? 1 : count + 1;
      target.textContent = '.'.repeat(count);
    }, 420);
  }

  function showNodePreview(node, anchor) {
    removeNodePreview(false);
    if (!node || !anchor) return;
    state.previewNodeId = node.id;
    const pop = document.createElement('div');
    pop.className = 'node-preview-popover';
    const tool = node.tool_name || node.allowed_tool || node.executor || node.kind || '系统节点';
    pop.innerHTML = `<div class="node-preview-head"><strong>${escapeHtml(node.title || node.id)}</strong><span class="node-state">${escapeHtml(statusLabel(node.status))}</span></div>
      <p><span data-node-preview-summary>${escapeHtml(nodePreviewSummary(node))}</span><span class="node-preview-live-dots" data-node-preview-dots aria-hidden="true"></span></p>
      <div class="node-preview-meta"><span>进度 ${Number(node.progress || 0)}%</span><span>${escapeHtml(tool)}</span><span>${escapeHtml(fmtTime(node.updated_at))}</span></div>
      <div class="node-preview-actions"><button data-node-preview-close type="button">关闭</button><button class="primary" data-node-preview-detail type="button">查看详细</button></div>`;
    const sidebar = dom.planNodeList.closest('.sidebar');
    sidebar.appendChild(pop);
    const listTop = dom.planNodeList.offsetTop;
    const targetTop = listTop + anchor.offsetTop - dom.planNodeList.scrollTop;
    const popHeight = Math.max(1, pop.offsetHeight || 132);
    pop.style.top = `${Math.max(8, targetTop - popHeight - 8)}px`;
    startNodePreviewDots(pop, node);
    pop.querySelector('[data-node-preview-close]').addEventListener('click', event => { event.stopPropagation(); removeNodePreview(true); });
    pop.querySelector('[data-node-preview-detail]').addEventListener('click', event => {
      event.stopPropagation();
      if (!state.currentTask) return;
      window.location.href = `/capture-agent/node-detail?task=${encodeURIComponent(state.currentTask.id)}&node=${encodeURIComponent(node.id)}`;
    });
  }

  function renderEmptyTask() {
    state.currentTask = null;
    state.selectedNodeId = null;
    state.probeAutoOpenedKey = '';
    removeNodePreview();
    renderReasoningModeControl();
    dom.probeAssistBtn?.classList.add('hidden');
    closeProbeAssistModal();
    if (dom.currentTaskTitle) dom.currentTaskTitle.textContent = '新建采集任务';
    if (dom.planVersion) dom.planVersion.textContent = '-';
    setStatus(dom.taskStatus, 'idle');
    if (dom.overallProgressText) dom.overallProgressText.textContent = '0%';
    dom.overallProgressBar.style.width = '0%';
    dom.planNodeList.innerHTML = '<div class="empty compact">创建任务后显示执行节点</div>';
    clearReasoning();
    dom.artifactList.innerHTML = '<span class="muted">完成后显示结果与报告</span>';
    disconnectLiveSpectrum();
    resetLiveSpectrum();
    dom.liveSpectrumCard?.classList.add('hidden');
    hideApprovalModal();
    updateActions();
  }
  function renderTask() {
    const task = state.currentTask;
    if (!task) return renderEmptyTask();
    if (dom.currentTaskTitle) dom.currentTaskTitle.textContent = taskDisplayTitle(task);
    renderReasoningModeControl();
    if (dom.planVersion) dom.planVersion.textContent = `v${task.plan_version || 1}${task.plan_locked ? ' · 已锁定' : ''}`;
    const planner = task.planner || state.runtime || {};
    if (dom.plannerModel) dom.plannerModel.textContent = `${planner.model || '规划服务'} · ${planner.endpoint || ''}`;
    setStatus(dom.taskStatus, task.status);
    const progress = Math.max(0, Math.min(100, Number(task.progress || 0)));
    if (dom.overallProgressText) dom.overallProgressText.textContent = `${progress}%`;
    dom.overallProgressBar.style.width = `${progress}%`;
    renderNodes();
    renderSelectedNode();
    renderArtifacts();
    // 0907: 节点详情已迁移到独立页面，主页面不再为隐藏详情建立 USRP 直播连接。
    disconnectLiveSpectrum();
    dom.liveSpectrumCard?.classList.add('hidden');
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
    const selectedTypes = task.device_selection?.selected_device_types || [];
    const hasUsrp = selectedTypes.includes('usrp');
    const prepare = task.plan_candidate?.prepare_result || {};
    const constraints = task.constraints || {};
    if (hasUsrp) {
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
      dom.saveApprovalParamsBtn.classList.remove('hidden');
    } else {
      const selection = task.device_selection || {};
      const deviceNames = (task.available_devices?.devices || [])
        .filter(item => selectedTypes.includes(item.device_type))
        .map(item => item.display_name || item.id || item.device_type);
      dom.approvalParamGrid.innerHTML = `
        <div class="approval-probe-only">
          <strong>本任务无需 USRP 频谱参数</strong>
          <p>智能体根据任务背景和当前在线设备，选择了 ${escapeHtml(selectedTypes.map(type => type === 'wifi_bluetooth_probe' ? 'WiFi/蓝牙探针' : type).join(' + ') || '探针设备')}。</p>
          <span>目标设备：${escapeHtml(deviceNames.join('、') || '按当前可用设备执行')}</span>
          <span>探针类型：${escapeHtml((selection.probe_kinds || []).join('、') || '按任务需求')}</span>
          <span>选择依据：${escapeHtml(Object.values(selection.rationale || {}).join('；') || selection.objective || '由规划模型综合任务背景与设备可用性判断')}</span>
        </div>`;
      dom.saveApprovalParamsBtn.classList.add('hidden');
    }
    const bootstrap = new Set(['template_analysis', 'device_discovery', 'device_selection', 'instruction_analysis', 'plan_generation', 'approval']);
    const steps = (task.nodes || []).filter(node => !bootstrap.has(node.id)).map(node => node.title || node.id);
    dom.approvalFlow.innerHTML = steps.map((step, index) => `
      <span class="flow-step"><b>${index + 1}</b>${escapeHtml(step)}</span>${index < steps.length - 1 ? '<span class="flow-arrow">→</span>' : ''}
    `).join('') || '<span class="muted">等待生成自适应设备执行流程</span>';
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
    const previewNodeId = state.previewNodeId;
    removeNodePreview(false);
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
    dom.planNodeList.querySelectorAll('[data-node-id]').forEach(item => item.addEventListener('click', event => {
      const node = task.nodes.find(candidate => candidate.id === item.dataset.nodeId);
      if (!node) return;
      state.selectedNodeId = node.id;
      dom.planNodeList.querySelectorAll('[data-node-id]').forEach(elm => elm.classList.toggle('selected', elm === item));
      renderSelectedNode();
      showNodePreview(node, item);
      event.stopPropagation();
    }));
    if (previewNodeId) {
      const anchor = dom.planNodeList.querySelector(`[data-node-id="${CSS.escape(previewNodeId)}"]`);
      const previewNode = task.nodes.find(candidate => candidate.id === previewNodeId);
      if (anchor && previewNode) showNodePreview(previewNode, anchor);
    }
  }
  function renderSelectedNode() {
    const task = state.currentTask;
    const node = task?.nodes?.find(item => item.id === state.selectedNodeId)
      || task?.nodes?.find(item => item.id === task.current_node_id)
      || task?.nodes?.[0];
    if (!node) {
      clearReasoning();
      return;
    }
    state.selectedNodeId = node.id;
    renderReasoning(node);
  }
  function renderReasoning(node) {
    const mode = reasoningMode(state.currentTask);
    const reasoning = String(node?.outputs?.llm_reasoning || '');
    const structured = String(node?.outputs?.llm_live_output || '');
    const summary = String(node?.outputs?.llm_status_summary || '').trim();
    const summaryUpdatedAt = String(node?.outputs?.llm_status_summary_updated_at || '');
    if (dom.reasoningSummaryTitle) dom.reasoningSummaryTitle.textContent = mode === 'fast' ? '模型输出摘要' : '动态思考摘要';
    if (dom.reasoningDetails) dom.reasoningDetails.classList.toggle('hidden', mode === 'fast');
    if (dom.reasoningDetailsTitle) dom.reasoningDetailsTitle.textContent = mode === 'fast' ? '快速模式不展示详细思考' : '查看详细思考过程';
    dom.reasoningSummary.textContent = summary || (node?.status === 'in_progress'
      ? '正在等待模型生成最新状态摘要…'
      : (node?.summary || '等待当前节点开始执行'));
    dom.reasoningSummaryMeta.textContent = summaryUpdatedAt
      ? `当前模型实时摘要 · ${fmtTime(summaryUpdatedAt)}`
      : (mode === 'fast' ? '' : '');
    dom.structuredStream.textContent = structured || '尚无输出';
    if (mode === 'fast') {
      dom.reasoningEmpty.classList.add('hidden');
      dom.reasoningStream.textContent = '快速模式不输出详细思考过程。';
      dom.reasoningStream.classList.remove('streaming');
      return;
    }
    if (!reasoning) {
      dom.reasoningEmpty.classList.remove('hidden');
      dom.reasoningStream.textContent = '尚无详细思考过程';
      dom.reasoningStream.classList.remove('streaming');
      return;
    }
    dom.reasoningEmpty.classList.add('hidden');
    dom.reasoningStream.textContent = reasoning;
    dom.reasoningStream.classList.toggle('streaming', node.status === 'in_progress');
    dom.reasoningStream.scrollTop = dom.reasoningStream.scrollHeight;
  }

  function clearReasoning() {
    const mode = reasoningMode(state.currentTask);
    if (dom.reasoningSummaryTitle) dom.reasoningSummaryTitle.textContent = mode === 'fast' ? '模型输出摘要' : '动态思考摘要';
    if (dom.reasoningDetails) dom.reasoningDetails.classList.toggle('hidden', mode === 'fast');
    dom.reasoningSummary.textContent = '等待模型生成当前任务摘要…';
    dom.reasoningSummaryMeta.textContent = mode === 'fast' ? '快速模式：非思考输出，摘要仍会实时显示' : '';
    dom.reasoningEmpty.classList.toggle('hidden', mode === 'fast');
    dom.reasoningStream.classList.remove('streaming');
    dom.reasoningStream.textContent = mode === 'fast' ? '快速模式不输出详细思考过程。' : '尚无详细思考过程';
    dom.structuredStream.textContent = '尚无输出';
  }

  function appendLiveDelta(event) {
    const task = state.currentTask;
    if (!task || !event.node_id) return;
    const node = task.nodes?.find(item => item.id === event.node_id);
    if (!node) return;
    node.outputs ||= {};
    const delta = String(event.data?.delta || '');
    if (event.type === 'llm_status_summary') {
      const summary = String(event.data?.summary || event.message || '').trim();
      if (summary) {
        node.outputs.llm_status_summary = summary;
        node.outputs.llm_status_summary_updated_at = event.created_at || new Date().toISOString();
        if (state.selectedNodeId === node.id) {
          dom.reasoningSummary.textContent = summary;
          dom.reasoningSummaryMeta.textContent = `当前模型实时摘要 · ${fmtTime(node.outputs.llm_status_summary_updated_at)}`;
        }
        if (state.previewNodeId === node.id) {
          const preview = document.querySelector('.node-preview-popover');
          const target = preview?.querySelector('[data-node-preview-summary]');
          if (target) target.textContent = summary;
        }
      }
      return;
    }
    if (event.type === 'llm_reasoning_delta' && delta) {
      node.outputs.llm_reasoning = String(node.outputs.llm_reasoning || '') + delta;
      if (state.selectedNodeId === node.id) {
        dom.reasoningEmpty.classList.add('hidden');
        dom.reasoningStream.classList.add('streaming');
        if (dom.reasoningStream.textContent === '尚无详细思考过程') dom.reasoningStream.textContent = '';
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
          <button class="artifact-detail-btn" type="button" data-artifact-detail="${escapeHtml(item.id)}">查看详细</button>
          ${item.spectrum_url ? `<button class="artifact-spectrum-btn" type="button" data-spectrum-url="${escapeHtml(item.spectrum_url)}" data-spectrum-name="${escapeHtml(item.file_name)}">频谱可视化</button>` : ''}
          ${isNpz ? `<button class="artifact-judge-btn" type="button" data-judge-artifact="${escapeHtml(item.id)}">研判</button>` : ''}
          <a href="${escapeHtml(item.download_url)}">下载</a>
        </div>
      </div>`;
    }).join('');
  }

  function closeArtifactDetailModal() {
    state.artifactDetailId = '';
    dom.artifactDetailModal?.classList.add('hidden');
  }

  async function openArtifactDetailModal(artifactId) {
    if (!state.currentTask?.id || !artifactId) return;
    state.artifactDetailId = artifactId;
    dom.artifactDetailModal?.classList.remove('hidden');
    dom.artifactDetailTitle.textContent = '任务产物详情';
    dom.artifactDetailSubtitle.textContent = '正在读取产物信息……';
    dom.artifactDetailMeta.innerHTML = '';
    dom.artifactDetailPreview.innerHTML = '<div class="probe-detail-loading">正在生成预览……</div>';
    try {
      const payload = await api(`/api/capture-agent/tasks/${encodeURIComponent(state.currentTask.id)}/artifacts/${encodeURIComponent(artifactId)}/details`);
      renderArtifactDetail(payload);
    } catch (error) {
      dom.artifactDetailPreview.innerHTML = `<div class="probe-detail-empty">${escapeHtml(error.message)}</div>`;
      showToast(error.message, true);
    }
  }

  function renderArtifactDetail(payload) {
    const artifact = payload.artifact || {};
    const preview = payload.preview || {};
    dom.artifactDetailTitle.textContent = artifact.file_name || '任务产物详情';
    dom.artifactDetailSubtitle.textContent = `${artifact.kind || '任务产物'} · ${fmtSize(artifact.size_bytes)} · ${escapeHtml(artifact.extension || '')}`;
    dom.artifactDetailDownload.href = artifact.download_url || '#';
    dom.artifactDetailMeta.innerHTML = [
      ['文件名', artifact.file_name || '-'], ['产物类型', artifact.kind || '-'], ['文件大小', fmtSize(artifact.size_bytes)],
      ['扩展名', artifact.extension || '-'], ['创建时间', fmtTime(artifact.created_at)], ['修改时间', fmtTime(artifact.modified_at)],
    ].map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong title="${escapeHtml(value)}">${escapeHtml(value)}</strong></div>`).join('');
    if (preview.type === 'npz') {
      const arrays = preview.arrays || [];
      dom.artifactDetailPreview.innerHTML = `<div class="artifact-preview-title">NPZ 数组结构</div><div class="probe-detail-table-wrap"><table class="probe-detail-table"><thead><tr><th>数组</th><th>形状</th><th>类型</th><th>元素数</th><th>最小值</th><th>均值</th><th>最大值</th></tr></thead><tbody>${arrays.map(item => `<tr><td>${escapeHtml(item.name)}</td><td>${escapeHtml((item.shape || []).join(' × '))}</td><td>${escapeHtml(item.dtype)}</td><td>${escapeHtml(item.elements)}</td><td>${escapeHtml(item.min ?? '-')}</td><td>${escapeHtml(item.mean ?? '-')}</td><td>${escapeHtml(item.max ?? '-')}</td></tr>`).join('')}</tbody></table></div>`;
    } else if (preview.type === 'json') {
      const fields = preview.top_level || [];
      const sample = preview.sample || {};
      dom.artifactDetailPreview.innerHTML = `<div class="artifact-preview-title">JSON 结构概览</div><div class="probe-detail-table-wrap"><table class="probe-detail-table"><thead><tr><th>顶层字段</th><th>数据类型</th><th>元素数量</th></tr></thead><tbody>${fields.map(item => `<tr><td>${escapeHtml(item.key)}</td><td>${escapeHtml(item.type)}</td><td>${escapeHtml(item.count ?? '-')}</td></tr>`).join('')}</tbody></table></div>${Object.keys(sample).length ? `<div class="artifact-sample-grid">${Object.entries(sample).map(([key, value]) => `<div><span>${escapeHtml(key)}</span><strong>${escapeHtml(value)}</strong></div>`).join('')}</div>` : ''}`;
    } else if (preview.type === 'table') {
      const rows = preview.rows || [];
      const head = rows[0] || [];
      dom.artifactDetailPreview.innerHTML = `<div class="artifact-preview-title">表格预览</div><div class="probe-detail-table-wrap"><table class="probe-detail-table"><thead><tr>${head.map(value => `<th>${escapeHtml(value)}</th>`).join('')}</tr></thead><tbody>${rows.slice(1).map(row => `<tr>${row.map(value => `<td>${escapeHtml(value)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    } else if (preview.type === 'text') {
      dom.artifactDetailPreview.innerHTML = `<div class="artifact-preview-title">文本预览</div><pre class="artifact-text-preview">${escapeHtml(preview.text || '')}</pre>`;
    } else {
      dom.artifactDetailPreview.innerHTML = `<div class="probe-detail-empty">${escapeHtml(preview.message || '该文件暂无可视化预览，可下载后查看。')}</div>`;
    }
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
    if (dom.pauseTaskBtn) dom.pauseTaskBtn.disabled = !['planning', 'running'].includes(status);
    if (dom.resumeTaskBtn) dom.resumeTaskBtn.disabled = status !== 'paused';
    dom.cancelTaskBtn.disabled = !status || ['completed', 'failed', 'cancelled'].includes(status);
    if (dom.evidenceChainBtn) dom.evidenceChainBtn.disabled = !state.currentTask;
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
    removeNodePreview(true);
    const payload = await api(`/api/capture-agent/tasks/${encodeURIComponent(taskId)}`);
    state.currentTask = payload.item;
    state.probeAutoOpenedKey = '';
    state.selectedNodeId = state.currentTask.current_node_id || state.currentTask.nodes?.[0]?.id || null;
    state.eventSeq = Number(state.currentTask.events?.at(-1)?.seq || 0);
    renderTask();
    openEventStream();
    refreshDeviceDashboard(false).catch(() => {});
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
        payload = await api('/api/capture-agent/tasks', { method: 'POST', body: { instruction, template_ids: selectedTemplateIds(), reasoning_mode: state.reasoningModePreference } });
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
    if (!task || !taskStreamActive(task)) {
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
        scheduleTaskRefresh();
      } catch (_) { /* ignore malformed event */ }
    });
    source.addEventListener('heartbeat', () => setConnection('实时连接', true));
    source.addEventListener('terminal', () => { scheduleTaskRefresh(true); });
    source.onerror = () => {
      setConnection('正在重连');
      if (state.currentTask && !taskStreamActive(state.currentTask)) closeEventStream();
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
        const previousStatus = state.currentTask.status;
        const payload = await api(`/api/capture-agent/tasks/${encodeURIComponent(state.currentTask.id)}`);
        const previousCurrent = state.currentTask.current_node_id;
        state.currentTask = payload.item;
        if (!state.selectedNodeId || state.selectedNodeId === previousCurrent) state.selectedNodeId = state.currentTask.current_node_id;
        state.eventSeq = Math.max(state.eventSeq, Number(state.currentTask.events?.at(-1)?.seq || 0));
        renderTask();
        if (!taskStreamActive(state.currentTask)) {
          closeEventStream();
          await refreshTaskList();
          if (state.currentTask.status === 'completed' && previousStatus !== 'completed') {
            await refreshDeviceDashboard(false).catch(() => {});
          }
        } else if (!state.eventSource) {
          openEventStream();
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
    if (pending.task_id) {
      localStorage.removeItem('deepem.captureAgent.pendingFromDetect');
      await refreshTaskList();
      await selectTask(String(pending.task_id));
      showToast('已进入智能检测创建的真实任务');
      return true;
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
      // 页面打开先读取已有设备状态；只有点击“查询可用设备”时才强制重新探测。
      refreshDeviceDashboard(false).catch(error => { dom.deviceQueryStatus.textContent = `设备状态读取失败：${error.message}`; });
      const consumedPending = await consumePendingDetectTask();
      if (!consumedPending) {
        const requestedTaskId = new URLSearchParams(window.location.search).get('task');
        if (requestedTaskId) {
          try { await selectTask(requestedTaskId); }
          catch (_) { if (state.tasks.length) await selectTask(state.tasks[0].id); else renderEmptyTask(); }
        } else if (state.tasks.length) await selectTask(state.tasks[0].id); else renderEmptyTask();
      }
    } catch (error) { showToast(error.message, true); }
  }

  dom.taskPicker.addEventListener('change', () => selectTask(dom.taskPicker.value).catch(error => showToast(error.message, true)));
  dom.modelModeBadge?.addEventListener('click', toggleReasoningMode);
  dom.queryDevicesBtn?.addEventListener('click', () => refreshDeviceDashboard(true).catch(error => showToast(error.message, true)));
  dom.evidenceChainBtn?.addEventListener('click', () => openEvidenceChain().catch(error => showToast(error.message, true)));
  dom.closeDeviceHistoryBtn?.addEventListener('click', closeDeviceHistory);
  dom.deviceHistoryModal?.addEventListener('click', event => { if (event.target === dom.deviceHistoryModal) closeDeviceHistory(); });
  dom.closeEvidenceChainBtn?.addEventListener('click', closeEvidenceChain);
  dom.evidenceChainModal?.addEventListener('click', event => { if (event.target === dom.evidenceChainModal) closeEvidenceChain(); });
  dom.newTaskBtn.addEventListener('click', newTaskMode);
  dom.renameTaskBtn.addEventListener('click', () => renameCurrentTask().catch(error => showToast(error.message, true)));
  dom.deleteTaskBtn.addEventListener('click', () => deleteCurrentTask().catch(error => showToast(error.message, true)));
  dom.probeAssistBtn?.addEventListener('click', openProbeAssistModal);
  dom.closeProbeAssistBtn?.addEventListener('click', closeProbeAssistModal);
  dom.probeAssistModal?.addEventListener('click', event => { if (event.target === dom.probeAssistModal) closeProbeAssistModal(); });
  dom.confirmProbeAssistBtn?.addEventListener('click', () => decideProbeAssistance(true));
  dom.declineProbeAssistBtn?.addEventListener('click', () => decideProbeAssistance(false));
  dom.openProbeDetailsBtn?.addEventListener('click', () => openProbeDetailModal());
  dom.closeProbeDetailBtn?.addEventListener('click', closeProbeDetailModal);
  dom.probeDetailModal?.addEventListener('click', event => { if (event.target === dom.probeDetailModal) closeProbeDetailModal(); });
  dom.probeDetailTabs?.addEventListener('click', event => {
    const button = event.target.closest('[data-probe-detail-tab]');
    if (!button) return;
    state.probeDetailTab = button.dataset.probeDetailTab;
    state.probeDetailPage = 1;
    renderProbeDetailModal();
  });
  dom.probeDetailSearch?.addEventListener('input', () => { state.probeDetailPage = 1; renderProbeDetailModal(); });
  dom.probeDetailPrev?.addEventListener('click', () => { state.probeDetailPage -= 1; renderProbeDetailModal(); });
  dom.probeDetailNext?.addEventListener('click', () => { state.probeDetailPage += 1; renderProbeDetailModal(); });
  dom.baselineManagerBtn.addEventListener('click', () => openBaselineModal().catch(error => showToast(error.message, true)));
  dom.closeBaselineModalBtn.addEventListener('click', closeBaselineModal);
  dom.baselineModal.addEventListener('click', event => { if (event.target === dom.baselineModal) closeBaselineModal(); });
  dom.refreshBaselineListBtn.addEventListener('click', () => loadBaselines().catch(error => showToast(error.message, true)));
  dom.saveBaselineBtn.addEventListener('click', () => saveSelectedBaseline().catch(error => showToast(error.message, true)));
  dom.approveTaskBtn.addEventListener('click', () => renderApprovalModal());
  dom.pauseTaskBtn?.addEventListener('click', () => performAction('pause'));
  dom.resumeTaskBtn?.addEventListener('click', () => performAction('resume'));
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
    const detailButton = event.target.closest('[data-artifact-detail]');
    if (detailButton) {
      openArtifactDetailModal(detailButton.dataset.artifactDetail);
      return;
    }
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
  dom.closeArtifactDetailBtn?.addEventListener('click', closeArtifactDetailModal);
  dom.artifactDetailModal?.addEventListener('click', event => { if (event.target === dom.artifactDetailModal) closeArtifactDetailModal(); });
  dom.closeSpectrumModalBtn.addEventListener('click', closeSpectrumModal);
  dom.spectrumModal.addEventListener('click', event => { if (event.target === dom.spectrumModal) closeSpectrumModal(); });
  window.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    if (dom.evidenceChainModal && !dom.evidenceChainModal.classList.contains('hidden')) closeEvidenceChain();
    else if (dom.deviceHistoryModal && !dom.deviceHistoryModal.classList.contains('hidden')) closeDeviceHistory();
    else if (!dom.probeDetailModal.classList.contains('hidden')) closeProbeDetailModal();
    else if (!dom.artifactDetailModal.classList.contains('hidden')) closeArtifactDetailModal();
    else if (!dom.probeAssistModal.classList.contains('hidden')) closeProbeAssistModal();
    else if (!dom.spectrumModal.classList.contains('hidden')) closeSpectrumModal();
    else if (!dom.baselineModal.classList.contains('hidden')) closeBaselineModal();
    else if (!dom.docxPreviewModal.classList.contains('hidden')) closeDocxPreviewModal();
    else if (!dom.templateModal.classList.contains('hidden')) closeTemplateModal();
  });
  window.addEventListener('resize', () => { if (state.lastFftMessage) renderLiveFftMessage(state.lastFftMessage, false); });
  dom.copyCodeBtn.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(dom.generatedCode.textContent || ''); showToast('代码已复制'); }
    catch (_) { showToast('复制失败', true); }
  });
  window.addEventListener('beforeunload', () => { closeEventStream(); disconnectLiveSpectrum(); disconnectDeviceSockets(); stopProbePolling(); closeSpectrumModal(); });
  loadInitial();
})();
