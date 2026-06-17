(function () {
  const app = window.DeepEMApp;
  const { escapeHtml, formatDateTime, renderSignalHistoryTable, renderAnomalyHistoryList, renderTimelineDetailLines } = app;

  app.openListModal = function openListModal({ title, meta = '', bodyHtml = '' }) {
    document.getElementById('listModalTitle').textContent = title;
    document.getElementById('listModalMeta').textContent = meta;
    document.getElementById('listModalBody').innerHTML = bodyHtml;
    document.getElementById('listModal').classList.remove('hidden');
    document.getElementById('listModal').setAttribute('aria-hidden', 'false');
  };

  app.closeListModal = function closeListModal() {
    document.getElementById('listModal').classList.add('hidden');
    document.getElementById('listModal').setAttribute('aria-hidden', 'true');
  };

  app.openSignalHistoryModal = function openSignalHistoryModal() {
    app.openListModal({
      title: '全部历史信号',
      meta: `累计接收 ${app.state.allSignalHistory.length} 条信号记录`,
      bodyHtml: renderSignalHistoryTable(app.state.allSignalHistory),
    });
  };

  app.openAllAnomaliesModal = function openAllAnomaliesModal() {
    app.openListModal({
      title: '全部异常信号',
      meta: `累计异常信号 ${app.state.allAnomalyInsights.length} 条`,
      bodyHtml: renderAnomalyHistoryList(app.state.allAnomalyInsights),
    });
  };

  app.openCaseListModal = function openCaseListModal() {
    const cases = window.lastSnapshot?.cases || [];
    document.getElementById('caseListModalTitle').textContent = '所有 Case 列表';
    document.getElementById('caseListModalMeta').textContent = `共 ${cases.length} 个 Case`;
    const bodyHtml = cases.length ? cases.map(caseItem => {
      const reviewed = window.reviewStatus[caseItem.signal_id];
      const buttonText = reviewed ? '复核已完成' : '进入复核';
      const buttonClass = reviewed ? 'btn-stop' : 'btn-detail';
      return `
        <div class="case-card case-list-card">
          <div><strong>${escapeHtml(caseItem.signal_id)}</strong> · 风险 ${escapeHtml(caseItem.risk_level)}</div>
          <div>假设：${escapeHtml(caseItem.hypothesis || '-')}</div>
          <div>状态：${escapeHtml(caseItem.status)}</div>
          <div>备注：${escapeHtml((caseItem.notes || []).join('；') || '-')}</div>
          <div>下一步：${escapeHtml((caseItem.next_actions || []).join('，') || '-')}</div>
          <div class="case-list-action-row">
            <button class="btn ${buttonClass} enter-review-btn" data-case-signal-id="${escapeHtml(caseItem.signal_id)}" type="button">${buttonText}</button>
          </div>
        </div>`;
    }).join('') : '<div class="empty-state">暂无 Case</div>';
    document.getElementById('caseListModalBody').innerHTML = bodyHtml;
    document.getElementById('caseListModal').classList.remove('hidden');
    document.getElementById('caseListModal').setAttribute('aria-hidden', 'false');
  };

  app.closeCaseListModal = function closeCaseListModal() {
    document.getElementById('caseListModal').classList.add('hidden');
    document.getElementById('caseListModal').setAttribute('aria-hidden', 'true');
  };

  app.openCaseReviewModal = function openCaseReviewModal(signalId) {
    const cases = window.lastSnapshot?.cases || [];
    const caseObj = cases.find(c => c.signal_id === signalId);
    if (!caseObj) return;

    const modal = document.getElementById('caseReviewModal');
    modal.dataset.signalId = signalId;
    document.getElementById('caseReviewModalTitle').textContent = `Case 复核 - ${escapeHtml(signalId)}`;
    document.getElementById('caseReviewModalMeta').textContent = `风险 ${escapeHtml(caseObj.risk_level)} · 状态 ${escapeHtml(caseObj.status)}`;

    const review = window.reviewStatus[signalId];
    document.getElementById('caseReviewModalBody').innerHTML = review ? `
      <div class="modal-section">
        <div><strong>信号 ID：</strong>${escapeHtml(caseObj.signal_id)}</div>
        <div><strong>风险等级：</strong>${escapeHtml(caseObj.risk_level)}</div>
        <div><strong>状态：</strong>${escapeHtml(caseObj.status)}</div>
        <div><strong>假设：</strong>${escapeHtml(caseObj.hypothesis || '-')}</div>
        <div><strong>备注：</strong>${escapeHtml((caseObj.notes || []).join('；') || '-')}</div>
        <div><strong>下一步：</strong>${escapeHtml((caseObj.next_actions || []).join('，') || '-')}</div>
      </div>
      <div class="modal-section">
        <div class="section-title">人工补充专家信息</div>
        <div>${escapeHtml(review.expertInfo)}</div>
      </div>
      <div class="modal-section">
        <div class="section-title">使用的复核工具</div>
        <div>${review.selectedTools.map(tool => escapeHtml(tool)).join('、')}</div>
      </div>
      <div class="modal-section">
        <div class="section-title">复核流程</div>
        <div id="reviewProgress" class="review-progress-box">${review.progressLog}</div>
      </div>` : `
      <div class="modal-section">
        <div><strong>信号 ID：</strong>${escapeHtml(caseObj.signal_id)}</div>
        <div><strong>风险等级：</strong>${escapeHtml(caseObj.risk_level)}</div>
        <div><strong>状态：</strong>${escapeHtml(caseObj.status)}</div>
        <div><strong>假设：</strong>${escapeHtml(caseObj.hypothesis || '-')}</div>
        <div><strong>备注：</strong>${escapeHtml((caseObj.notes || []).join('；') || '-')}</div>
        <div><strong>下一步：</strong>${escapeHtml((caseObj.next_actions || []).join('，') || '-')}</div>
      </div>
      <div class="modal-section">
        <div class="section-title">人工补充专家信息</div>
        <textarea id="expertInfoInput" class="full-width-textarea" placeholder="请输入用于评判异常的专家信息..." rows="3"></textarea>
      </div>
      <div class="modal-section">
        <div class="section-title">选择复核工具</div>
        <div id="toolSelection" class="tool-selection-list">
          <label><input type="checkbox" name="tool" value="spectrum_analyzer" checked> 频谱分析仪</label>
          <label><input type="checkbox" name="tool" value="signal_decoder"> 信号解码器</label>
          <label><input type="checkbox" name="tool" value="anomaly_detector"> 异常检测器</label>
          <label><input type="checkbox" name="tool" value="baseline_comparator"> 基线比对工具</label>
          <label><input type="checkbox" name="tool" value="threat_intelligence"> 威胁情报查询</label>
        </div>
      </div>
      <div class="modal-section">
        <button id="startReviewBtn" class="btn btn-start" type="button">开始复核</button>
      </div>
      <div class="modal-section">
        <div class="section-title">复核流程</div>
        <div id="reviewProgress" class="review-progress-box">复核尚未开始...</div>
      </div>`;

    modal.classList.remove('hidden');
    modal.setAttribute('aria-hidden', 'false');
  };

  app.closeCaseReviewModal = function closeCaseReviewModal() {
    document.getElementById('caseReviewModal').classList.add('hidden');
    document.getElementById('caseReviewModal').setAttribute('aria-hidden', 'true');
  };

  app.openInsightModal = function openInsightModal(index, source = 'latest') {
    const collection = source === 'all' ? app.state.allAnomalyInsights : app.state.latestInsights;
    const item = collection[index];
    if (!item) return;
    document.getElementById('modalTitle').textContent = item.title;
    document.getElementById('modalMeta').textContent = `${formatDateTime(item.timestamp)} · ${item.fingerprint}`;
    document.getElementById('modalBody').innerHTML = `
      <div class="modal-section">
        <div><strong>异常摘要：</strong>${escapeHtml(item.summary || '-')}</div>
        <div><strong>最终判定：</strong>${escapeHtml(item.final_result || '-')}</div>
        <div><strong>Case 状态：</strong>${escapeHtml(item.status || '-')}</div>
        <div><strong>研判假设：</strong>${escapeHtml(item.hypothesis || '-')}</div>
        <div><strong>分析评分：</strong>${escapeHtml(item.analysis_score ?? '-')}</div>
      </div>
      <div class="modal-section">
        <div class="section-title">原有检测过程检测结果</div>
        ${renderTimelineDetailLines(item.steps || [])}
      </div>`;
    document.getElementById('detailModal').classList.remove('hidden');
    document.getElementById('detailModal').setAttribute('aria-hidden', 'false');
  };

  app.closeInsightModal = function closeInsightModal() {
    document.getElementById('detailModal').classList.add('hidden');
    document.getElementById('detailModal').setAttribute('aria-hidden', 'true');
  };

  app.openChat = function openChat() {
    window.location.href = '/chat';
  };
})();
