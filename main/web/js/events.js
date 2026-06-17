(function () {
  const app = window.DeepEMApp;

  app.refresh = async function refresh() {
    try {
      app.render(await app.api('/api/snapshot'));
    } catch (error) {
      console.error(error);
    }
  };

  app.startReviewProcess = function startReviewProcess() {
    const signalId = document.getElementById('caseReviewModal').dataset.signalId;
    if (!signalId) return;

    const expertInfo = document.getElementById('expertInfoInput').value.trim();
    const selectedTools = Array.from(document.querySelectorAll('#toolSelection input[name="tool"]:checked')).map(cb => cb.value);
    if (!expertInfo) {
      alert('请输入专家信息');
      return;
    }
    if (!selectedTools.length) {
      alert('请至少选择一个复核工具');
      return;
    }

    const progressDiv = document.getElementById('reviewProgress');
    progressDiv.innerHTML = '<div>复核已启动...</div>';
    const logEntries = ['复核已启动...'];

    setTimeout(() => {
      const msg = '智能体思考中：分析信号特征...';
      progressDiv.innerHTML += '<div>' + msg + '</div>';
      logEntries.push(msg);
    }, 1000);
    setTimeout(() => {
      const msg1 = '调用工具：频谱分析仪...';
      const msg2 = '工具结果：已关联当前 NPZ 的时频热图与自动研判结论。';
      progressDiv.innerHTML += '<div>' + msg1 + '</div><div>' + msg2 + '</div>';
      logEntries.push(msg1, msg2);
    }, 3000);
    setTimeout(() => {
      const msg1 = '调用工具：信号解码器...';
      const msg2 = '工具结果：当前示例流程使用占位工具，已跑通任务链路。';
      progressDiv.innerHTML += '<div>' + msg1 + '</div><div>' + msg2 + '</div>';
      logEntries.push(msg1, msg2);
    }, 5000);
    setTimeout(() => {
      const msg = '智能体思考中：综合工具结果...';
      progressDiv.innerHTML += '<div>' + msg + '</div>';
      logEntries.push(msg);
    }, 7000);
    setTimeout(() => {
      const msg = '复核完成：已保留自动结论，并等待人工进一步确认。';
      progressDiv.innerHTML += '<div>' + msg + '</div>';
      logEntries.push(msg);
      const startBtn = document.getElementById('startReviewBtn');
      if (startBtn) {
        startBtn.textContent = '复核已完成';
        startBtn.disabled = true;
        startBtn.classList.remove('btn-start');
        startBtn.classList.add('btn-stop');
      }
      window.reviewStatus[signalId] = {
        expertInfo,
        selectedTools,
        progressLog: logEntries.map(entry => '<div>' + app.escapeHtml(entry) + '</div>').join(''),
      };
    }, 9000);
  };

  app.bindEvents = function bindEvents() {
    const byId = id => document.getElementById(id);
    const on = (id, eventName, handler) => {
      const element = byId(id);
      if (element) element.addEventListener(eventName, handler);
    };
    const onQuery = (selector, eventName, handler) => {
      const element = document.querySelector(selector);
      if (element) element.addEventListener(eventName, handler);
    };

    on('toggleStream', 'click', async () => {
      const btn = document.getElementById('toggleStream');
      if (!btn || btn.disabled || app.state.startingCapture || app.state.startingDemoCapture || app.state.stoppingCapture) return;
      app.state.startingCapture = true;
      btn.disabled = true;
      btn.textContent = '启动中...';
      try {
        var body = '{}';
        if (app.state.deviceParams && Object.keys(app.state.deviceParams).length) {
          body = JSON.stringify(app.state.deviceParams);
        }
        await app.api('/api/control/start', { method: 'POST', body: body });
      } catch (error) {
        alert(error.message || 'USRP 采集启动失败');
      } finally {
        app.state.startingCapture = false;
        app.refresh();
      }
    });



    on('toggleDemoStream', 'click', async () => {
      const btn = document.getElementById('toggleDemoStream');
      if (!btn || btn.disabled || app.state.startingCapture || app.state.startingDemoCapture || app.state.stoppingCapture) return;
      app.state.startingDemoCapture = true;
      btn.disabled = true;
      btn.textContent = '启动中...';
      try {
        await app.api('/api/control/start-demo', { method: 'POST', body: JSON.stringify({ force: true }) });
      } catch (error) {
        alert(error.message || '模拟采集启动失败');
      } finally {
        app.state.startingDemoCapture = false;
        app.refresh();
      }
    });

    on('stopCapture', 'click', async () => {
      const btn = document.getElementById('stopCapture');
      if (!btn || btn.disabled || app.state.stoppingCapture) return;
      app.state.stoppingCapture = true;
      btn.disabled = true;
      btn.textContent = '停止中...';
      try {
        await app.api('/api/control/stop', { method: 'POST', body: '{}' });
      } catch (error) {
        alert(error.message || '停止采集失败');
      } finally {
        app.state.stoppingCapture = false;
        app.refresh();
      }
    });

    on('resetPlatform', 'click', async () => {
      try {
        await app.api('/api/control/reset', { method: 'POST', body: '{}' });
      } catch (error) {
        alert(error.message || '初始化平台失败');
      } finally {
        app.refresh();
      }
    });

    on('toggleLogs', 'click', () => {
      byId('logPanel')?.classList.toggle('hidden');
    });

    on('chatLauncher', 'click', app.openChat);
    on('closeModal', 'click', app.closeInsightModal);
    on('closeListModal', 'click', app.closeListModal);
    onQuery('#detailModal .modal-backdrop', 'click', app.closeInsightModal);
    onQuery('#listModal .modal-backdrop', 'click', app.closeListModal);
    on('showSignalHistory', 'click', app.openSignalHistoryModal);
    on('showAllAnomalies', 'click', app.openAllAnomaliesModal);
    on('showCaseList', 'click', app.openCaseListModal);
    on('closeCaseListModal', 'click', app.closeCaseListModal);
    onQuery('#caseListModal .modal-backdrop', 'click', app.closeCaseListModal);
    on('closeCaseReviewModal', 'click', app.closeCaseReviewModal);
    onQuery('#caseReviewModal .modal-backdrop', 'click', app.closeCaseReviewModal);

    on('timeline', 'click', event => {
      const card = event.target.closest('.timeline-item.clickable');
      if (!card || card.dataset.expandable !== 'true') return;
      if (event.target.closest('details')) return;
      const panel = card.querySelector('.timeline-evidence');
      if (!panel) return;
      const isOpening = panel.classList.contains('hidden');
      panel.classList.toggle('hidden');
      card.classList.toggle('open');
      const key = card.dataset.key;
      if (!key) return;
      if (isOpening) app.state.openTimelineIndexes.add(key);
      else app.state.openTimelineIndexes.delete(key);
    });

    on('agentInsights', 'click', event => {
      const card = event.target.closest('.insight-card');
      if (!card) return;
      app.openInsightModal(Number(card.dataset.insightIndex));
    });

    on('listModalBody', 'click', event => {
      const anomalyCard = event.target.closest('.history-insight-card');
      if (!anomalyCard) return;
      app.openInsightModal(Number(anomalyCard.dataset.historyInsightIndex), 'all');
    });

    on('caseListModalBody', 'click', event => {
      const reviewBtn = event.target.closest('.enter-review-btn');
      if (!reviewBtn) return;
      app.closeCaseListModal();
      app.openCaseReviewModal(reviewBtn.dataset.caseSignalId);
    });

    on('caseReviewModalBody', 'click', event => {
      if (event.target.id === 'startReviewBtn') app.startReviewProcess();
    });

    document.addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      if (byId('detailModal') && !byId('detailModal').classList.contains('hidden')) return app.closeInsightModal();
      if (byId('listModal') && !byId('listModal').classList.contains('hidden')) return app.closeListModal();
      if (byId('caseListModal') && !byId('caseListModal').classList.contains('hidden')) return app.closeCaseListModal();
      if (byId('caseReviewModal') && !byId('caseReviewModal').classList.contains('hidden')) app.closeCaseReviewModal();
    });

    if (typeof app.bindDeviceMgmtEvents === 'function') app.bindDeviceMgmtEvents();
  };
})();
