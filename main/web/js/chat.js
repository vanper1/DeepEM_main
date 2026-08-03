(function () {
  const app = window.DeepEMApp;
  const STORAGE_KEY = 'deepem.chat.currentSessionId';
  const CHAT_MODE_KEY = 'deepem.chat.mode';
  const MODEL_SETTINGS_KEY = 'deepem.chat.modelSettings.v2';
  const DEFAULT_MODEL_OPTIONS = Object.freeze({
    temperature: 1.0,
    top_p: 0.95,
    top_k: 20,
    min_p: 0.0,
    presence_penalty: 1.5,
    repetition_penalty: 1.0,
    enable_thinking: true,
    preserve_thinking: false,
    reasoning_effort: 'medium',
    thinking_token_budget: 16384,
  });

  const timeline = () => document.getElementById('chatTimeline');
  const input = () => document.getElementById('chatComposerInput');
  const sendBtn = () => document.getElementById('chatComposerSend');
  const sessionList = () => document.getElementById('chatSessionList');
  const newSessionBtn = () => document.getElementById('chatNewSessionBtn');
  const chatModeSelector = () => document.getElementById('chatModeSelector');
  const backToLatestBtn = () => document.getElementById('chatBackToLatest');
  const chatUploadButton = () => document.getElementById('chatUploadButton');
  const chatUploadInput = () => document.getElementById('chatUploadInput');
  const chatAttachmentLane = () => document.getElementById('chatAttachmentLane');
  const chatAttachmentHint = () => document.getElementById('chatAttachmentHint');
  const chatUploadProgress = () => document.getElementById('chatUploadProgress');
  const chatUploadProgressFill = () => document.getElementById('chatUploadProgressFill');
  const chatUploadProgressText = () => document.getElementById('chatUploadProgressText');
  const nl2sqlButton = () => document.getElementById('nl2sqlConfigButton');
  const nl2sqlPopover = () => document.getElementById('nl2sqlPopover');
  const nl2sqlPopoverClose = () => document.getElementById('nl2sqlPopoverClose');
  const nl2sqlForceEnabled = () => document.getElementById('nl2sqlForceEnabled');
  const nl2sqlAutoSelectTables = () => document.getElementById('nl2sqlAutoSelectTables');
  const nl2sqlManualSection = () => document.getElementById('nl2sqlManualSection');
  const nl2sqlManualList = () => document.getElementById('nl2sqlManualList');
  const nl2sqlConfigHint = () => document.getElementById('nl2sqlConfigHint');
  const nl2sqlDbStatus = () => document.getElementById('nl2sqlDbStatus');
  const nl2sqlDbUploadButton = () => document.getElementById('nl2sqlDbUploadButton');
  const nl2sqlDbResetButton = () => document.getElementById('nl2sqlDbResetButton');
  const nl2sqlDbFileInput = () => document.getElementById('nl2sqlDbFileInput');
  const nl2sqlDbSelect = () => document.getElementById('nl2sqlDbSelect');
  const nl2sqlDbDropdown = () => document.getElementById('nl2sqlDbDropdown');
  const nl2sqlDbDropdownTrigger = () => document.getElementById('nl2sqlDbDropdownTrigger');
  const nl2sqlDbDropdownLabel = () => document.getElementById('nl2sqlDbDropdownLabel');
  const nl2sqlDbDropdownMenu = () => document.getElementById('nl2sqlDbDropdownMenu');
  const nl2sqlDbDropdownList = () => document.getElementById('nl2sqlDbDropdownList');
  const nl2sqlDbHoverPanel = () => document.getElementById('nl2sqlDbHoverPanel');
  const placeBaselineStatus = () => document.getElementById('placeBaselineStatus');
  const placeBaselineInput = () => document.getElementById('placeBaselineInput');
  const placeBaselineHint = () => document.getElementById('placeBaselineHint');
  const placeBaselineInitButton = () => document.getElementById('placeBaselineInitButton');
  const placeBaselineSaveButton = () => document.getElementById('placeBaselineSaveButton');
  const modelSettingsToggle = () => document.getElementById('modelSettingsToggle');
  const modelSettingsClose = () => document.getElementById('modelSettingsClose');
  const modelSettingsBackdrop = () => document.getElementById('modelSettingsBackdrop');
  const modelSettingsDrawer = () => document.getElementById('modelSettingsDrawer');
  const modelSettingsReset = () => document.getElementById('modelSettingsReset');
  const llmTemperature = () => document.getElementById('llmTemperature');
  const llmTemperatureValue = () => document.getElementById('llmTemperatureValue');
  const llmTopP = () => document.getElementById('llmTopP');
  const llmTopPValue = () => document.getElementById('llmTopPValue');
  const llmTopK = () => document.getElementById('llmTopK');
  const llmMinP = () => document.getElementById('llmMinP');
  const llmMinPValue = () => document.getElementById('llmMinPValue');
  const llmPresencePenalty = () => document.getElementById('llmPresencePenalty');
  const llmPresencePenaltyValue = () => document.getElementById('llmPresencePenaltyValue');
  const llmRepetitionPenalty = () => document.getElementById('llmRepetitionPenalty');
  const llmRepetitionPenaltyValue = () => document.getElementById('llmRepetitionPenaltyValue');
  const llmEnableThinking = () => document.getElementById('llmEnableThinking');
  const llmPreserveThinking = () => document.getElementById('llmPreserveThinking');
  const llmReasoningEffort = () => document.getElementById('llmReasoningEffort');
  const llmThinkingTokenBudget = () => document.getElementById('llmThinkingTokenBudget');

  let currentRun = null;
  const state = {
    thinkingBubble: null,
    provisionalFinalBubble: null,
    autoFollow: true,
    sessions: [],
    currentSessionId: null,
    isGenerating: false,
    chatMode: 'general',
    openSessionMenuId: null,
    databases: [],
    dbDropdownOpen: false,
    hoverDatabaseId: '',
    renamingDatabaseId: '',
    modelSettingsOpen: false,
    placeBaseline: {
      loaded: false,
      loading: false,
      initialized: false,
      placeId: '',
      fingerprints: [],
      candidateFingerprints: [],
      source: '',
      updatedAt: '',
    },
    draftAttachmentsBySession: {},
    nl2sql: {
      forceEnabled: false,
      autoSelectTables: true,
      manualSelectedTables: [],
      allTables: [],
      loaded: false,
      loading: false,
      databaseId: '',
      dbPath: '',
      sourceLabel: '',
    },
    modelOptions: { ...DEFAULT_MODEL_OPTIONS },
  };

  function currentSession() {
    return state.sessions.find(item => item.id === state.currentSessionId) || null;
  }

  function currentDraftAttachments() {
    const sessionId = state.currentSessionId || '__global__';
    if (!Array.isArray(state.draftAttachmentsBySession[sessionId])) {
      state.draftAttachmentsBySession[sessionId] = [];
    }
    return state.draftAttachmentsBySession[sessionId];
  }

  function replaceDraftAttachments(items, sessionId = state.currentSessionId || '__global__') {
    state.draftAttachmentsBySession[sessionId] = Array.isArray(items) ? [...items] : [];
    if (sessionId === (state.currentSessionId || '__global__')) {
      renderDraftAttachments();
    }
  }

  function appendDraftAttachments(items, sessionId = state.currentSessionId || '__global__') {
    const existing = Array.isArray(state.draftAttachmentsBySession[sessionId])
      ? [...state.draftAttachmentsBySession[sessionId]]
      : [];
    const seen = new Set(existing.map(item => String(item.asset_id || '')));
    (items || []).forEach(item => {
      const assetId = String(item.asset_id || '');
      if (!assetId || seen.has(assetId)) return;
      seen.add(assetId);
      existing.push(item);
    });
    replaceDraftAttachments(existing, sessionId);
  }

  function removeDraftAttachment(assetId, sessionId = state.currentSessionId || '__global__') {
    const filtered = currentDraftAttachments().filter(item => String(item.asset_id || '') !== String(assetId || ''));
    replaceDraftAttachments(filtered, sessionId);
  }

  function clearDraftAttachments(sessionId = state.currentSessionId || '__global__') {
    replaceDraftAttachments([], sessionId);
  }

  function sortSessions() {
    state.sessions.sort((a, b) => {
      const av = String(a.updated_at || '');
      const bv = String(b.updated_at || '');
      if (av === bv) return String(a.id || '').localeCompare(String(b.id || ''));
      return bv.localeCompare(av);
    });
  }

  function isNearBottom() {
    const el = timeline();
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight <= 48;
  }

  function updateBackToLatestButton() {
    const hidden = state.autoFollow || isNearBottom();
    backToLatestBtn().classList.toggle('hidden', hidden);
  }

  function scrollToLatest(force = false) {
    if (!force && !state.autoFollow) {
      updateBackToLatestButton();
      return;
    }
    window.requestAnimationFrame(() => {
      const el = timeline();
      el.scrollTop = el.scrollHeight;
      updateBackToLatestButton();
    });
  }

  function jumpToLatest() {
    state.autoFollow = true;
    scrollToLatest(true);
    input().focus();
  }

  function enhanceRenderedContent(root) {
    if (!root) return;
    root.querySelectorAll('a').forEach(node => {
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer');
    });
    root.querySelectorAll('img').forEach(node => {
      node.setAttribute('loading', 'lazy');
    });
  }

  function renderMarkdownInto(container, markdown) {
    container.innerHTML = app.renderMarkdown(markdown || '');
    enhanceRenderedContent(container);
  }

  function scheduleMarkdownRender(container) {
    if (!container || container.dataset.markdownScheduled === '1') return;
    container.dataset.markdownScheduled = '1';
    window.requestAnimationFrame(() => {
      container.dataset.markdownScheduled = '0';
      renderMarkdownInto(container, container.dataset.rawMarkdown || '');
      if (state.autoFollow) scrollToLatest();
    });
  }

  function setMarkdownBuffer(container, value) {
    if (!container) return;
    container.dataset.rawMarkdown = String(value || '');
    scheduleMarkdownRender(container);
  }

  function appendMarkdownDelta(container, delta) {
    if (!container) return;
    container.dataset.rawMarkdown = `${container.dataset.rawMarkdown || ''}${delta || ''}`;
    scheduleMarkdownRender(container);
  }

  function flushMarkdownRender(container) {
    if (!container) return;
    container.dataset.markdownScheduled = '0';
    renderMarkdownInto(container, container.dataset.rawMarkdown || '');
  }

  function normalizeAttachment(item) {
    const metadata = item && item.metadata && typeof item.metadata === 'object' ? item.metadata : {};
    const assetId = String(item.asset_id || metadata.asset_id || '');
    const uploadKind = String(item.upload_kind || metadata.upload_kind || item.upload_kind || '');
    const mimeType = String(item.mime_type || metadata.mime_type || '');
    const previewAssetId = String(metadata.preview_asset_id || item.preview_asset_id || '');
    const previewUrl = item.preview_url || (previewAssetId ? app.assetUrl(previewAssetId) : '');
    const assetUrl = item.url || item.uri || (assetId ? app.assetUrl(assetId) : '');
    return {
      asset_id: assetId,
      file_name: item.file_name || item.label || '未命名文件',
      url: assetUrl,
      mime_type: mimeType,
      upload_kind: uploadKind,
      summary: item.summary || '',
      preview_url: previewUrl,
      database_id: item.database_id || metadata.database_id || '',
      metadata,
    };
  }

  function attachmentTypeLabel(item) {
    if (item.upload_kind === 'image') return '图片';
    if (item.upload_kind === 'document') return '文档';
    if (item.upload_kind === 'signal_bin') return 'BIN';
    if (item.upload_kind === 'signal_preview') return '预览';
    if (item.database_id) return '数据库';
    return '附件';
  }

  function renderAttachmentCard(item, options = {}) {
    const normalized = normalizeAttachment(item);
    const card = document.createElement('div');
    card.className = `chat-attachment-card${options.compact ? ' compact' : ''}`;

    const previewUrl = normalized.preview_url || (normalized.mime_type.startsWith('image/') ? normalized.url : '');
    const imageUrl = normalized.mime_type.startsWith('image/') ? normalized.url : previewUrl;
    const removeButton = options.removable
      ? `<button class="chat-attachment-remove" type="button" data-remove-attachment="${app.escapeHtml(normalized.asset_id)}">×</button>`
      : '';
    const summary = normalized.summary ? `<div class="chat-attachment-summary">${app.escapeHtml(normalized.summary)}</div>` : '';
    const databaseInfo = normalized.database_id ? `<div class="chat-attachment-meta">database_id: ${app.escapeHtml(normalized.database_id)}</div>` : '';

    card.innerHTML = `
      ${removeButton}
      <div class="chat-attachment-head">
        <div class="chat-attachment-title">${app.escapeHtml(normalized.file_name)}</div>
        <div class="chat-attachment-type">${app.escapeHtml(attachmentTypeLabel(normalized))}</div>
      </div>
      <div class="chat-attachment-meta">${app.escapeHtml(normalized.mime_type || normalized.upload_kind || 'application/octet-stream')}</div>
      ${summary}
      ${databaseInfo}
      <a class="chat-attachment-link" href="${app.escapeHtml(normalized.url || '#')}" target="_blank" rel="noopener noreferrer">查看文件</a>
    `;

    if (imageUrl) {
      const preview = document.createElement('div');
      preview.className = 'chat-attachment-preview';
      preview.innerHTML = `<img src="${app.escapeHtml(imageUrl)}" alt="${app.escapeHtml(normalized.file_name)}">`;
      card.insertBefore(preview, card.querySelector('.chat-attachment-link'));
    }

    return card;
  }

  function renderAttachmentList(items, options = {}) {
    const attachments = (items || []).map(normalizeAttachment).filter(item => item.asset_id || item.url);
    if (!attachments.length) return null;
    const wrap = document.createElement('div');
    wrap.className = `chat-attachment-list${options.compact ? ' compact' : ''}`;
    attachments.forEach(item => wrap.appendChild(renderAttachmentCard(item, options)));
    return wrap;
  }

  function renderDraftAttachments() {
    const host = chatAttachmentLane();
    const attachments = currentDraftAttachments();
    host.innerHTML = '';
    if (!attachments.length) {
      host.classList.add('hidden');
      updateAttachmentHint();
      return;
    }
    host.classList.remove('hidden');
    const list = renderAttachmentList(attachments, { removable: true, compact: true });
    if (list) host.appendChild(list);
    updateAttachmentHint();
  }

  function updateAttachmentHint(message) {
    const attachments = currentDraftAttachments();
    if (message) {
      chatAttachmentHint().textContent = message;
      return;
    }
    if (!attachments.length) {
      chatAttachmentHint().textContent = '支持图片、PDF、Markdown、TXT、DOCX、BIN。';
      return;
    }
    const labels = attachments.map(item => item.file_name).slice(0, 3);
    const suffix = attachments.length > 3 ? ` 等 ${attachments.length} 个文件` : '';
    chatAttachmentHint().textContent = `待发送附件：${labels.join('，')}${suffix}`;
  }

  function updateUploadProgress(percent, message) {
    const wrapper = chatUploadProgress();
    const fill = chatUploadProgressFill();
    const text = chatUploadProgressText();
    if (!wrapper || !fill || !text) return;
    const value = Math.max(0, Math.min(100, Number(percent) || 0));
    wrapper.classList.remove('hidden');
    fill.style.width = `${value}%`;
    text.textContent = `${Math.round(value)}%`;
    if (message) updateAttachmentHint(message);
  }

  function hideUploadProgress() {
    const wrapper = chatUploadProgress();
    const fill = chatUploadProgressFill();
    const text = chatUploadProgressText();
    if (!wrapper || !fill || !text) return;
    wrapper.classList.add('hidden');
    fill.style.width = '0%';
    text.textContent = '0%';
  }

  function createBubble(role, content, options = {}) {
    const parent = options.parent || timeline();
    const attachments = Array.isArray(options.attachments) ? options.attachments : [];
    const wrapper = document.createElement('div');
    wrapper.className = `chat-bubble-row ${role}${options.provisional ? ' provisional' : ''}`;

    const stack = document.createElement('div');
    stack.className = 'chat-bubble-stack';

    const bubble = document.createElement('div');
    bubble.className = `chat-bubble ${role}${options.streaming ? ' streaming-answer' : ''}`;

    const body = document.createElement('div');
    body.className = `chat-message-body ${role === 'assistant' ? 'markdown-body' : 'plain-text'}`;
    bubble.appendChild(body);
    bubble._body = body;

    if (options.streaming) {
      const caret = document.createElement('span');
      caret.className = 'chat-stream-caret';
      caret.setAttribute('aria-hidden', 'true');
      bubble.appendChild(caret);
    }

    stack.appendChild(bubble);

    const displayContent = content || (role === 'operator' && attachments.length ? '已上传附件。' : '');
    if (role === 'assistant') {
      setMarkdownBuffer(body, displayContent);
      if (!options.streaming) flushMarkdownRender(body);
    } else {
      body.textContent = displayContent;
    }

    const attachmentList = renderAttachmentList(attachments, { compact: role === 'operator' });
    if (attachmentList) stack.appendChild(attachmentList);

    wrapper.appendChild(stack);
    parent.appendChild(wrapper);
    scrollToLatest();
    return bubble;
  }

  function createRunChain() {
    const section = document.createElement('section');
    section.className = 'chat-run-chain';
    section.innerHTML = '<div class="chat-run-chain-title">本轮智能体过程</div><div class="chat-run-steps"></div>';
    return {
      root: section,
      steps: section.querySelector('.chat-run-steps'),
      finalBubble: null,
      activeReasoningBlock: null,
      mounted: false,
    };
  }

  function mountRunChain() {
    if (!currentRun || currentRun.mounted) return;
    if (!currentRun.root.isConnected) {
      timeline().appendChild(currentRun.root);
    }
    currentRun.mounted = true;
    scrollToLatest();
  }

  function lockScrollToBottom(el) {
    if (!el) return;
    window.requestAnimationFrame(() => {
      el.scrollTop = el.scrollHeight;
    });
  }

  function isSyntheticNL2SQLReasoningDelta(text) {
    const normalized = String(text || '').trim();
    return /^\[(表选择|SQL 生成|SQL 修复)\]$/.test(normalized);
  }

  function ensureReasoningBlock() {
    if (!currentRun) currentRun = createRunChain();
    mountRunChain();
    if (currentRun.activeReasoningBlock) return currentRun.activeReasoningBlock;
    const step = document.createElement('div');
    step.className = 'chat-step reasoning streaming';
    step.innerHTML = '<div class="chat-step-label">思考过程</div><div class="chat-step-scroll"><div class="chat-step-content"></div></div>';
    currentRun.steps.appendChild(step);
    currentRun.activeReasoningBlock = step;
    scrollToLatest();
    return step;
  }

  function beginReasoningBlock(placeholder = '模型已开始思考，正在接收流式输出...') {
    removeThinkingBubble();
    const block = ensureReasoningBlock();
    const content = block.querySelector('.chat-step-content');
    if (content && !content.textContent) {
      content.textContent = placeholder;
      content.dataset.placeholder = '1';
    }
    return block;
  }

  function appendReasoningBlockText(text, options = {}) {
    const block = ensureReasoningBlock();
    if (options.streaming === false) {
      block.classList.remove('streaming');
    }
    const content = block.querySelector('.chat-step-content');
    if (!content) return block;
    if (content.dataset.placeholder === '1') {
      content.textContent = '';
      content.dataset.placeholder = '0';
    }
    content.textContent += text || '';
    content.dataset.hasContent = '1';
    const scrollWrap = block.querySelector('.chat-step-scroll');
    if (state.autoFollow) {
      lockScrollToBottom(scrollWrap);
      if (typeof block.scrollIntoView === 'function') {
        block.scrollIntoView({ block: 'nearest' });
      }
      scrollToLatest();
    }
    return block;
  }

  function appendReasoningDelta(delta) {
    const normalized = delta || '';
    if (!normalized || isSyntheticNL2SQLReasoningDelta(normalized)) return;
    removeThinkingBubble();
    appendReasoningBlockText(normalized);
  }

  function finalizeReasoningBlock() {
    if (!currentRun || !currentRun.activeReasoningBlock) return;
    currentRun.activeReasoningBlock.classList.remove('streaming');
    currentRun.activeReasoningBlock = null;
  }

  function appendStep(kind, text) {
    if (!currentRun) currentRun = createRunChain();
    mountRunChain();
    finalizeReasoningBlock();
    const step = document.createElement('div');
    step.className = `chat-step ${kind}`;
    step.textContent = text;
    currentRun.steps.appendChild(step);
    scrollToLatest();
    return step;
  }

  function hasImageAttachment(items) {
    return (items || []).some(item => {
      const normalized = normalizeAttachment(item);
      return normalized.upload_kind === 'image' || String(normalized.mime_type || '').startsWith('image/');
    });
  }

  function renderTable(columns, rows) {
    const wrap = document.createElement('div');
    wrap.className = 'chat-table-wrap';
    const head = columns.map(col => `<th>${app.escapeHtml(String(col))}</th>`).join('');
    const body = rows.slice(0, 50).map(row => `
      <tr>${row.map(cell => `<td>${app.escapeHtml(cell == null ? '' : String(cell))}</td>`).join('')}</tr>
    `).join('');
    wrap.innerHTML = `
      <table>
        <thead><tr>${head}</tr></thead>
        <tbody>${body || '<tr><td colspan="99">暂无结果</td></tr>'}</tbody>
      </table>
    `;
    return wrap;
  }

  function buildChartConfig(chart, columns, rows) {
    if (!rows.length || columns.length < 2) return null;
    if (chart.type === 'pie') {
      const labelKey = chart.label || columns[0];
      const valueKey = chart.value || columns[1];
      const labelIndex = columns.indexOf(labelKey);
      const valueIndex = columns.indexOf(valueKey);
      if (labelIndex < 0 || valueIndex < 0) return null;
      return {
        type: 'pie',
        data: {
          labels: rows.map(row => String(row[labelIndex] ?? '')),
          datasets: [{ data: rows.map(row => Number(row[valueIndex] || 0)) }],
        },
        options: { responsive: true, plugins: { legend: { position: 'bottom' } } },
      };
    }
    const xKey = chart.x || columns[0];
    const yKey = chart.y || columns[1];
    const xIndex = columns.indexOf(xKey);
    const yIndex = columns.indexOf(yKey);
    if (xIndex < 0 || yIndex < 0) return null;
    return {
      type: chart.type,
      data: {
        labels: rows.map(row => String(row[xIndex] ?? '')),
        datasets: [{ label: yKey, data: rows.map(row => Number(row[yIndex] || 0)) }],
      },
      options: { responsive: true, plugins: { legend: { display: chart.type !== 'bar' } } },
    };
  }

  function renderChart(chart, columns, rows) {
    const wrap = document.createElement('div');
    wrap.className = 'chat-chart-wrap';
    const title = document.createElement('div');
    title.className = 'chat-tool-meta';
    title.textContent = `图表建议：${chart.title || chart.type}`;
    wrap.appendChild(title);

    if (!window.Chart) {
      const fallback = document.createElement('div');
      fallback.className = 'chat-tool-meta';
      fallback.textContent = 'Chart.js 未加载，已跳过图表渲染。';
      wrap.appendChild(fallback);
      return wrap;
    }

    const canvas = document.createElement('canvas');
    wrap.appendChild(canvas);

    const chartConfig = buildChartConfig(chart, columns, rows);
    if (!chartConfig) {
      const fallback = document.createElement('div');
      fallback.className = 'chat-tool-meta';
      fallback.textContent = '当前结果不适合图表展示。';
      wrap.appendChild(fallback);
      return wrap;
    }
    new window.Chart(canvas.getContext('2d'), chartConfig);
    return wrap;
  }

  function appendDatabaseToolCard(data) {
    if (!currentRun) currentRun = createRunChain();
    mountRunChain();
    const card = document.createElement('div');
    card.className = 'chat-tool-card';
    const columns = Array.isArray(data.columns) ? data.columns : [];
    const rows = Array.isArray(data.rows) ? data.rows : [];
    const chart = data.chart || { type: 'table' };
    const sql = data.sql || data.executed_sql || '';
    const nl2sqlConfig = data.nl2sql_config || {};
    const selectedTables = Array.isArray(nl2sqlConfig.selected_tables) ? nl2sqlConfig.selected_tables : [];
    const databaseName = data.database_name || nl2sqlConfig.database_name || data.database_file_name || data.db_path || '当前数据库';
    const databaseFile = data.database_file_name || '';
    const databaseMeta = databaseFile && databaseFile !== databaseName ? `SQLite 文件：${databaseFile}` : '';

    card.innerHTML = `
      <div class="chat-tool-card-title">数据库查询结果</div>
      <div class="chat-tool-meta">数据库：${app.escapeHtml(databaseName)}</div>
      ${databaseMeta ? `<div class="chat-tool-meta">${app.escapeHtml(databaseMeta)}</div>` : ''}
      <div class="chat-tool-meta">选表模式：${app.escapeHtml(nl2sqlConfig.selection_mode || '-')} ｜ 使用表：${app.escapeHtml(selectedTables.join(', ') || '全部表')}</div>
      <div class="chat-inline-summary">${app.escapeHtml(data.summary || '')}</div>
      <details open>
        <summary>最终 SQL</summary>
        <pre class="chat-sql-block">${app.escapeHtml(sql)}</pre>
      </details>
      <div class="chat-tool-meta">返回 ${app.escapeHtml(String(data.row_count ?? rows.length))} 行${data.truncated ? '（已截断）' : ''}</div>
    `;

    card.appendChild(renderTable(columns, rows));
    if (chart.type && chart.type !== 'table') {
      card.appendChild(renderChart(chart, columns, rows));
    }
    currentRun.steps.appendChild(card);
    scrollToLatest();
  }

  function appendDocumentToolCard(data) {
    if (!currentRun) currentRun = createRunChain();
    mountRunChain();
    const card = document.createElement('div');
    card.className = 'chat-tool-card';

    const file = data.file || {};
    const results = Array.isArray(data.results) ? data.results : [];
    card.innerHTML = `
      <div class="chat-tool-card-title">文档检索结果</div>
      <div class="chat-tool-meta">${app.escapeHtml(file.file_name || data.file_id || '共享文档库')}</div>
      <div class="chat-inline-summary">${app.escapeHtml(data.summary || '')}</div>
    `;

    if (file.preview_url || file.preview_markdown) {
      const preview = document.createElement('div');
      preview.className = 'chat-doc-preview';
      renderMarkdownInto(preview, file.preview_markdown || `![preview](${file.preview_url})`);
      card.appendChild(preview);
    }

    if (file.summary) {
      const summary = document.createElement('div');
      summary.className = 'chat-doc-snippet';
      summary.textContent = file.summary;
      card.appendChild(summary);
    }

    if (!results.length) {
      const empty = document.createElement('div');
      empty.className = 'chat-tool-meta';
      empty.textContent = '没有返回可展示的内容片段。';
      card.appendChild(empty);
    } else {
      results.forEach((item, index) => {
        const snippet = document.createElement('div');
        snippet.className = 'chat-doc-snippet';
        const locationParts = [];
        if (item.page) locationParts.push(`page ${item.page}`);
        if (item.sheet) locationParts.push(`sheet ${item.sheet}`);
        if (item.chunk_index != null) locationParts.push(`chunk ${item.chunk_index}`);
        snippet.innerHTML = `
          <div class="chat-doc-snippet-meta">
            <span>片段 ${index + 1}</span>
            <span>${app.escapeHtml(locationParts.join(' ｜ ') || '文件摘要')}</span>
            <span>score ${app.escapeHtml(String(item.score ?? '-'))}</span>
          </div>
          <div class="chat-doc-snippet-text">${app.escapeHtml(item.chunk_text || item.summary || '')}</div>
        `;
        if (item.preview_url || item.preview_markdown) {
          const preview = document.createElement('div');
          preview.className = 'chat-doc-preview inline';
          renderMarkdownInto(preview, item.preview_markdown || `![preview](${item.preview_url})`);
          snippet.appendChild(preview);
        }
        card.appendChild(snippet);
      });
    }

    currentRun.steps.appendChild(card);
    scrollToLatest();
  }

  function appendGenericToolCard(data, title) {
    if (!currentRun) currentRun = createRunChain();
    mountRunChain();
    const card = document.createElement('div');
    card.className = 'chat-tool-card';
    card.innerHTML = `
      <div class="chat-tool-card-title">${app.escapeHtml(title || '工具返回')}</div>
      <pre class="chat-sql-block">${app.escapeHtml(JSON.stringify(data || {}, null, 2))}</pre>
    `;
    currentRun.steps.appendChild(card);
    scrollToLatest();
  }



  function appendAutonomousUsrpCard(data, title) {
    if (!currentRun) currentRun = createRunChain();
    mountRunChain();
    const card = document.createElement('div');
    card.className = 'chat-tool-card autonomous-usrp-card';
    const plan = data.task_plan || (data.data && data.data.task_plan) || {};
    const code = data.generated_code || data.code || '';
    const execution = data.execution_result || {};
    const progressStage = data.stage || '';
    const progressMessage = data.message || '';
    const outputFile = execution.output_file || (execution.execution_result && execution.execution_result.output_file) || '';
    const freqCount = execution.freq_count || (execution.execution_result && execution.execution_result.freq_count) || plan.freq_count || '';
    const repeatCount = execution.repeat_count || plan.repeat_count || '';
    const dataSource = data.data_source || execution.data_source || plan.data_source || '';
    const knowledge = Array.isArray(data.knowledge_chunks) ? data.knowledge_chunks : [];
    const validation = data.validation || {};

    card.innerHTML = `
      <div class="chat-tool-card-title">${app.escapeHtml(title || '代码生成型自主采集')}</div>
      ${progressStage || progressMessage ? `<div class="chat-tool-meta">阶段：${app.escapeHtml(progressStage || '-')} ｜ ${app.escapeHtml(progressMessage || '')}</div>` : ''}
      <div class="chat-inline-summary">
        ${freqCount ? `频点数：${app.escapeHtml(String(freqCount))} ｜ ` : ''}
        ${repeatCount ? `重复次数：${app.escapeHtml(String(repeatCount))} ｜ ` : ''}
        ${dataSource ? `数据源：${app.escapeHtml(String(dataSource))} ｜ ` : ''}
        ${outputFile ? `输出：${app.escapeHtml(outputFile)}` : '智能体正在自主规划/生成/执行采集流程'}
      </div>
      ${Object.keys(plan).length ? `<details open><summary>结构化采集计划</summary><pre class="chat-sql-block">${app.escapeHtml(JSON.stringify(plan, null, 2))}</pre></details>` : ''}
      ${Object.keys(validation).length ? `<details><summary>代码安全检查</summary><pre class="chat-sql-block">${app.escapeHtml(JSON.stringify(validation, null, 2))}</pre></details>` : ''}
      ${code ? `<details open><summary>智能体自主生成的 Python 采集函数</summary><pre class="chat-code-block"><code>${app.escapeHtml(code)}</code></pre></details>` : ''}
      ${Object.keys(execution).length ? `<details open><summary>执行结果</summary><pre class="chat-sql-block">${app.escapeHtml(JSON.stringify(execution, null, 2))}</pre></details>` : ''}
    `;
    if (knowledge.length) {
      const details = document.createElement('details');
      details.innerHTML = `<summary>检索到的内置 USRP API 知识片段</summary>`;
      knowledge.slice(0, 6).forEach((item, index) => {
        const snippet = document.createElement('div');
        snippet.className = 'chat-doc-snippet';
        snippet.innerHTML = `
          <div class="chat-doc-snippet-meta"><span>片段 ${index + 1}</span><span>${app.escapeHtml(item.title || '')}</span><span>score ${app.escapeHtml(String(item.score ?? '-'))}</span></div>
          <div class="chat-doc-snippet-text">${app.escapeHtml(item.preview || item.text || '')}</div>
        `;
        details.appendChild(snippet);
      });
      card.appendChild(details);
    }
    currentRun.steps.appendChild(card);
    scrollToLatest();
  }

  function appendSpectrumBaselineToolCard(data, title) {
    if (!currentRun) currentRun = createRunChain();
    mountRunChain();
    const card = document.createElement('div');
    card.className = 'chat-tool-card spectrum-baseline-card';
    const visuals = Array.isArray(data.visualizations) ? data.visualizations : [];
    const residual = data.residual_summary || {};
    const peaks = Array.isArray(data.top_peaks) ? data.top_peaks : (Array.isArray(data.new_peaks) ? data.new_peaks : []);
    card.innerHTML = `
      <div class="chat-tool-card-title">${app.escapeHtml(title || '频谱基线研判工具')}</div>
      <div class="chat-inline-summary">${app.escapeHtml(data.key_conclusion || data.summary || '')}</div>
      <div class="chat-tool-meta">阈值：${app.escapeHtml(String(data.threshold_db ?? '-'))} dB ｜ 中心伪峰剔除：±${app.escapeHtml(String(data.dc_exclusion_bins ?? '-'))} bins ｜ 边缘剔除：${app.escapeHtml(String(data.edge_exclusion_bins ?? '-'))} bins</div>
      ${data.verdict ? `<div class="chat-tool-meta">判定：${app.escapeHtml(data.verdict)} ｜ 风险：${app.escapeHtml(data.risk_level || '-')} ｜ 新增峰：${app.escapeHtml(String(data.new_peak_count ?? 0))} ｜ 增强峰：${app.escapeHtml(String(data.enhanced_peak_count ?? 0))} ｜ 宽带抬升：${app.escapeHtml(String(data.wideband_segment_count ?? 0))}</div>` : ''}
      ${Object.keys(residual).length ? `<div class="chat-tool-meta">残差：P95=${app.escapeHtml(String(residual.p95_residual_db ?? '-'))} dB ｜ Max=${app.escapeHtml(String(residual.max_residual_db ?? '-'))} dB ｜ 超阈值 bin=${app.escapeHtml(String(residual.bins_over_threshold ?? 0))}</div>` : ''}
    `;
    visuals.forEach(item => {
      if (!item || !item.url) return;
      const wrap = document.createElement('div');
      wrap.className = 'chat-doc-preview inline';
      wrap.innerHTML = `<div class="chat-tool-meta">${app.escapeHtml(item.title || 'Visualization')}</div><img src="${app.escapeHtml(item.url)}" alt="${app.escapeHtml(item.title || 'spectrum visualization')}">`;
      card.appendChild(wrap);
    });
    if (peaks.length) {
      const details = document.createElement('details');
      details.open = true;
      details.innerHTML = `<summary>关键频谱点预览</summary>`;
      const list = document.createElement('div');
      list.className = 'chat-doc-snippet';
      list.innerHTML = peaks.slice(0, 12).map((item, index) => `
        <div class="chat-doc-snippet-meta">
          <span>#${index + 1}</span>
          <span>${app.escapeHtml(item.frequency_label || String(item.frequency_mhz || '-'))}</span>
          <span>Power ${app.escapeHtml(String(item.power_db ?? '-'))} dB</span>
          <span>${item.prominence_db != null ? `Prom. ${app.escapeHtml(String(item.prominence_db))} dB` : ''}${item.power_delta_db != null ? `Δ ${app.escapeHtml(String(item.power_delta_db))} dB` : ''}</span>
        </div>`).join('');
      details.appendChild(list);
      card.appendChild(details);
    }
    const raw = document.createElement('details');
    raw.innerHTML = `<summary>结构化工具输出</summary><pre class="chat-sql-block">${app.escapeHtml(JSON.stringify(data || {}, null, 2))}</pre>`;
    card.appendChild(raw);
    currentRun.steps.appendChild(card);
    scrollToLatest();
  }

  function appendToolCard(toolName, data) {
    if (['extract_baseline_spectrum_peaks', 'compare_spectrum_with_baseline'].includes(toolName)) {
      const titleMap = {
        extract_baseline_spectrum_peaks: '基线主要频谱点提取',
        compare_spectrum_with_baseline: '当前采集与基线比对',
      };
      appendSpectrumBaselineToolCard(data || {}, titleMap[toolName] || toolName);
      return;
    }
    if (['retrieve_usrp_api_knowledge', 'generate_usrp_task_code', 'execute_usrp_task_code', 'run_autonomous_usrp_task', 'autonomous_usrp_progress'].includes(toolName)) {
      const titleMap = {
        retrieve_usrp_api_knowledge: '内置 USRP API 知识检索',
        generate_usrp_task_code: '智能体自主生成采集代码',
        execute_usrp_task_code: '自主采集代码执行',
        run_autonomous_usrp_task: '端到端自主 USRP 采集任务',
        autonomous_usrp_progress: '自主采集中间过程',
      };
      appendAutonomousUsrpCard(data || {}, titleMap[toolName] || toolName);
      return;
    }
    if (toolName === 'query_local_database') {
      appendDatabaseToolCard(data || {});
      return;
    }
    if (toolName === 'query_uploaded_documents') {
      appendDocumentToolCard(data || {});
      return;
    }
    appendGenericToolCard(data, toolName || '工具返回');
  }

  function updateThinkingBubbleText(text) {
    if (!state.thinkingBubble || !text) return;
    const label = state.thinkingBubble.querySelector('[data-thinking-label]');
    if (label) label.textContent = text;
  }

  function showThinkingBubble(text = '智能体思考中') {
    if (state.thinkingBubble) {
      updateThinkingBubbleText(text);
      return state.thinkingBubble;
    }
    const wrapper = document.createElement('div');
    wrapper.className = 'chat-bubble-row assistant';
    wrapper.innerHTML = `<div class="chat-bubble-stack"><div class="chat-bubble assistant thinking"><span class="chat-thinking-spinner" aria-hidden="true"></span><span data-thinking-label>${app.escapeHtml(text)}</span></div></div>`;
    timeline().appendChild(wrapper);
    state.thinkingBubble = wrapper;
    scrollToLatest();
    return wrapper;
  }

  function removeThinkingBubble() {
    if (state.thinkingBubble && state.thinkingBubble.parentNode) {
      state.thinkingBubble.parentNode.removeChild(state.thinkingBubble);
    }
    state.thinkingBubble = null;
  }

  function ensureFinalBubble(options = {}) {
    const { provisional = false } = options;
    if (!currentRun) currentRun = createRunChain();
    if (currentRun.finalBubble) return currentRun.finalBubble;
    const parent = currentRun.mounted ? currentRun.root : timeline();
    currentRun.finalBubble = createBubble('assistant', '', { parent, streaming: true, provisional });
    state.provisionalFinalBubble = provisional ? currentRun.finalBubble.closest('.chat-bubble-row') : null;
    return currentRun.finalBubble;
  }

  function appendFinalAnswerDelta(delta, options = {}) {
    const { provisional = false } = options;
    const bubble = ensureFinalBubble({ provisional });
    appendMarkdownDelta(bubble._body, delta || '');
  }

  function discardFinalBubble() {
    if (!currentRun || !currentRun.finalBubble) return;
    const row = currentRun.finalBubble.closest('.chat-bubble-row');
    if (row && row.parentNode) row.parentNode.removeChild(row);
    currentRun.finalBubble = null;
    state.provisionalFinalBubble = null;
    scrollToLatest();
  }

  function finalizeFinalBubble() {
    if (!currentRun || !currentRun.finalBubble) return;
    const row = currentRun.finalBubble.closest('.chat-bubble-row');
    if (row) row.classList.remove('provisional');
    currentRun.finalBubble.classList.remove('streaming-answer');
    const caret = currentRun.finalBubble.querySelector('.chat-stream-caret');
    if (caret) caret.remove();
    flushMarkdownRender(currentRun.finalBubble._body);
    state.provisionalFinalBubble = null;
  }

  function clearTimeline() {
    timeline().innerHTML = '';
    currentRun = null;
    state.provisionalFinalBubble = null;
    removeThinkingBubble();
  }

  function appendHistoricalRunStep(step) {
    const type = String(step && step.type || '');
    if (type === 'reasoning') {
      const text = step.content || step.text || '';
      if (text) {
        appendReasoningBlockText(text, { streaming: false });
        finalizeReasoningBlock();
      }
      return;
    }
    if (type === 'tool_call') {
      appendStep('tool_call', step.text || `调用工具：${step.tool_name || '-'}`);
      return;
    }
    if (type === 'tool_result') {
      if (step.error) {
        appendStep('error', step.text || `工具 ${step.tool_name || '-'} 执行失败：${step.error}`);
        return;
      }
      appendToolCard(step.tool_name, step.data || {});
      return;
    }
    if (type === 'case_update') {
      appendStep('case_update', step.text || step.content || '更新病例状态');
      return;
    }
    if (type === 'observation') {
      appendStep('observation', step.text || step.content || '工具返回：已收到结果');
      return;
    }
    if (step && (step.text || step.content)) {
      appendStep(type || 'observation', step.text || step.content);
    }
  }

  function createHistoricalAssistant(item) {
    const steps = Array.isArray(item.run_steps) ? item.run_steps : [];
    if (!steps.length) {
      createBubble(item.role, item.content || '', { attachments: item.attachments || [] });
      return;
    }
    const previousRun = currentRun;
    currentRun = createRunChain();
    mountRunChain();
    steps.forEach(appendHistoricalRunStep);
    finalizeReasoningBlock();
    createBubble('assistant', item.content || '', { parent: currentRun.root, attachments: item.attachments || [] });
    currentRun = previousRun;
  }

  function renderHistory(items) {
    clearTimeline();
    (items || []).forEach(item => {
      if (item.role === 'assistant') {
        createHistoricalAssistant(item);
        return;
      }
      createBubble(item.role, item.content || '', { attachments: item.attachments || [] });
    });
    state.autoFollow = true;
    scrollToLatest(true);
  }

  function updateActionStates() {
    const hasSession = !!currentSession();
    newSessionBtn().disabled = state.isGenerating;
    chatModeSelector().disabled = state.isGenerating;
    nl2sqlButton().disabled = state.isGenerating || state.chatMode === 'general';
    chatUploadButton().disabled = state.isGenerating || !hasSession;
    sessionList().classList.toggle('disabled', state.isGenerating);
    if (modelSettingsReset()) modelSettingsReset().disabled = state.isGenerating;
    sendBtn().textContent = state.isGenerating ? '停止生成' : '发送';
    sendBtn().classList.toggle('is-stop', state.isGenerating);
    sendBtn().disabled = !hasSession;
  }

  function setGenerating(generating) {
    state.isGenerating = generating;
    if (generating) {
      state.openSessionMenuId = null;
    }
    updateActionStates();
  }

  function loadChatMode() {
    const stored = sessionStorage.getItem(CHAT_MODE_KEY);
    state.chatMode = stored === 'workspace' ? 'workspace' : 'general';
  }

  function renderChatMode() {
    chatModeSelector().querySelectorAll('[data-chat-mode]').forEach(button => {
      const active = button.dataset.chatMode === state.chatMode;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    if (state.chatMode === 'general') togglePopover(false);
    updateActionStates();
  }

  function setChatMode(mode) {
    state.chatMode = mode === 'workspace' ? 'workspace' : 'general';
    sessionStorage.setItem(CHAT_MODE_KEY, state.chatMode);
    renderChatMode();
  }

  function formatSessionTime(value) {
    if (!value) return '-';
    const date = new Date(value);
    return date.toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
  }

  function updateSessionHeader() {
    updateActionStates();
    renderDraftAttachments();
  }

  function renderSessionList() {
    const container = sessionList();
    container.innerHTML = '';
    if (!state.sessions.length) {
      container.innerHTML = '<div class="empty-state">还没有会话，点击“新聊天”开始。</div>';
      updateSessionHeader();
      return;
    }
    state.sessions.forEach(item => {
      const isActive = item.id === state.currentSessionId;
      const isMenuOpen = state.openSessionMenuId === item.id;
      const node = document.createElement('div');
      node.className = `chat-session-item${isActive ? ' active' : ''}${item.is_generating ? ' generating' : ''}`;
      node.dataset.sessionId = item.id;
      node.innerHTML = `
        <div class="chat-session-item-body">
          <div class="chat-session-title-row">
            <div class="chat-session-title">${app.escapeHtml(item.title || '新对话')}</div>
            <div class="chat-session-time">${app.escapeHtml(formatSessionTime(item.updated_at))}</div>
          </div>
          <div class="chat-session-preview">${app.escapeHtml(item.last_message_preview || '暂时还没有消息')}</div>
        </div>
        ${isActive ? `
          <div class="chat-session-menu-anchor">
            <button class="chat-session-menu-trigger" type="button" data-session-menu-trigger="${app.escapeHtml(item.id)}" aria-label="会话菜单" aria-expanded="${isMenuOpen ? 'true' : 'false'}">…</button>
            <div class="chat-session-menu-popover${isMenuOpen ? '' : ' hidden'}" data-session-menu-popover="${app.escapeHtml(item.id)}">
              <button class="chat-session-menu-action" type="button" data-session-action="rename" data-session-id="${app.escapeHtml(item.id)}">重命名</button>
              <button class="chat-session-menu-action delete" type="button" data-session-action="delete" data-session-id="${app.escapeHtml(item.id)}">删除</button>
            </div>
          </div>
        ` : ''}
      `;
      container.appendChild(node);
    });
    updateSessionHeader();
  }

  function patchSession(itemPatch) {
    const index = state.sessions.findIndex(item => item.id === itemPatch.id);
    if (index >= 0) {
      state.sessions[index] = { ...state.sessions[index], ...itemPatch };
    } else {
      state.sessions.push(itemPatch);
    }
    sortSessions();
    renderSessionList();
    updateSessionHeader();
  }

  function summarizeOperatorPreview(data) {
    const content = String(data.content || '').trim();
    if (content) return content;
    const attachments = Array.isArray(data.attachments) ? data.attachments.map(normalizeAttachment) : [];
    if (!attachments.length) return '';
    return `已上传 ${attachments.map(item => item.file_name).join('，')}`;
  }

  function patchCurrentSessionFromMessage(data) {
    const session = currentSession();
    if (!session) return;
    patchSession({
      ...session,
      title: data.session_title || session.title,
      updated_at: data.created_at || new Date().toISOString(),
      last_message_preview: summarizeOperatorPreview(data) || session.last_message_preview,
      last_message_role: data.role || session.last_message_role,
      message_count: Number(session.message_count || 0) + 1,
    });
  }

  async function fetchSessions() {
    const data = await app.api('/api/chat/sessions');
    return Array.isArray(data.items) ? data.items : [];
  }

  async function ensureSessionsLoaded() {
    let items = await fetchSessions();
    if (!items.length) {
      const created = await app.api('/api/chat/sessions', {
        method: 'POST',
        body: JSON.stringify({ title: '' }),
      });
      if (created.item) {
        items = [created.item];
      }
      if (!items.length) {
        items = await fetchSessions();
      }
    }
    state.sessions = items;
    sortSessions();
  }

  async function loadHistory(sessionId) {
    const data = await app.api(`/api/chat/sessions/${encodeURIComponent(sessionId)}/history`);
    if (data.session) patchSession(data.session);
    renderHistory(data.items || []);
  }

  async function switchSession(sessionId, options = {}) {
    const { force = false, skipGeneratingCheck = false } = options;
    if (!sessionId) return;
    if (!force && sessionId === state.currentSessionId) return;
    if (!skipGeneratingCheck && state.isGenerating) return;
    state.currentSessionId = sessionId;
    state.openSessionMenuId = null;
    window.localStorage.setItem(STORAGE_KEY, sessionId);
    renderSessionList();
    updateSessionHeader();
    await loadHistory(sessionId);
    renderDraftAttachments();
    updateAttachmentHint();
    updateSessionHeader();
    input().focus();
  }

  async function initializeSessions() {
    await ensureSessionsLoaded();
    renderSessionList();
    const stored = window.localStorage.getItem(STORAGE_KEY);
    const candidate = state.sessions.some(item => item.id === stored)
      ? stored
      : (state.sessions[0] && state.sessions[0].id);
    if (candidate) {
      await switchSession(candidate, { force: true, skipGeneratingCheck: true });
    }
  }

  async function refreshSessions(options = {}) {
    const { reloadHistory = false } = options;
    await ensureSessionsLoaded();
    if (!state.sessions.length) {
      clearTimeline();
      renderSessionList();
      return;
    }
    if (!state.sessions.some(item => item.id === state.currentSessionId)) {
      await switchSession(state.sessions[0].id, { force: true, skipGeneratingCheck: true });
      return;
    }
    renderSessionList();
    updateSessionHeader();
    if (reloadHistory && state.currentSessionId) {
      await loadHistory(state.currentSessionId);
      updateSessionHeader();
    }
  }

  async function createNewSession() {
    if (state.isGenerating) return;
    const data = await app.api('/api/chat/sessions', {
      method: 'POST',
      body: JSON.stringify({ title: '' }),
    });
    if (!data.item) return;
    patchSession(data.item);
    await switchSession(data.item.id, { force: true, skipGeneratingCheck: true });
  }

  async function renameCurrentSession() {
    if (state.isGenerating) return;
    const session = currentSession();
    if (!session) return;
    const nextTitle = window.prompt('输入新的会话名称', session.title || '');
    if (nextTitle == null) return;
    const data = await app.api(`/api/chat/sessions/${encodeURIComponent(session.id)}`, {
      method: 'PATCH',
      body: JSON.stringify({ title: nextTitle }),
    });
    if (!data.item) return;
    patchSession(data.item);
  }

  async function deleteCurrentSession() {
    if (state.isGenerating) return;
    const session = currentSession();
    if (!session) return;
    const confirmed = window.confirm(`确定删除会话“${session.title || '新对话'}”吗？删除后不可恢复。`);
    if (!confirmed) return;
    const resp = await fetch(`/api/chat/sessions/${encodeURIComponent(session.id)}`, { method: 'DELETE' });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.detail || '删除会话失败');
    }
    delete state.draftAttachmentsBySession[session.id];
    state.sessions = state.sessions.filter(item => item.id !== session.id);
    if (data.fallback_session) {
      patchSession(data.fallback_session);
      await switchSession(data.fallback_session.id, { force: true, skipGeneratingCheck: true });
      return;
    }
    if (!state.sessions.length) {
      await createNewSession();
      return;
    }
    await switchSession(state.sessions[0].id, { force: true, skipGeneratingCheck: true });
  }

  function closeSessionMenu() {
    if (!state.openSessionMenuId) return;
    state.openSessionMenuId = null;
    renderSessionList();
  }

  function toggleSessionMenu(sessionId) {
    state.openSessionMenuId = state.openSessionMenuId === sessionId ? null : sessionId;
    renderSessionList();
  }

  function getDatabaseRecord(databaseId) {
    return state.databases.find(item => item.database_id === databaseId) || null;
  }

  function databaseItems() {
    return state.databases.length
      ? state.databases
      : [{ database_id: 'default', display_name: '默认数据库', is_default: true, tables: [] }];
  }

  function databaseLabel(record) {
    return String((record && (record.display_name || record.file_name || record.database_id)) || '默认数据库');
  }

  function databaseTables(record) {
    return Array.isArray(record && record.tables) ? record.tables.map(item => String(item)) : [];
  }

  function databaseSourceLabel(record) {
    if (!record || record.is_default || record.database_id === 'default') return '内置默认库';
    if (record.source_kind === 'excel_import') return record.file_name ? `Excel 导入：${record.file_name}` : 'Excel 导入';
    if (record.source_kind === 'table_import') return record.file_name ? `表格导入：${record.file_name}` : '表格导入';
    if (record.source_kind === 'sqlite_upload') return record.file_name ? `SQLite 上传：${record.file_name}` : 'SQLite 上传';
    return record.file_name || record.sqlite_file_name || record.database_id;
  }

  function databaseTitle(record) {
    const tables = databaseTables(record);
    return `${databaseLabel(record)}\n表：${tables.length ? tables.join('、') : '暂无表'}`;
  }

  function tablePreviewText(tables, limit = 5) {
    if (!tables.length) return '暂无表';
    const head = tables.slice(0, limit).join('、');
    return tables.length > limit ? `${head} 等 ${tables.length} 张表` : head;
  }

  function getNL2SQLPayload() {
    return {
      force_enabled: !!state.nl2sql.forceEnabled,
      auto_select_tables: !!state.nl2sql.autoSelectTables,
      manual_selected_tables: state.nl2sql.autoSelectTables ? [] : [...state.nl2sql.manualSelectedTables],
      database_id: state.nl2sql.databaseId || '',
      db_path: state.nl2sql.dbPath || '',
    };
  }

  function clampNumber(value, min, max, fallback) {
    const number = Number(value);
    if (!Number.isFinite(number)) return fallback;
    return Math.min(max, Math.max(min, number));
  }

  function normalizeModelOptions(raw = {}) {
    return {
      temperature: clampNumber(raw.temperature, 0, 2, DEFAULT_MODEL_OPTIONS.temperature),
      top_p: clampNumber(raw.top_p, 0, 1, DEFAULT_MODEL_OPTIONS.top_p),
      top_k: Math.round(clampNumber(raw.top_k, 1, 200, DEFAULT_MODEL_OPTIONS.top_k)),
      min_p: clampNumber(raw.min_p, 0, 1, DEFAULT_MODEL_OPTIONS.min_p),
      presence_penalty: clampNumber(raw.presence_penalty, -2, 2, DEFAULT_MODEL_OPTIONS.presence_penalty),
      repetition_penalty: clampNumber(raw.repetition_penalty, 0.01, 2, DEFAULT_MODEL_OPTIONS.repetition_penalty),
      enable_thinking: raw.enable_thinking == null ? DEFAULT_MODEL_OPTIONS.enable_thinking : !!raw.enable_thinking,
      preserve_thinking: raw.preserve_thinking == null ? DEFAULT_MODEL_OPTIONS.preserve_thinking : !!raw.preserve_thinking,
      reasoning_effort: raw.reasoning_effort || DEFAULT_MODEL_OPTIONS.reasoning_effort,
      thinking_token_budget: Math.round(clampNumber(raw.thinking_token_budget, 1, 131072, DEFAULT_MODEL_OPTIONS.thinking_token_budget)),
    };
  }

  function getModelOptionsPayload() {
    state.modelOptions = normalizeModelOptions(state.modelOptions);
    var payload = { ...state.modelOptions };
    console.log('[FRONTEND] thinking_token_budget =', payload.thinking_token_budget, '| full llm_options keys:', Object.keys(payload).join(', '));
    return payload;
  }

  function persistModelOptions() {
    window.localStorage.setItem(MODEL_SETTINGS_KEY, JSON.stringify(getModelOptionsPayload()));
  }

  function loadModelOptions() {
    try {
      const stored = JSON.parse(window.localStorage.getItem(MODEL_SETTINGS_KEY) || '{}');
      state.modelOptions = normalizeModelOptions({ ...DEFAULT_MODEL_OPTIONS, ...stored });
    } catch (error) {
      console.error(error);
      state.modelOptions = { ...DEFAULT_MODEL_OPTIONS };
    }
  }

  function renderModelSettings() {
    if (!llmTemperature()) return;
    const options = normalizeModelOptions(state.modelOptions);
    state.modelOptions = options;
    llmTemperature().value = String(options.temperature);
    llmTemperatureValue().textContent = options.temperature.toFixed(2);
    llmTopP().value = String(options.top_p);
    llmTopPValue().textContent = options.top_p.toFixed(2);
    llmTopK().value = String(options.top_k);
    llmMinP().value = String(options.min_p);
    llmMinPValue().textContent = options.min_p.toFixed(2);
    llmPresencePenalty().value = String(options.presence_penalty);
    llmPresencePenaltyValue().textContent = options.presence_penalty.toFixed(2);
    llmRepetitionPenalty().value = String(options.repetition_penalty);
    llmRepetitionPenaltyValue().textContent = options.repetition_penalty.toFixed(2);
    llmEnableThinking().checked = !!options.enable_thinking;
    llmPreserveThinking().checked = !!options.preserve_thinking;
    if (llmReasoningEffort()) llmReasoningEffort().value = options.reasoning_effort;
    if (llmThinkingTokenBudget()) llmThinkingTokenBudget().value = String(options.thinking_token_budget);
  }

  function syncModelSettingFromInput(event) {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    state.modelOptions = {
      temperature: Number(llmTemperature().value),
      top_p: Number(llmTopP().value),
      top_k: Number(llmTopK().value),
      min_p: Number(llmMinP().value),
      presence_penalty: Number(llmPresencePenalty().value),
      repetition_penalty: Number(llmRepetitionPenalty().value),
      enable_thinking: !!llmEnableThinking().checked,
      preserve_thinking: !!llmPreserveThinking().checked,
      reasoning_effort: llmReasoningEffort() ? llmReasoningEffort().value : DEFAULT_MODEL_OPTIONS.reasoning_effort,
      thinking_token_budget: llmThinkingTokenBudget() ? parseInt(llmThinkingTokenBudget().value, 10) || DEFAULT_MODEL_OPTIONS.thinking_token_budget : DEFAULT_MODEL_OPTIONS.thinking_token_budget,
    };
    renderModelSettings();
    persistModelOptions();
  }

  function setModelSettingsOpen(open) {
    state.modelSettingsOpen = !!open;
    const drawer = modelSettingsDrawer();
    const backdrop = modelSettingsBackdrop();
    if (!drawer || !backdrop) return;
    drawer.classList.toggle('hidden', !state.modelSettingsOpen);
    backdrop.classList.toggle('hidden', !state.modelSettingsOpen);
    drawer.setAttribute('aria-hidden', state.modelSettingsOpen ? 'false' : 'true');
  }

  function renderDatabaseSelect() {
    const select = nl2sqlDbSelect();
    select.innerHTML = '';
    const items = databaseItems();
    items.forEach(item => {
      const option = document.createElement('option');
      option.value = item.database_id;
      option.textContent = databaseLabel(item);
      option.title = databaseTitle(item);
      if (item.database_id === state.nl2sql.databaseId) option.selected = true;
      select.appendChild(option);
    });
    renderDatabaseDropdown(items);
  }

  function setDatabaseDropdownOpen(open) {
    state.dbDropdownOpen = !!open;
    renderDatabaseSelect();
  }

  function renderDatabaseDropdown(items = databaseItems()) {
    const trigger = nl2sqlDbDropdownTrigger();
    const label = nl2sqlDbDropdownLabel();
    const menu = nl2sqlDbDropdownMenu();
    const list = nl2sqlDbDropdownList();
    if (!trigger || !label || !menu || !list) return;

    const selected = getDatabaseRecord(state.nl2sql.databaseId) || items[0];
    label.textContent = databaseLabel(selected);
    trigger.title = databaseTitle(selected);
    trigger.setAttribute('aria-expanded', state.dbDropdownOpen ? 'true' : 'false');
    menu.classList.toggle('hidden', !state.dbDropdownOpen);

    const validIds = new Set(items.map(item => String(item.database_id || '')));
    if (!validIds.has(state.hoverDatabaseId)) {
      state.hoverDatabaseId = String((selected && selected.database_id) || '');
    }

    list.innerHTML = '';
    items.forEach(item => {
      const databaseId = String(item.database_id || '');
      const tables = databaseTables(item);
      const option = document.createElement('div');
      option.className = 'nl2sql-db-option';
      option.dataset.databaseId = databaseId;
      option.setAttribute('role', 'option');
      option.setAttribute('aria-selected', databaseId === state.nl2sql.databaseId ? 'true' : 'false');
      option.title = databaseTitle(item);
      if (databaseId === state.nl2sql.databaseId) option.classList.add('active');

      if (state.renamingDatabaseId === databaseId && !item.is_default) {
        option.classList.add('is-renaming');
        option.innerHTML = `
          <div class="nl2sql-db-rename-form">
            <input class="nl2sql-db-rename-input" type="text" value="${app.escapeHtml(databaseLabel(item))}" maxlength="80" aria-label="数据库名称">
            <button class="nl2sql-db-inline-btn" type="button" data-save-db="${app.escapeHtml(databaseId)}">保存</button>
            <button class="nl2sql-db-inline-btn ghost" type="button" data-cancel-db-rename="${app.escapeHtml(databaseId)}">取消</button>
          </div>
        `;
      } else {
        const renameButton = item.is_default
          ? ''
          : `<button class="nl2sql-db-rename-btn" type="button" data-rename-db="${app.escapeHtml(databaseId)}">改名</button>`;
        option.innerHTML = `
          <div class="nl2sql-db-option-main">
            <div class="nl2sql-db-option-title">${app.escapeHtml(databaseLabel(item))}</div>
            <div class="nl2sql-db-option-meta">${app.escapeHtml(databaseSourceLabel(item))}</div>
          </div>
          <div class="nl2sql-db-option-side">
            <span class="nl2sql-db-table-pill">${app.escapeHtml(String(tables.length))} 表</span>
            ${renameButton}
          </div>
          <div class="nl2sql-db-option-tables">${app.escapeHtml(tablePreviewText(tables, 3))}</div>
        `;
      }
      list.appendChild(option);
    });
    renderDatabaseHoverPanel();
  }

  function renderDatabaseHoverPanel() {
    const panel = nl2sqlDbHoverPanel();
    if (!panel) return;
    const record = getDatabaseRecord(state.hoverDatabaseId) || getDatabaseRecord(state.nl2sql.databaseId) || databaseItems()[0];
    const tables = databaseTables(record);
    const tableItems = tables.length
      ? tables.map(name => `<span class="nl2sql-db-hover-table">${app.escapeHtml(name)}</span>`).join('')
      : '<div class="nl2sql-loading-text">当前数据库暂无表。</div>';
    panel.innerHTML = `
      <div class="nl2sql-db-hover-title">${app.escapeHtml(databaseLabel(record))}</div>
      <div class="nl2sql-db-hover-meta">${app.escapeHtml(databaseSourceLabel(record))}</div>
      <div class="nl2sql-db-hover-count">${app.escapeHtml(String(tables.length))} 张表</div>
      <div class="nl2sql-db-hover-body">${tableItems}</div>
    `;
  }

  async function renameDatabase(databaseId, displayName) {
    const resp = await fetch(`/api/nl2sql/databases/${encodeURIComponent(databaseId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: displayName }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.detail || '数据库重命名失败');
    }
    if (data.item) {
      mergeDatabaseRecord(data.item);
      if (state.nl2sql.databaseId === databaseId) {
        state.nl2sql.sourceLabel = databaseLabel(data.item);
      }
    }
    state.renamingDatabaseId = '';
    renderDatabaseSelect();
    updateConfigHint();
  }

  function normalizeBaselineText(text) {
    const seen = new Set();
    const result = [];
    String(text || '')
      .replace(/，/g, '\n')
      .replace(/,/g, '\n')
      .split(/\r?\n/)
      .forEach(item => {
        const normalized = item.trim().replace(/\s+/g, ' ');
        if (!normalized || seen.has(normalized)) return;
        seen.add(normalized);
        result.push(normalized);
      });
    return result;
  }

  function mergePlaceBaseline(data) {
    state.placeBaseline = {
      loaded: true,
      loading: false,
      initialized: !!data.initialized,
      placeId: data.place_id || '',
      fingerprints: Array.isArray(data.fingerprints) ? data.fingerprints.map(String) : [],
      candidateFingerprints: Array.isArray(data.candidate_fingerprints) ? data.candidate_fingerprints.map(String) : [],
      source: data.source || '',
      updatedAt: data.updated_at || '',
    };
  }

  function renderPlaceBaseline() {
    const status = placeBaselineStatus();
    const inputNode = placeBaselineInput();
    const hint = placeBaselineHint();
    if (!status || !inputNode || !hint) return;

    const baseline = state.placeBaseline;
    const activeItems = baseline.initialized ? baseline.fingerprints : baseline.candidateFingerprints;
    if (document.activeElement !== inputNode) {
      inputNode.value = activeItems.join('\n');
    }

    if (baseline.loading) {
      status.textContent = '正在读取场所基线...';
      hint.textContent = '基线信息来自工作区知识，可初始化后手动编辑。';
      return;
    }
    const count = activeItems.length;
    status.textContent = baseline.initialized
      ? `已初始化：${baseline.placeId || '当前场所'}，${count} 条基线。`
      : `待初始化：工作区知识中有 ${count} 条候选基线。`;
    hint.textContent = baseline.initialized
      ? '修改后点击“保存”，模型和工具会使用保存后的场所基线。'
      : '点击“从工作区初始化”后，候选基线会保存到当前任务；也可以直接编辑后保存。';
  }

  async function loadPlaceBaseline(forceReload = false) {
    if (!forceReload && (state.placeBaseline.loaded || state.placeBaseline.loading)) {
      renderPlaceBaseline();
      return;
    }
    state.placeBaseline.loading = true;
    renderPlaceBaseline();
    try {
      const data = await app.api('/api/workspace/place-baseline');
      mergePlaceBaseline(data);
    } catch (error) {
      console.error(error);
      placeBaselineStatus().textContent = '读取场所基线失败。';
      placeBaselineHint().textContent = error.message || '请确认后端服务已启动。';
    } finally {
      state.placeBaseline.loading = false;
      renderPlaceBaseline();
    }
  }

  async function initializePlaceBaseline() {
    placeBaselineInitButton().disabled = true;
    try {
      const items = normalizeBaselineText(placeBaselineInput().value);
      const data = await app.api('/api/workspace/place-baseline/initialize', {
        method: 'POST',
        body: JSON.stringify({
          place_id: state.placeBaseline.placeId || '',
          fingerprints: items,
        }),
      });
      mergePlaceBaseline(data);
      renderPlaceBaseline();
    } finally {
      placeBaselineInitButton().disabled = false;
    }
  }

  async function savePlaceBaseline() {
    placeBaselineSaveButton().disabled = true;
    try {
      const data = await app.api('/api/workspace/place-baseline', {
        method: 'PUT',
        body: JSON.stringify({
          place_id: state.placeBaseline.placeId || '',
          fingerprints: normalizeBaselineText(placeBaselineInput().value),
        }),
      });
      mergePlaceBaseline(data);
      renderPlaceBaseline();
    } finally {
      placeBaselineSaveButton().disabled = false;
    }
  }

  function updateDbStatus() {
    const record = getDatabaseRecord(state.nl2sql.databaseId);
    if (!record || state.nl2sql.databaseId === 'default') {
      nl2sqlDbStatus().textContent = '当前使用默认数据库。';
      return;
    }
    nl2sqlDbStatus().textContent = `当前已选择：${databaseLabel(record)}`;
  }

  function updateConfigHint() {
    const tablesText = state.nl2sql.autoSelectTables
      ? '自动选表已开启'
      : `手动已选 ${state.nl2sql.manualSelectedTables.length || 0} 张表。`;
    const dbText = state.nl2sql.databaseId && state.nl2sql.databaseId !== 'default'
      ? `数据库：${state.nl2sql.sourceLabel || state.nl2sql.databaseId}`
      : '数据库：默认';
    nl2sqlConfigHint().textContent = `强制NL2SQL ${state.nl2sql.forceEnabled ? '已勾选' : '未勾选'}；${tablesText}；${dbText}`;
    updateDbStatus();
  }

  function renderManualTableOptions() {
    const container = nl2sqlManualList();
    container.innerHTML = '';

    if (!state.nl2sql.loaded) {
      container.innerHTML = '<div class="nl2sql-loading-text">正在读取表名...</div>';
      return;
    }

    const allTables = state.nl2sql.allTables || [];
    if (!allTables.length) {
      container.innerHTML = '<div class="nl2sql-loading-text">当前数据库暂无表。</div>';
      return;
    }

    const selected = new Set(state.nl2sql.manualSelectedTables);
    const allChecked = allTables.length > 0 && allTables.every(name => selected.has(name));
    container.appendChild(createManualItem('全部表', '__all__', allChecked));
    allTables.forEach(name => {
      container.appendChild(createManualItem(name, name, selected.has(name)));
    });
  }

  function createManualItem(label, value, checked) {
    const wrapper = document.createElement('label');
    wrapper.className = 'nl2sql-manual-item';
    wrapper.innerHTML = `<input type="checkbox" value="${app.escapeHtml(value)}" ${checked ? 'checked' : ''}><span>${app.escapeHtml(label)}</span>`;
    wrapper.querySelector('input').disabled = !!state.nl2sql.autoSelectTables;
    return wrapper;
  }

  function applyManualSectionState() {
    const disabled = !!state.nl2sql.autoSelectTables;
    nl2sqlManualSection().classList.toggle('disabled', disabled);
    nl2sqlManualSection().setAttribute('aria-disabled', disabled ? 'true' : 'false');
    nl2sqlManualList().querySelectorAll('input').forEach(node => {
      node.disabled = disabled;
    });
    renderDatabaseSelect();
    updateConfigHint();
  }

  function mergeDatabaseRecord(record) {
    const index = state.databases.findIndex(item => item.database_id === record.database_id);
    if (index >= 0) {
      state.databases[index] = { ...state.databases[index], ...record };
    } else {
      state.databases.push(record);
    }
    state.databases.sort((a, b) => {
      if (a.database_id === 'default') return -1;
      if (b.database_id === 'default') return 1;
      return String(b.created_at || '').localeCompare(String(a.created_at || ''));
    });
  }

  async function selectDatabase(databaseId, options = {}) {
    const { reloadTables = true } = options;
    const record = getDatabaseRecord(databaseId) || getDatabaseRecord('default');
    state.nl2sql.databaseId = record ? record.database_id : 'default';
    state.nl2sql.dbPath = record ? (record.db_path || '') : '';
    state.nl2sql.sourceLabel = record ? (record.display_name || record.file_name || record.database_id) : '默认数据库';
    state.nl2sql.allTables = Array.isArray(record && record.tables) ? [...record.tables] : [];
    state.nl2sql.loaded = Array.isArray(record && record.tables);
    state.nl2sql.manualSelectedTables = [];
    state.hoverDatabaseId = state.nl2sql.databaseId;
    renderDatabaseSelect();
    renderManualTableOptions();
    applyManualSectionState();
    renderPlaceBaseline();
    updateConfigHint();
    if (reloadTables) {
      await loadTablesIfNeeded(true);
    }
  }

  async function loadDatabaseCatalog(preferredDatabaseId) {
    const data = await app.api('/api/nl2sql/databases');
    state.databases = Array.isArray(data.items) ? data.items : [];
    if (!state.databases.some(item => item.database_id === 'default')) {
      state.databases.unshift({
        database_id: 'default',
        display_name: '默认数据库',
        file_name: '默认数据库',
        db_path: '',
        tables: [],
        is_default: true,
        source_kind: 'default',
      });
    }
    const serverPreferredId = data.preferred_database_id && getDatabaseRecord(data.preferred_database_id)
      ? data.preferred_database_id
      : '';
    const nextId = preferredDatabaseId && getDatabaseRecord(preferredDatabaseId)
      ? preferredDatabaseId
      : (state.databases.some(item => item.database_id === state.nl2sql.databaseId) ? state.nl2sql.databaseId : (serverPreferredId || 'default'));
    await selectDatabase(nextId, { reloadTables: false });
  }

  async function loadTablesIfNeeded(forceReload = false) {
    if (!forceReload && (state.nl2sql.loaded || state.nl2sql.loading)) return;
    state.nl2sql.loading = true;
    if (forceReload) {
      state.nl2sql.loaded = false;
      state.nl2sql.manualSelectedTables = [];
    }
    renderManualTableOptions();
    try {
      const params = new URLSearchParams();
      if (state.nl2sql.databaseId) params.set('database_id', state.nl2sql.databaseId);
      if (!state.nl2sql.databaseId && state.nl2sql.dbPath) params.set('db_path', state.nl2sql.dbPath);
      const query = params.toString() ? `?${params.toString()}` : '';
      const data = await app.api(`/api/nl2sql/tables${query}`);
      state.nl2sql.allTables = Array.isArray(data.tables) ? data.tables : [];
      const record = getDatabaseRecord(state.nl2sql.databaseId);
      if (record) record.tables = [...state.nl2sql.allTables];
      state.nl2sql.loaded = true;
    } catch (error) {
      console.error(error);
      nl2sqlManualList().innerHTML = '<div class="nl2sql-loading-text">读取表名失败。</div>';
    } finally {
      state.nl2sql.loading = false;
      renderManualTableOptions();
      applyManualSectionState();
      updateConfigHint();
    }
  }

  async function uploadSelectedDb(file) {
    const formData = new FormData();
    formData.append('file', file);
    const resp = await fetch('/api/nl2sql/upload-db', { method: 'POST', body: formData });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.detail || '数据库上传失败');
    }
    mergeDatabaseRecord({
      database_id: data.database_id,
      display_name: data.display_name || data.file_name || data.database_id,
      file_name: data.file_name,
      db_path: data.db_path,
      tables: Array.isArray(data.tables) ? data.tables : [],
      is_default: false,
      source_kind: data.source_kind,
      source_suffix: data.source_suffix,
      sqlite_file_name: data.sqlite_file_name,
      created_at: new Date().toISOString(),
    });
    await selectDatabase(data.database_id, { reloadTables: false });
    state.nl2sql.allTables = Array.isArray(data.tables) ? data.tables : [];
    state.nl2sql.loaded = true;
    state.nl2sql.manualSelectedTables = [];
    renderManualTableOptions();
    applyManualSectionState();
    updateConfigHint();
  }

  async function resetSelectedDb() {
    await selectDatabase('default', { reloadTables: true });
  }

  function togglePopover(show) {
    if (!show) {
      state.dbDropdownOpen = false;
      state.renamingDatabaseId = '';
    }
    nl2sqlPopover().classList.toggle('hidden', !show);
    if (show) {
      void loadTablesIfNeeded();
      void loadPlaceBaseline();
      updateConfigHint();
    }
    renderDatabaseSelect();
  }

  function handleManualListChange(event) {
    const target = event.target;
    if (!(target instanceof HTMLInputElement) || target.disabled) return;
    const value = target.value;
    const allTables = state.nl2sql.allTables || [];
    const selected = new Set(state.nl2sql.manualSelectedTables);

    if (value === '__all__') {
      state.nl2sql.manualSelectedTables = target.checked ? [...allTables] : [];
      renderManualTableOptions();
      applyManualSectionState();
      return;
    }

    if (target.checked) {
      selected.add(value);
    } else {
      selected.delete(value);
    }
    state.nl2sql.manualSelectedTables = allTables.filter(name => selected.has(name));
    renderManualTableOptions();
    applyManualSectionState();
  }

  function buildUploadFormData(files) {
    const formData = new FormData();
    if (state.currentSessionId) formData.append('session_id', state.currentSessionId);
    Array.from(files || []).forEach(file => formData.append('files', file));
    return formData;
  }

  async function parseUploadJsonResponse(resp, fallbackMessage) {
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || data.message || fallbackMessage || '文件上传失败');
    }
    return Array.isArray(data.items) ? data.items : [];
  }

  async function uploadChatFilesViaLegacy(files) {
    const resp = await fetch('/api/chat/uploads', { method: 'POST', body: buildUploadFormData(files) });
    return parseUploadJsonResponse(resp, '文件上传失败');
  }

  async function uploadChatFilesViaStream(files) {
    const resp = await fetch('/api/chat/uploads/stream', { method: 'POST', body: buildUploadFormData(files) });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || data.message || `流式上传接口不可用：${resp.status}`);
    }
    if (!resp.body || typeof TextDecoder === 'undefined') {
      return parseUploadJsonResponse(resp, '文件上传失败');
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    let uploadedItems = [];

    const consumeLine = rawLine => {
      const line = rawLine.trim();
      if (!line) return;
      let event;
      try {
        event = JSON.parse(line);
      } catch (error) {
        console.warn('无法解析上传进度事件', line, error);
        return;
      }
      if (event.type === 'progress') {
        const fileName = event.file_name ? `${event.file_name}：` : '';
        const message = event.message || '正在解析文件';
        updateUploadProgress(event.percent, `${fileName}${message} ${Math.round(Number(event.file_percent ?? event.percent) || 0)}%`);
      } else if (event.type === 'done') {
        uploadedItems = Array.isArray(event.items) ? event.items : [];
        updateUploadProgress(100, '解析完成 100%');
      } else if (event.type === 'error') {
        throw new Error(event.message || '文件上传失败');
      }
    };

    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const rawLine of lines) consumeLine(rawLine);
      if (done) break;
    }
    if (buffer.trim()) consumeLine(buffer);
    return uploadedItems;
  }

  async function uploadChatFiles(files) {
    try {
      return await uploadChatFilesViaStream(files);
    } catch (streamError) {
      console.warn('流式上传失败，尝试兼容 /api/chat/uploads：', streamError);
      updateUploadProgress(5, '流式上传不可用，正在切换兼容接口...');
      try {
        const items = await uploadChatFilesViaLegacy(files);
        updateUploadProgress(100, '兼容上传完成 100%');
        return items;
      } catch (legacyError) {
        legacyError.message = legacyError.message || streamError.message || '文件上传失败';
        throw legacyError;
      }
    }
  }

  function handleEvent(eventName, data) {
    if (data.session_id && data.session_id !== state.currentSessionId) return;
    if (eventName === 'message' && data.role === 'operator') {
      createBubble('operator', data.content || '', { attachments: data.attachments || [] });
      currentRun = createRunChain();
      showThinkingBubble('流式连接中，等待模型输出...');
      if (hasImageAttachment(data.attachments || [])) {
        appendStep('observation', '已接收图片附件，正在送入多模态模型解析。');
      } else {
        appendStep('observation', '请求已发送，正在等待模型流式输出。');
      }
      patchCurrentSessionFromMessage(data);
      state.autoFollow = true;
      scrollToLatest(true);
      return;
    }
    if (eventName === 'stream_open') {
      showThinkingBubble(data.text || '流式连接已建立，正在等待模型输出。');
      return;
    }
    if (eventName === 'reasoning_start' || eventName === 'assistant_reasoning_start') {
      beginReasoningBlock();
      return;
    }
    if (eventName === 'reasoning_delta' || eventName === 'assistant_reasoning' || eventName === 'thought_delta') {
      appendReasoningDelta(data.delta || data.text || '');
      return;
    }
    if (eventName === 'reasoning_done' || eventName === 'assistant_reasoning_done' || eventName === 'thought_done') {
      finalizeReasoningBlock();
      return;
    }
    if (eventName === 'reasoning' || eventName === 'thought') {
      appendReasoningDelta(data.delta || data.text || '');
      finalizeReasoningBlock();
      return;
    }
    if (eventName === 'tool_call') {
      removeThinkingBubble();
      appendStep('tool_call', data.text || `调用工具：${data.tool_name || '-'}`);
      return;
    }
    if (eventName === 'observation') {
      removeThinkingBubble();
      appendStep('observation', data.text || '工具返回：已收到结果');
      return;
    }
    if (eventName === 'tool_result') {
      removeThinkingBubble();
      appendToolCard(data.tool_name, data.data || {});
      return;
    }
    if (eventName === 'case_update') {
      appendStep('case_update', data.text || '更新病例状态');
      return;
    }
    if (eventName === 'final_answer_start') {
      removeThinkingBubble();
      finalizeReasoningBlock();
      ensureFinalBubble({ provisional: !!data.provisional });
      return;
    }
    if (eventName === 'final_answer_delta') {
      appendFinalAnswerDelta(data.delta || '', { provisional: !!data.provisional });
      return;
    }
    if (eventName === 'final_answer_discard') {
      discardFinalBubble();
      return;
    }
    if (eventName === 'error') {
      removeThinkingBubble();
      appendStep('error', `错误：${data.message || '未知错误'}`);
      return;
    }
    if (eventName === 'done') {
      removeThinkingBubble();
      finalizeReasoningBlock();
      if (data.cancelled) {
        discardFinalBubble();
        appendStep('observation', '已停止生成');
      } else {
        finalizeFinalBubble();
      }
      currentRun = null;
      setGenerating(false);
      void refreshSessions();
      input().focus();
    }
  }

  async function sendMessage(content, sessionId, attachments = []) {
    const requestChatMode = state.chatMode;
    setGenerating(true);
    state.autoFollow = true;
    scrollToLatest(true);
    const resp = await fetch('/api/chat/sse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
      body: JSON.stringify({
        session_id: sessionId,
        content,
        attachment_ids: attachments.map(item => item.asset_id),
        chat_mode: requestChatMode,
        nl2sql_options: getNL2SQLPayload(),
        llm_options: getModelOptionsPayload(),
      }),
    });

    if (!resp.ok || !resp.body) {
      setGenerating(false);
      let detail = '聊天请求失败';
      try {
        const errorData = await resp.json();
        detail = errorData.detail || detail;
      } catch (error) {
        console.error(error);
      }
      throw new Error(detail);
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    let receivedFirstChunk = false;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      if (!receivedFirstChunk) {
        receivedFirstChunk = true;
        if (state.thinkingBubble || currentRun) {
          showThinkingBubble('正在接收流式数据...');
        }
      }
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop() || '';
      for (const frame of frames) {
        const lines = frame.split(/\r?\n/).map(line => line.replace(/\r$/, '')).filter(Boolean);
        let eventName = 'message';
        const dataLines = [];
        for (const line of lines) {
          if (line.startsWith(':')) continue;
          if (line.startsWith('event:')) eventName = line.slice(6).trim();
          if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
        }
        if (!dataLines.length) continue;
        handleEvent(eventName, JSON.parse(dataLines.join('\n')));
      }
    }
    if (state.isGenerating) {
      setGenerating(false);
      currentRun = null;
      await refreshSessions();
    }
  }

  async function stopGeneration() {
    if (!state.isGenerating || !state.currentSessionId) return;
    await fetch('/api/chat/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: state.currentSessionId }),
    });
  }

  function bindUploadEvents() {
    chatUploadButton().addEventListener('click', () => {
      if (!state.currentSessionId || state.isGenerating) return;
      chatUploadInput().click();
    });

    const docManagementButton = () => document.getElementById('docManagementButton');
    if (docManagementButton()) {
      docManagementButton().addEventListener('click', () => {
        window.open('http://10.168.1.102:5601/app/discover', '_blank');
      });
    }


    chatUploadInput().addEventListener('change', async event => {
      const files = Array.from(event.target.files || []);
      if (!files.length) return;
      try {
        chatUploadButton().disabled = true;
        updateAttachmentHint('正在上传附件...');
        const items = await uploadChatFiles(files);
        appendDraftAttachments(items);
        hideUploadProgress();
        updateAttachmentHint();
      } catch (error) {
        console.error(error);
        hideUploadProgress();
        updateAttachmentHint(error.message || '文件上传失败');
      } finally {
        chatUploadButton().disabled = state.isGenerating || !currentSession();
        event.target.value = '';
      }
    });

    chatAttachmentLane().addEventListener('click', event => {
      const button = event.target.closest('[data-remove-attachment]');
      if (!button) return;
      removeDraftAttachment(button.dataset.removeAttachment);
      updateAttachmentHint();
    });
  }

  function bindModelSettingsEvents() {
    if (!modelSettingsToggle() || !modelSettingsDrawer()) return;
    [
      llmTemperature(),
      llmTopP(),
      llmTopK(),
      llmMinP(),
      llmPresencePenalty(),
      llmRepetitionPenalty(),
      llmEnableThinking(),
      llmPreserveThinking(),
      llmReasoningEffort(),
      llmThinkingTokenBudget(),
    ].filter(Boolean).forEach(node => {
      node.addEventListener('input', syncModelSettingFromInput);
      node.addEventListener('change', syncModelSettingFromInput);
    });

    modelSettingsToggle().addEventListener('click', () => setModelSettingsOpen(true));
    if (modelSettingsClose()) modelSettingsClose().addEventListener('click', () => setModelSettingsOpen(false));
    if (modelSettingsBackdrop()) modelSettingsBackdrop().addEventListener('click', () => setModelSettingsOpen(false));
    if (modelSettingsReset()) modelSettingsReset().addEventListener('click', () => {
      state.modelOptions = { ...DEFAULT_MODEL_OPTIONS };
      renderModelSettings();
      persistModelOptions();
    });
  }

  function bindNL2SQLEvents() {
    nl2sqlButton().addEventListener('click', event => {
      event.stopPropagation();
      const hidden = nl2sqlPopover().classList.contains('hidden');
      togglePopover(hidden);
    });
    nl2sqlPopoverClose().addEventListener('click', () => togglePopover(false));

    const dbManagerButton = () => document.getElementById('dbManagerButton');
    if (dbManagerButton()) {
      dbManagerButton().addEventListener('click', () => {
        window.open('/db-manager', '_blank');
      });
    }

    nl2sqlForceEnabled().addEventListener('change', event => {
      state.nl2sql.forceEnabled = !!event.target.checked;
      updateConfigHint();
    });
    nl2sqlAutoSelectTables().addEventListener('change', event => {
      state.nl2sql.autoSelectTables = !!event.target.checked;
      renderManualTableOptions();
      applyManualSectionState();
    });
    nl2sqlDbSelect().addEventListener('change', async event => {
      await selectDatabase(event.target.value, { reloadTables: true });
    });
    const openDatabaseDropdown = event => {
      event.preventDefault();
      event.stopPropagation();
      setDatabaseDropdownOpen(!state.dbDropdownOpen);
    };
    nl2sqlDbDropdownTrigger().addEventListener('pointerdown', openDatabaseDropdown);
    nl2sqlDbDropdownTrigger().addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
    });
    nl2sqlDbDropdown().addEventListener('pointerdown', event => {
      event.stopPropagation();
    });
    nl2sqlDbDropdownList().addEventListener('mouseover', event => {
      const option = event.target.closest('[data-database-id]');
      if (!option) return;
      state.hoverDatabaseId = option.dataset.databaseId || '';
      renderDatabaseHoverPanel();
    });
    nl2sqlDbDropdownList().addEventListener('focusin', event => {
      const option = event.target.closest('[data-database-id]');
      if (!option) return;
      state.hoverDatabaseId = option.dataset.databaseId || '';
      renderDatabaseHoverPanel();
    });
    nl2sqlDbDropdownList().addEventListener('click', async event => {
      const renameButton = event.target.closest('[data-rename-db]');
      if (renameButton) {
        event.stopPropagation();
        state.renamingDatabaseId = renameButton.dataset.renameDb || '';
        state.dbDropdownOpen = true;
        renderDatabaseSelect();
        window.requestAnimationFrame(() => {
          const inputNode = nl2sqlDbDropdownList().querySelector('.nl2sql-db-rename-input');
          if (inputNode) {
            inputNode.focus();
            inputNode.select();
          }
        });
        return;
      }

      const cancelButton = event.target.closest('[data-cancel-db-rename]');
      if (cancelButton) {
        event.stopPropagation();
        state.renamingDatabaseId = '';
        renderDatabaseSelect();
        return;
      }

      const saveButton = event.target.closest('[data-save-db]');
      if (saveButton) {
        event.stopPropagation();
        const form = saveButton.closest('.nl2sql-db-rename-form');
        const inputNode = form ? form.querySelector('.nl2sql-db-rename-input') : null;
        const nextName = inputNode ? inputNode.value.trim() : '';
        if (!nextName) {
          nl2sqlConfigHint().textContent = '数据库名称不能为空。';
          return;
        }
        try {
          saveButton.disabled = true;
          await renameDatabase(saveButton.dataset.saveDb || '', nextName);
        } catch (error) {
          console.error(error);
          nl2sqlConfigHint().textContent = error.message || '数据库重命名失败。';
        } finally {
          saveButton.disabled = false;
        }
        return;
      }

      if (event.target.closest('input, button')) return;
      const option = event.target.closest('[data-database-id]');
      if (!option) return;
      await selectDatabase(option.dataset.databaseId || 'default', { reloadTables: true });
      setDatabaseDropdownOpen(false);
    });
    nl2sqlManualList().addEventListener('change', handleManualListChange);
    nl2sqlDbUploadButton().addEventListener('click', () => nl2sqlDbFileInput().click());
    nl2sqlDbFileInput().addEventListener('change', async event => {
      const [file] = event.target.files || [];
      if (!file) return;
      try {
        nl2sqlDbUploadButton().disabled = true;
        await uploadSelectedDb(file);
      } catch (error) {
        console.error(error);
        nl2sqlManualList().innerHTML = `<div class="nl2sql-loading-text">${app.escapeHtml(error.message || '数据库上传失败。')}</div>`;
      } finally {
        nl2sqlDbUploadButton().disabled = false;
        event.target.value = '';
      }
    });
    nl2sqlDbResetButton().addEventListener('click', async () => {
      await resetSelectedDb();
    });
    placeBaselineInitButton().addEventListener('click', async event => {
      event.preventDefault();
      try {
        await initializePlaceBaseline();
      } catch (error) {
        console.error(error);
        placeBaselineHint().textContent = error.message || '初始化场所基线失败。';
      }
    });
    placeBaselineSaveButton().addEventListener('click', async event => {
      event.preventDefault();
      try {
        await savePlaceBaseline();
      } catch (error) {
        console.error(error);
        placeBaselineHint().textContent = error.message || '保存场所基线失败。';
      }
    });
    document.addEventListener('click', event => {
      if (!nl2sqlPopover().contains(event.target) && !nl2sqlButton().contains(event.target)) {
        togglePopover(false);
      }
    });
  }

  function bindSessionEvents() {
    newSessionBtn().addEventListener('click', async () => {
      try {
        closeSessionMenu();
        await createNewSession();
      } catch (error) {
        console.error(error);
      }
    });

    sessionList().addEventListener('click', async event => {
      const menuAction = event.target.closest('[data-session-action]');
      if (menuAction) {
        event.preventDefault();
        event.stopPropagation();
        const action = menuAction.dataset.sessionAction;
        const sessionId = menuAction.dataset.sessionId;
        if (sessionId && sessionId !== state.currentSessionId) {
          await switchSession(sessionId, { skipGeneratingCheck: true });
        }
        closeSessionMenu();
        try {
          if (action === 'rename') {
            await renameCurrentSession();
          } else if (action === 'delete') {
            await deleteCurrentSession();
          }
        } catch (error) {
          console.error(error);
          if (action === 'delete') {
            window.alert(error.message || '删除失败');
          }
        }
        return;
      }

      const menuTrigger = event.target.closest('[data-session-menu-trigger]');
      if (menuTrigger) {
        event.preventDefault();
        event.stopPropagation();
        if (state.isGenerating) return;
        toggleSessionMenu(menuTrigger.dataset.sessionMenuTrigger);
        return;
      }

      const popover = event.target.closest('[data-session-menu-popover]');
      if (popover) {
        event.stopPropagation();
        return;
      }

      const target = event.target.closest('[data-session-id]');
      if (!target || state.isGenerating) return;
      try {
        closeSessionMenu();
        await switchSession(target.dataset.sessionId);
      } catch (error) {
        console.error(error);
      }
    });

    document.addEventListener('click', event => {
      if (
        event.target.closest('[data-session-menu-popover]') ||
        event.target.closest('[data-session-menu-trigger]')
      ) {
        return;
      }
      closeSessionMenu();
    });
  }

  async function maybeStartAutoBaselineJudgement() {
    const params = new URLSearchParams(window.location.search || '');
    if (params.get('auto_judge') !== '1') return;
    const judgementId = params.get('judgement_id') || 'latest';
    const key = `deepem:auto-baseline-judge:${judgementId}`;
    if (window.sessionStorage.getItem(key)) return;
    window.sessionStorage.setItem(key, '1');
    if (!state.currentSessionId) {
      await createNewSession();
    } else {
      const created = await app.api('/api/chat/sessions', { method: 'POST', body: JSON.stringify({ title: '频谱基线研判' }) });
      if (created.item) {
        patchSession(created.item);
        await switchSession(created.item.id, { force: true, skipGeneratingCheck: true });
      }
    }
    const content = '固定频谱基线研判：请按固定流程执行，先调用 extract_baseline_spectrum_peaks，再调用 compare_spectrum_with_baseline。只依据这两个工具结果给出总结和研判依据，不调用其他工具。';
    showThinkingBubble('正在启动固定频谱基线研判...');
    try {
      await sendMessage(content, state.currentSessionId, []);
      const cleanUrl = window.location.pathname;
      window.history.replaceState({}, document.title, cleanUrl);
    } catch (error) {
      console.error(error);
      setGenerating(false);
      removeThinkingBubble();
      appendStep('error', `错误：${error.message || '自动研判启动失败'}`);
    }
  }

  document.addEventListener('DOMContentLoaded', async () => {
    loadChatMode();
    loadModelOptions();
    renderModelSettings();
    renderChatMode();
    nl2sqlForceEnabled().checked = state.nl2sql.forceEnabled;
    nl2sqlAutoSelectTables().checked = state.nl2sql.autoSelectTables;
    renderManualTableOptions();
    applyManualSectionState();
    updateConfigHint();
    bindUploadEvents();
    bindModelSettingsEvents();
    bindNL2SQLEvents();
    bindSessionEvents();
    chatModeSelector().addEventListener('click', event => {
      const button = event.target.closest('[data-chat-mode]');
      if (!button || state.isGenerating) return;
      setChatMode(button.dataset.chatMode);
    });
    timeline().addEventListener('scroll', () => {
      state.autoFollow = isNearBottom();
      updateBackToLatestButton();
    });
    timeline().addEventListener('wheel', event => {
      if (event.deltaY < 0) {
        state.autoFollow = false;
        updateBackToLatestButton();
      }
    }, { passive: true });
    backToLatestBtn().addEventListener('click', jumpToLatest);
    await initializeSessions();
    renderDraftAttachments();
    await maybeStartAutoBaselineJudgement();
    void (async () => {
      try {
        await loadDatabaseCatalog();
        await loadTablesIfNeeded(true);
        await loadPlaceBaseline(true);
      } catch (error) {
        console.error(error);
        updateConfigHint();
      }
    })();
    document.getElementById('chatComposer').addEventListener('submit', async event => {
      event.preventDefault();
      if (state.isGenerating) {
        try {
          await stopGeneration();
        } catch (error) {
          console.error(error);
        }
        return;
      }
      const content = input().value.trim();
      const attachments = [...currentDraftAttachments()];
      if ((!content && !attachments.length) || !state.currentSessionId) return;
      input().value = '';
      clearDraftAttachments(state.currentSessionId);
      try {
        await sendMessage(content, state.currentSessionId, attachments);
        updateAttachmentHint();
      } catch (error) {
        console.error(error);
        appendDraftAttachments(attachments, state.currentSessionId);
        setGenerating(false);
        removeThinkingBubble();
        appendStep('error', `错误：${error.message || '发送失败'}`);
        updateAttachmentHint(error.message || '发送失败');
      }
    });
  });
})();
