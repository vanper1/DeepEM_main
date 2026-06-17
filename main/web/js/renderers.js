(function () {
  const app = window.DeepEMApp;
  const { escapeHtml, formatTime, formatDateTime, statusToClass } = app;

  app.renderTimelineDetailLines = function renderTimelineDetailLines(details = []) {
    if (!details.length) return '<div class="detail-empty">暂无更多细节。</div>';
    return details.map(line => `
      <div class="detail-line">
        <strong>${escapeHtml(line.kind_label || line.kind || '片段')}</strong>
        <div>${escapeHtml(line.content)}</div>
      </div>`).join('');
  };

  app.renderNormalEvidence = function renderNormalEvidence(evidence = []) {
    if (!evidence.length) return '<div class="detail-empty">暂无正常依据。</div>';
    return evidence.map(line => `<div class="evidence-line">${escapeHtml(line)}</div>`).join('');
  };

  app.renderSignalHistoryTable = function renderSignalHistoryTable(items = []) {
    if (!items.length) return '<div class="empty-state">暂无历史信号。</div>';
    return `
      <div class="table-wrap history-table-wrap">
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>信号 ID</th>
              <th>通道</th>
              <th>评分</th>
              <th>分类</th>
              <th>指纹</th>
              <th>承载信息</th>
            </tr>
          </thead>
          <tbody>
            ${items.slice().reverse().map(item => `
              <tr class="${escapeHtml(item.classification)}">
                <td>${formatDateTime(item.timestamp)}</td>
                <td>${escapeHtml(item.signal_id || '-')}</td>
                <td>${escapeHtml(item.channel ?? '-')}</td>
                <td>${escapeHtml(item.score ?? '-')}</td>
                <td>${escapeHtml(item.classification_label || item.classification || '-')}</td>
                <td>${escapeHtml(item.fingerprint || '-')}</td>
                <td>${escapeHtml(item.carries_information ? '是' : '否')}</td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
  };

  app.renderAnomalyHistoryList = function renderAnomalyHistoryList(items = []) {
    if (!items.length) return '<div class="empty-state">暂无异常信号。</div>';
    return `
      <div class="history-list">
        ${items.map((item, index) => `
          <button class="history-insight-card" data-history-insight-index="${index}" type="button">
            <div class="insight-top-row">
              <strong>${escapeHtml(item.title)}</strong>
              <span class="tag ${statusToClass(item.status || '待研判')}">${escapeHtml(item.status || '待研判')}</span>
            </div>
            <div class="muted">${formatDateTime(item.timestamp)} · 指纹 ${escapeHtml(item.fingerprint)}</div>
            <div class="insight-basic">分类：${escapeHtml(item.classification || '-')} ｜ 风险：${escapeHtml(item.risk_level || '-')}</div>
            <div class="insight-result"><strong>最终判定：</strong>${escapeHtml(item.final_result || '-')}</div>
            <div class="muted nested-tip">点击查看原有检测过程检测结果</div>
          </button>`).join('')}
      </div>`;
  };

  app.render = function render(snapshot) {
    const taskIdEl = document.getElementById('taskId');
    if (taskIdEl) taskIdEl.textContent = snapshot.task_id;
    const tickCountEl = document.getElementById('tickCount');
    if (tickCountEl) tickCountEl.textContent = `文件 ${snapshot.tick}`;
    const statusLampEl = document.getElementById('statusLamp');
    if (statusLampEl) statusLampEl.className = `lamp ${snapshot.streaming ? 'on' : 'off'}`;

    const activeSession = snapshot.collector?.active_session || null;
    let statusText = 'collector_agent 未触发';
    if (activeSession) {
      const base = `会话 ${activeSession.session_id || '-'}`;
      const isDemoSource = activeSession.source_mode === 'demo';
      const sourceModeLabel = isDemoSource ? '模拟采集' : 'USRP 真实采集';
      const receivedCount = Number(activeSession.files_received || 0);
      const plan = activeSession.acquisition_plan || [];
      const planIndex = Number(activeSession.current_plan_index ?? -1) + 1;
      const expectedCount = activeSession.expected_file_count ? Number(activeSession.expected_file_count) : null;
      const progressText = plan.length ? `（频点 ${Math.max(planIndex, 0)}/${plan.length}，文件 ${receivedCount}）` : (expectedCount ? `（${receivedCount}/${expectedCount}）` : `（文件 ${receivedCount}）`);
      if (activeSession.status === 'pending') statusText = `${base} ${isDemoSource ? '待 collector_agent 拉取' : '等待启动下一频点'}（${sourceModeLabel}）`;
      else if (activeSession.status === 'starting') statusText = `${base} 正在调用 ${isDemoSource ? '模拟采集' : 'USRP'}${progressText}`;
      else if (activeSession.status === 'running') statusText = `${base} ${sourceModeLabel}中${progressText}`;
      else if (activeSession.status === 'completed') statusText = `${base} ${sourceModeLabel}完成`;
      else statusText = `${base} 状态 ${activeSession.status}`;
    }
    const statusTextEl = document.getElementById('statusText');
    if (statusTextEl) statusTextEl.textContent = statusText;

    const toggleBtn = document.getElementById('toggleStream');
    const demoBtn = document.getElementById('toggleDemoStream');
    const stopBtn = document.getElementById('stopCapture');
    const activeStatus = activeSession?.status || null;
    const sourceMode = activeSession?.source_mode || null;
    const aiReview = snapshot.ai_review || {};
    const queuedReviews = Number(aiReview.queued || snapshot.queues?.detection || 0);
    const runningReviews = Number(aiReview.running || 0);
    const reviewBusy = queuedReviews > 0 || runningReviews > 0;
    const isBusy = Boolean(snapshot.streaming);

    if (toggleBtn && app.state.startingCapture) {
      toggleBtn.textContent = '启动中...';
      toggleBtn.className = 'btn btn-stop';
      toggleBtn.disabled = true;
    } else if (toggleBtn && isBusy) {
      toggleBtn.textContent = sourceMode === 'demo' ? '模拟采集中...' : 'USRP 采集中...';
      toggleBtn.className = 'btn btn-stop';
      toggleBtn.disabled = true;
    } else if (toggleBtn && activeStatus === 'completed') {
      toggleBtn.textContent = '开始下一轮采集';
      toggleBtn.className = 'btn btn-start';
      toggleBtn.disabled = false;
    } else if (toggleBtn) {
      toggleBtn.textContent = '启动采集';
      toggleBtn.className = 'btn btn-start';
      toggleBtn.disabled = false;
    }



    if (demoBtn) {
      if (app.state.startingDemoCapture) {
        demoBtn.textContent = '启动中...';
        demoBtn.className = 'btn btn-stop';
        demoBtn.disabled = true;
      } else if (isBusy) {
        demoBtn.textContent = sourceMode === 'demo' ? '模拟采集中...' : '模拟采集';
        demoBtn.className = sourceMode === 'demo' ? 'btn btn-stop' : 'btn btn-start';
        demoBtn.disabled = true;
      } else if (activeStatus === 'completed' && sourceMode === 'demo') {
        demoBtn.textContent = '开始下一轮模拟采集';
        demoBtn.className = 'btn btn-start';
        demoBtn.disabled = false;
      } else {
        demoBtn.textContent = '模拟采集';
        demoBtn.className = 'btn btn-start';
        demoBtn.disabled = false;
      }
    }

    if (stopBtn) {
      stopBtn.className = 'btn btn-stop';
      if (app.state.stoppingCapture) {
        stopBtn.textContent = '停止中...';
        stopBtn.disabled = true;
      } else {
        stopBtn.textContent = !isBusy && reviewBusy ? '停止研判' : '停止采集';
        stopBtn.disabled = !(isBusy || reviewBusy);
      }
    }

    const latestBatch = snapshot.realtime.latest_batch || {};
    const anomalyLabel = latestBatch.has_anomaly === undefined ? '待接收' : (latestBatch.has_anomaly ? '是' : '否');
    const completedReviews = Number(aiReview.completed || 0);
    const totalReviews = Number(aiReview.total || 0);
    let aiReviewLabel = '待异常触发';
    if (runningReviews > 0 || queuedReviews > 0) {
      aiReviewLabel = `处理中 ${runningReviews} / 排队 ${queuedReviews}`;
    } else if (totalReviews > 0) {
      aiReviewLabel = `已完成 ${completedReviews}/${totalReviews}`;
    }
    document.getElementById('batchInfo').innerHTML = `
      <div><strong>样本数</strong><br>${escapeHtml(latestBatch.raw_sample_count || '-')}</div>
      <div><strong>检测到告警</strong><br>${escapeHtml(anomalyLabel)}</div>
      <div><strong>大模型研判</strong><br>${escapeHtml(aiReviewLabel)}</div>`;

    if (typeof app.syncUsrpWebSocket === 'function') app.syncUsrpWebSocket(snapshot);
    const specForDisplay = app.state.liveFftSpec || snapshot.realtime.recent_spectrogram;
    app.renderSpectrogram(specForDisplay);

    app.state.allSignalHistory = snapshot.realtime.all_observations || [];
    const totalSignalCount = snapshot.realtime.total_received_signals ?? app.state.allSignalHistory.length;
    document.getElementById('signalResultTitle').textContent = `已接收信号数量：${totalSignalCount}`;

    document.getElementById('obsTable').innerHTML = (snapshot.realtime.recent_observations || []).slice().reverse().map(item => `
      <tr class="${escapeHtml(item.classification)}">
        <td>${formatTime(item.timestamp)}</td>
        <td>${escapeHtml(item.channel ?? '-')}</td>
        <td>${escapeHtml(item.score ?? '-')}</td>
        <td>${escapeHtml(item.classification_label || item.classification)}</td>
        <td>${escapeHtml(item.fingerprint)}</td>
        <td>${escapeHtml(item.carries_information ? '是' : '否')}</td>
      </tr>`).join('');

    const timelineEl = document.getElementById('timeline');
    const timelineItems = (snapshot.timeline || []).slice().reverse();
    timelineEl.innerHTML = timelineItems.map((item, index) => `
      <div class="timeline-item ${escapeHtml(item.level)} ${item.expandable ? 'clickable' : ''}" data-timeline-index="${index}" data-expandable="${item.expandable ? 'true' : 'false'}" data-key="${escapeHtml(item.timestamp)}_${escapeHtml(item.agent)}">
        <div class="timeline-meta">${formatTime(item.timestamp)} · ${escapeHtml(item.event_type_label || item.event_type)}</div>
        <div class="timeline-title-row">
          <div class="title">${escapeHtml(item.agent)}</div>
          ${item.expandable ? '<span class="expand-hint">点击展开</span>' : ''}
        </div>
        <div>${escapeHtml(item.summary)}</div>
        ${item.expandable ? `
          <div class="timeline-evidence hidden">
            <div class="section-title">${item.details && item.details.length ? '过程详情' : '判定证据'}</div>
            ${item.details && item.details.length ? app.renderTimelineDetailLines(item.details) : app.renderNormalEvidence(item.normal_evidence || [])}
          </div>` : ''}
        ${!item.expandable && item.details && item.details.length ? `
          <details class="timeline-details">
            <summary>查看智能体/工具过程</summary>
            ${app.renderTimelineDetailLines(item.details)}
          </details>` : ''}
      </div>`).join('');

    timelineEl.querySelectorAll('.timeline-item.clickable').forEach(card => {
      const key = card.dataset.key;
      if (app.state.openTimelineIndexes.has(key)) {
        const panel = card.querySelector('.timeline-evidence');
        if (panel) {
          panel.classList.remove('hidden');
          card.classList.add('open');
        }
      }
    });

    app.state.latestInsights = snapshot.anomaly_insights || [];
    app.state.allAnomalyInsights = snapshot.all_anomaly_insights || app.state.latestInsights;
    document.getElementById('agentInsights').innerHTML = app.state.latestInsights.length ? app.state.latestInsights.map((item, index) => `
      <button class="insight-card" data-insight-index="${index}" type="button">
        <div class="insight-top-row">
          <strong>${escapeHtml(item.title)}</strong>
          <span class="tag ${statusToClass(item.status)}">${escapeHtml(item.status)}</span>
        </div>
        <div class="muted">${formatDateTime(item.timestamp)} · 指纹 ${escapeHtml(item.fingerprint)}</div>
        <div class="insight-basic">分类：${escapeHtml(item.classification)} ｜ 风险：${escapeHtml(item.risk_level)}</div>
        <div class="insight-result"><strong>最终判定：</strong>${escapeHtml(item.final_result)}</div>
      </button>`).join('') : '<div class="empty-state">暂无异常事件</div>';


    const caseArea = document.getElementById('caseDetails');
    caseArea.innerHTML = snapshot.cases.length ? snapshot.cases.map(item => `
      <div class="case-card">
        <div><strong>${escapeHtml(item.signal_id)}</strong> · 风险 ${escapeHtml(item.risk_level)}</div>
        <div>假设：${escapeHtml(item.hypothesis || '-')}</div>
        <div>状态：${escapeHtml(item.status)}</div>
        <div>备注：${escapeHtml((item.notes || []).join('；') || '-')}</div>
        <div>下一步：${escapeHtml((item.next_actions || []).join('，') || '-')}</div>
      </div>`).join('') : '暂无 Case';

    document.getElementById('logs').textContent = (snapshot.logs || []).join('\n');
    window.lastSnapshot = snapshot;
  };
})();
