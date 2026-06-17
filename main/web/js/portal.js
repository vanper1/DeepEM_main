
(function () {
  const $ = (selector, scope = document) => scope.querySelector(selector);
  const $$ = (selector, scope = document) => Array.from(scope.querySelectorAll(selector));

  const tasks = [
    { id: 'TASK-20260602-006', time: '2026-06-02 13:20', name: '园区北楼电磁环境复测', place: '北楼 4F 机房', status: '完成-无异常', operator: 'operator' },
    { id: 'TASK-20260602-005', time: '2026-06-02 11:15', name: '实验室异常 Wi-Fi 指纹排查', place: '信工所 A 座', status: '完成-2个异常', operator: 'operator' },
    { id: 'TASK-20260601-014', time: '2026-06-01 17:42', name: '会议室蓝牙 Beacon 巡检', place: '会议中心 2F', status: '中止-无异常', operator: 'operator' },
    { id: 'TASK-20260531-009', time: '2026-05-31 09:10', name: '重点区域频谱基线采集', place: '地下设备间', status: '完成-1个异常', operator: 'operator' }
  ];

  const templates = [
    { id: 'TPL-001', time: '2026-06-02 09:00', name: '室内 Wi-Fi / 蓝牙异常检测模板', scene: '办公区、会议室、实验室', operator: 'operator', body: '采集 2.4GHz / 5GHz 频段，自动识别未知 AP、蓝牙 Beacon 与异常功率峰值。' },
    { id: 'TPL-002', time: '2026-05-29 15:30', name: 'USRP 宽带频谱巡检模板', scene: '机房、设备间、园区边界', operator: 'operator', body: '配置 USRP 频点扫描、切片上传、频谱峰值筛查与智能体研判流程。' },
    { id: 'TPL-003', time: '2026-05-26 10:20', name: '重点场所基线复核模板', scene: '涉密会议、重点实验', operator: 'operator', body: '对比场所基线和历史指纹，生成异常列表与复核建议。' }
  ];

  const statusClass = (status) => status.includes('无异常') ? 'ok' : (status.includes('中止') ? 'stop' : 'warn');
  const safe = (v) => String(v ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));

  function setupLogin() {
    const btn = $('#loginButton');
    if (!btn) return;
    btn.addEventListener('click', () => { window.location.href = '/home'; });
    $$('#loginForm input').forEach(input => {
      input.addEventListener('keydown', event => {
        if (event.key === 'Enter') {
          event.preventDefault();
          window.location.href = '/home';
        }
      });
    });
  }

  function setupTaskList() {
    const table = $('#taskTableBody');
    const detail = $('#taskDetail');
    if (!table || !detail) return;
    let activeId = tasks[0].id;
    let batch = false;
    let rows = tasks.slice();

    function renderList() {
      table.innerHTML = rows.map(task => `
        <tr data-id="${safe(task.id)}" class="${task.id === activeId ? 'active' : ''}">
          ${batch ? `<td><input type="checkbox" class="task-check" data-id="${safe(task.id)}"></td>` : ''}
          <td>${safe(task.time)}</td><td>${safe(task.name)}</td><td>${safe(task.place)}</td>
          <td><span class="status-pill ${statusClass(task.status)}">${safe(task.status)}</span></td><td>${safe(task.operator)}</td>
        </tr>`).join('');
      const head = $('#taskTableHead');
      if (head) head.innerHTML = `${batch ? '<th>选择</th>' : ''}<th>任务时间</th><th>任务名称</th><th>任务地点</th><th>任务状态</th><th>操作员</th>`;
    }
    function renderDetail() {
      const task = tasks.find(item => item.id === activeId) || rows[0] || tasks[0];
      activeId = task.id;
      detail.innerHTML = `
        <div class="deem-kv">
          <div><span>任务名称</span><strong>${safe(task.name)}</strong></div>
          <div><span>任务ID</span><strong>${safe(task.id)}</strong></div>
          <div><span>任务时间</span><strong>${safe(task.time)}</strong></div>
          <div><span>任务地点</span><strong>${safe(task.place)}</strong></div>
          <div><span>任务状态</span><strong>${safe(task.status)}</strong></div>
          <div><span>操作员</span><strong>${safe(task.operator)}</strong></div>
        </div>
        <div class="deem-report-box">
          <strong>任务操作流程列表及步骤数据</strong><br>
          1. 设备资源校验：USRP-01 在线，采样配置正常。<br>
          2. 频谱切片采集：已生成 128 个切片文件。<br>
          3. 智能体研判：基线比对完成，异常详情可在执行页查看。<br>
          4. 报告生成：形成当前任务的阅览区与复核入口。
        </div>
        <div class="deem-form-actions"><a class="deem-btn primary" href="/detect/run?task=${encodeURIComponent(task.id)}">进入执行研判页</a></div>`;
    }
    table.addEventListener('click', event => {
      if (event.target.matches('input')) return;
      const tr = event.target.closest('tr[data-id]');
      if (!tr) return;
      activeId = tr.dataset.id;
      renderList(); renderDetail();
    });
    $('#taskSearchBtn')?.addEventListener('click', () => {
      const q = ($('#taskSearchInput')?.value || '').trim();
      rows = tasks.filter(t => !q || [t.id, t.name, t.place, t.status, t.operator].join(' ').includes(q));
      if (!rows.find(t => t.id === activeId) && rows[0]) activeId = rows[0].id;
      renderList(); renderDetail();
    });
    $('#taskFilterBtn')?.addEventListener('click', () => {
      const keyword = prompt('输入筛选状态关键词，例如：完成、中止、异常、无异常', '完成') || '';
      rows = tasks.filter(t => !keyword.trim() || t.status.includes(keyword.trim()) || t.name.includes(keyword.trim()) || t.place.includes(keyword.trim()));
      if (!rows.find(t => t.id === activeId) && rows[0]) activeId = rows[0].id;
      renderList(); renderDetail();
    });
    $('#taskBatchBtn')?.addEventListener('click', () => { batch = !batch; renderList(); });
    $('#taskDeleteBtn')?.addEventListener('click', () => {
      const selected = $$('.task-check:checked').map(cb => cb.dataset.id);
      if (!selected.length) { alert('请先勾选要删除的任务。'); return; }
      rows = rows.filter(t => !selected.includes(t.id));
      alert(`已模拟删除 ${selected.length} 个任务，后续可接入真实后端。`);
      if (!rows.find(t => t.id === activeId) && rows[0]) activeId = rows[0].id;
      renderList(); renderDetail();
    });
    renderList(); renderDetail();
  }

  function setupNewTask() {
    const templatePane = $('#templatePane');
    const commandPane = $('#commandPane');
    if (!templatePane && !commandPane) return;
    const renderTemplates = () => {
      if (templatePane) {
        templatePane.innerHTML = templates.map((tpl, idx) => `<div class="deem-list-item ${idx === 0 ? 'active' : ''}" data-template="${safe(tpl.id)}"><strong>${safe(tpl.name)}</strong><span>${safe(tpl.scene)}</span><br><span>${safe(tpl.body)}</span></div>`).join('');
      }
    };
    function updateMode() {
      const mode = $('input[name="taskMode"]:checked')?.value || 'template';
      if (templatePane) templatePane.style.display = mode === 'template' ? 'grid' : 'none';
      if (commandPane) commandPane.style.display = mode === 'command' ? 'block' : 'none';
    }
    $$('input[name="taskMode"]').forEach(input => input.addEventListener('change', updateMode));
    $('#createTaskBtn')?.addEventListener('click', () => {
      const name = ($('#taskName')?.value || '新建智能检测任务').trim();
      window.location.href = `/detect/verify?name=${encodeURIComponent(name)}`;
    });
    renderTemplates(); updateMode();
  }

  function setupVerify() {
    const title = $('#verifyTaskName');
    if (!title) return;
    const params = new URLSearchParams(location.search);
    const name = params.get('name') || '新建任务';
    title.textContent = `${name} 任务校验`;
    $('#verifyResourceText') && ($('#verifyResourceText').textContent = `任务执行需要 USRP-01、频谱分析服务、知识库基线资源，当前均已通过校验。`);
    $('#startDetectBtn')?.addEventListener('click', () => { window.location.href = `/detect/run?task=${encodeURIComponent(name)}`; });
  }

  function setupTemplates() {
    const table = $('#templateTableBody');
    const detail = $('#templateDetail');
    if (!table || !detail) return;
    let activeId = templates[0].id;
    let rows = templates.slice();
    function render() {
      table.innerHTML = rows.map(tpl => `<tr data-id="${safe(tpl.id)}" class="${tpl.id === activeId ? 'active' : ''}"><td>${safe(tpl.time)}</td><td>${safe(tpl.name)}</td><td>${safe(tpl.scene)}</td><td>${safe(tpl.operator)}</td></tr>`).join('');
      const tpl = templates.find(t => t.id === activeId) || rows[0] || templates[0];
      detail.innerHTML = `<div class="deem-kv"><div><span>模板名称</span><strong>${safe(tpl.name)}</strong></div><div><span>操作员</span><strong>${safe(tpl.operator)}</strong></div><div><span>适用场景</span><strong>${safe(tpl.scene)}</strong></div><div><span>创建时间</span><strong>${safe(tpl.time)}</strong></div></div><div class="deem-report-box"><strong>检测模板内容阅览区</strong><br>${safe(tpl.body)}<br><br>流程：资源校验 → 参数下发 → 采集切片 → 智能研判 → 报告归档。</div>`;
    }
    table.addEventListener('click', e => { const tr = e.target.closest('tr[data-id]'); if (tr) { activeId = tr.dataset.id; render(); } });
    $('#templateSearchBtn')?.addEventListener('click', () => {
      const q = ($('#templateSearchInput')?.value || '').trim();
      rows = templates.filter(t => !q || [t.name, t.scene, t.operator].join(' ').includes(q));
      if (!rows.find(t => t.id === activeId) && rows[0]) activeId = rows[0].id;
      render();
    });
    $('#templateFilterBtn')?.addEventListener('click', () => {
      const q = prompt('输入场景关键词，例如：会议室、机房、办公区', '机房') || '';
      rows = templates.filter(t => !q.trim() || t.scene.includes(q.trim()) || t.name.includes(q.trim()));
      render();
    });
    render();
  }

  function setupDevicePage() {
    if (!$('#devicePage')) return;

    async function api(path, options = {}) {
      const headers = new Headers(options.headers || {});
      if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
      const resp = await fetch(path, { ...options, headers });
      const contentType = resp.headers.get('content-type') || '';
      const payload = contentType.includes('application/json') ? await resp.json().catch(() => ({})) : await resp.text();
      if (!resp.ok) throw new Error((payload && payload.detail) || (payload && payload.message) || payload || `请求失败：${resp.status}`);
      return payload;
    }

    $('#scanDeviceBtn')?.addEventListener('click', async () => {
      const resultEl = $('#deviceScanResult');
      if (resultEl) resultEl.textContent = '正在扫描真实设备...';
      try {
        const result = await api('/api/devices/scan', { method: 'POST' });
        const devices = result.devices || [];
        if (!devices.length) {
          if (resultEl) resultEl.textContent = '未发现设备。';
          return;
        }
        if (resultEl) resultEl.textContent = `已扫描到 ${devices.length} 台设备：` + devices.map(d => `${safe(d.dev_id || '-')} ${safe(d.status || 'UNKNOWN')}`).join('、');
      } catch (error) {
        if (resultEl) resultEl.textContent = '真实扫描失败，保留模拟展示：已扫描到 3 台 USRP：USRP-01 在线、USRP-02 空闲、USRP-03 离线。' + (error.message ? `（${error.message}）` : '');
      }
    });
    $('#configDeviceBtn')?.addEventListener('click', async () => {
      const resultEl = $('#deviceActionResult');
      try {
        await api('/api/devices/params', { method: 'POST', body: JSON.stringify({}) });
        if (resultEl) resultEl.textContent = '参数已下发到后端配置接口。';
      } catch (error) {
        if (resultEl) resultEl.textContent = '参数下发失败，已保留前端配置展示。' + (error.message ? `（${error.message}）` : '');
      }
    });
    $('#startCaptureBtn')?.addEventListener('click', async () => {
      const resultEl = $('#deviceActionResult');
      try {
        await api('/api/control/start', { method: 'POST', body: '{}' });
        if (resultEl) resultEl.textContent = '采集已启动。';
      } catch (error) {
        if (resultEl) resultEl.textContent = '采集启动失败：' + (error.message || '网络错误');
      }
    });
    $('#stopDeviceBtn')?.addEventListener('click', async () => {
      const resultEl = $('#deviceActionResult');
      try {
        await api('/api/control/stop', { method: 'POST', body: '{}' });
        if (resultEl) resultEl.textContent = '采集已停止。';
      } catch (error) {
        if (resultEl) resultEl.textContent = '停止失败：' + (error.message || '网络错误');
      }
    });
  }

  function setupAlgorithmPage() {
    if (!$('#algoTree')) return;
    $('#algoTree').addEventListener('click', e => {
      const li = e.target.closest('li[data-name]');
      if (!li) return;
      $$('#algoTree li').forEach(node => node.classList.remove('active'));
      li.classList.add('active');
      $('#algoDetail').innerHTML = `<div class="deem-kv"><div><span>类别/算法</span><strong>${safe(li.dataset.name)}</strong></div><div><span>状态</span><strong>可用</strong></div><div><span>适用场景</span><strong>智能电磁检测</strong></div><div><span>操作员</span><strong>operator</strong></div></div><div class="deem-report-box">展示选定类别或算法的详细信息。当前为前端模拟逻辑，后续可接入算法管理后端接口。</div>`;
    });
    $('#newCategoryBtn')?.addEventListener('click', () => {
      const name = prompt('请输入新类别名称', '新增算法类别');
      if (name) alert(`已模拟新增类别：${name}`);
    });
  }

  function setupGenericForms() {
    $('#createTemplateBtn')?.addEventListener('click', () => { alert('检测模板已模拟保存。'); window.location.href = '/templates'; });
    $('#createAlgorithmBtn')?.addEventListener('click', () => { alert('算法已模拟新增。'); window.location.href = '/algorithms'; });
  }

  document.addEventListener('DOMContentLoaded', () => {
    setupLogin();
    setupTaskList();
    setupNewTask();
    setupVerify();
    setupTemplates();
    setupDevicePage();
    setupAlgorithmPage();
    setupGenericForms();
  });
})();
