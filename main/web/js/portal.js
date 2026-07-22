
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

  const DETECT_TASK_CONTEXT_KEY = 'deepem.detect.taskContext';
  const CAPTURE_PENDING_KEY = 'deepem.captureAgent.pendingFromDetect';

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    let body = options.body;
    if (body && !(body instanceof FormData) && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
      body = JSON.stringify(body);
    }
    const resp = await fetch(path, { ...options, headers, body });
    const contentType = resp.headers.get('content-type') || '';
    const payload = contentType.includes('application/json') ? await resp.json().catch(() => ({})) : await resp.text();
    if (!resp.ok) throw new Error((payload && (payload.detail || payload.message)) || payload || `请求失败：${resp.status}`);
    return payload;
  }

  function formatMhz(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return '-';
    const mhz = Math.abs(num) > 100000 ? num / 1000000 : num;
    return `${mhz.toLocaleString('zh-CN', { maximumFractionDigits: 6 })} MHz`;
  }

  function rangeText(range, unit = 'MHz') {
    if (!range || range.min === undefined || range.max === undefined) return '-';
    const min = unit === 'MHz' ? formatMhz(range.min) : `${safe(range.min)} ${unit}`;
    const max = unit === 'MHz' ? formatMhz(range.max) : `${safe(range.max)} ${unit}`;
    return `${min} ~ ${max}`;
  }

  function setupNewTask() {
    const templatePane = $('#templatePane');
    const commandPane = $('#commandPane');
    if (!templatePane && !commandPane) return;

    const pageState = { templates: [], selectedTemplateIds: new Set(), selectedTaskId: '' };
    const taskPicker = $('#existingTaskPicker');
    if (taskPicker) {
      taskPicker.innerHTML = '<option value="">新建智能检测任务</option>' + tasks.map(task => `<option value="${safe(task.id)}">${safe(task.time)} · ${safe(task.name)}</option>`).join('');
      taskPicker.addEventListener('change', () => {
        pageState.selectedTaskId = taskPicker.value;
        const task = tasks.find(item => item.id === pageState.selectedTaskId);
        if (task) {
          $('#taskName') && ($('#taskName').value = task.name);
          $('#taskPlace') && ($('#taskPlace').value = task.place);
          $('#taskOperator') && ($('#taskOperator').value = task.operator);
          $('#selectedTaskInfo') && ($('#selectedTaskInfo').innerHTML = `<strong>已选择历史任务：</strong>${safe(task.name)}<br>状态：${safe(task.status)}；地点：${safe(task.place)}。`);
        } else {
          $('#taskName') && ($('#taskName').value = '园区电磁环境智能检测');
          $('#taskPlace') && ($('#taskPlace').value = '中国科学院信息工程研究所园区');
          $('#taskOperator') && ($('#taskOperator').value = 'operator');
          $('#selectedTaskInfo') && ($('#selectedTaskInfo').textContent = '当前为新建任务。可直接选择模板或输入任务指令。');
        }
      });
    }

    function renderTemplates() {
      if (!templatePane) return;
      if (!pageState.templates.length) {
        templatePane.innerHTML = '<div class="deem-list-item">暂无已上传模板，请上传 DOCX 或切换到任务指令模式。</div>';
        return;
      }
      templatePane.innerHTML = pageState.templates.map((tpl) => {
        const checked = pageState.selectedTemplateIds.has(tpl.id);
        return `<label class="deem-list-item detect-template-item ${checked ? 'active' : ''}">
          <input type="checkbox" value="${safe(tpl.id)}" ${checked ? 'checked' : ''}>
          <strong>${safe(tpl.file_name || tpl.name || tpl.id)}</strong>
          <span>${safe(tpl.updated_at || tpl.created_at || '')}</span><br>
          <span>${safe((tpl.markdown || tpl.text || '').slice(0, 90))}</span>
        </label>`;
      }).join('');
      templatePane.querySelectorAll('input[type="checkbox"]').forEach(input => {
        input.addEventListener('change', () => {
          if (input.checked) pageState.selectedTemplateIds.add(input.value); else pageState.selectedTemplateIds.delete(input.value);
          renderTemplates();
        });
      });
    }

    async function loadTemplates(defaultSelect = false) {
      if (templatePane) templatePane.innerHTML = '<div class="deem-list-item">正在读取采集智能体模板……</div>';
      try {
        const payload = await api('/api/capture-agent/templates');
        pageState.templates = payload.items || [];
        if (defaultSelect && !pageState.selectedTemplateIds.size && pageState.templates[0]) pageState.selectedTemplateIds.add(pageState.templates[0].id);
        renderTemplates();
      } catch (error) {
        if (templatePane) templatePane.innerHTML = `<div class="deem-list-item">模板读取失败：${safe(error.message || '网络错误')}</div>`;
      }
    }

    async function uploadTemplates(files) {
      if (!files || !files.length) return;
      const invalid = Array.from(files).find(file => !file.name.toLowerCase().endsWith('.docx'));
      if (invalid) { alert('仅支持上传 DOCX 模板。'); return; }
      const form = new FormData();
      Array.from(files).forEach(file => form.append('files', file));
      const status = $('#templateUploadStatus');
      if (status) status.textContent = `正在上传 ${files.length} 个模板……`;
      try {
        const payload = await api('/api/capture-agent/templates', { method: 'POST', body: form });
        (payload.items || []).forEach(item => pageState.selectedTemplateIds.add(item.id));
        if (status) status.textContent = `已上传：${(payload.items || []).map(item => item.file_name).join('、')}`;
        await loadTemplates(false);
      } catch (error) {
        if (status) status.textContent = `上传失败：${error.message || '网络错误'}`;
      } finally {
        const upload = $('#captureTemplateUpload');
        if (upload) upload.value = '';
      }
    }

    function updateMode() {
      const mode = $('input[name="taskMode"]:checked')?.value || 'template';
      if (templatePane) templatePane.style.display = mode === 'template' ? 'grid' : 'grid';
      if (commandPane) commandPane.style.display = mode === 'command' ? 'block' : 'none';
    }
    $$('input[name="taskMode"]').forEach(input => input.addEventListener('change', updateMode));
    $('#refreshTemplateBtn')?.addEventListener('click', () => loadTemplates(false));
    $('#captureTemplateUpload')?.addEventListener('change', event => uploadTemplates(event.target.files));
    $('#createTaskBtn')?.addEventListener('click', () => {
      const mode = $('input[name="taskMode"]:checked')?.value || 'template';
      const selectedTemplates = pageState.templates.filter(tpl => pageState.selectedTemplateIds.has(tpl.id));
      const rawInstruction = ($('#taskInstruction')?.value || '').trim();
      if (mode === 'template' && !selectedTemplates.length) { alert('请先选择或上传一个模板，或切换到任务指令模式。'); return; }
      if (mode === 'command' && !rawInstruction) { alert('请输入任务指令。'); return; }
      const name = ($('#taskName')?.value || '新建智能检测任务').trim();
      const place = ($('#taskPlace')?.value || '').trim();
      const operator = ($('#taskOperator')?.value || '').trim();
      const instruction = rawInstruction || `根据选定模板执行“${name}”，地点：${place}。请结合后续设备状态、设备能力与参数范围，生成 MHz 参数下的采集计划。`;
      const context = {
        selected_task_id: pageState.selectedTaskId,
        name, place, operator, mode, instruction,
        template_ids: selectedTemplates.map(tpl => tpl.id),
        template_names: selectedTemplates.map(tpl => tpl.file_name || tpl.name || tpl.id),
        created_at: new Date().toISOString(),
      };
      localStorage.setItem(DETECT_TASK_CONTEXT_KEY, JSON.stringify(context));
      window.location.href = `/detect/verify?name=${encodeURIComponent(name)}`;
    });
    loadTemplates(true);
    updateMode();
  }

  function setupVerify() {
    const title = $('#verifyTaskName');
    if (!title) return;
    const params = new URLSearchParams(location.search);
    const stored = localStorage.getItem(DETECT_TASK_CONTEXT_KEY);
    const taskContext = stored ? JSON.parse(stored) : { name: params.get('name') || '新建任务', instruction: '', template_ids: [], template_names: [] };
    const name = params.get('name') || taskContext.name || '新建任务';
    title.textContent = '调用设备平台接口';

    const summaryEl = $('#deviceStatusSummary');
    const listEl = $('#deviceListPane');
    const paramEl = $('#deviceParamPane');
    const startBtn = $('#startDetectBtn');
    const readyTitle = $('#deviceReadyTitle');
    const readyText = $('#deviceReadyText');
    let selectedDevice = null;
    let devicesPayload = null;
    let paramsPayload = null;

    function renderParams(paramsData) {
      if (!paramEl) return;
      if (!paramsData || !Object.keys(paramsData).length) {
        paramEl.textContent = '尚未读取到当前设备参数。';
        return;
      }
      paramEl.innerHTML = `<strong>当前平台参数（MHz）：</strong><br>
        设备：${safe(paramsData.dev_id || '-')}；中心频率：${formatMhz(paramsData.freq)}；采样率：${formatMhz(paramsData.sample_rate)}；带宽：${formatMhz(paramsData.bandwidth)}；增益：${safe(paramsData.gain ?? '-')} dB；天线：${safe(paramsData.antenna || '-')}`;
    }

    function renderDevices(devices, errorText = '') {
      if (!listEl) return;
      if (!devices.length) {
        listEl.innerHTML = `<div class="deem-report-box compact">未获取到设备。${errorText ? `错误：${safe(errorText)}` : ''}</div>`;
        if (summaryEl) summaryEl.textContent = '设备平台接口已调用，但当前没有可选设备。';
        return;
      }
      if (summaryEl) summaryEl.textContent = `已获取 ${devices.length} 台设备的状态与能力范围，请选择执行设备。${errorText ? `接口提示：${errorText}` : ''}`;
      listEl.innerHTML = devices.map((device, index) => {
        const cfg = device.dev_config || {};
        const status = String(device.status || 'UNKNOWN').toUpperCase();
        return `<label class="detect-device-card ${index === 0 ? 'active' : ''}">
          <input type="radio" name="selectedDetectDevice" value="${safe(device.dev_id || '')}" ${index === 0 ? 'checked' : ''}>
          <div class="detect-device-head"><strong>${safe(device.dev_id || '未命名设备')}</strong><span>${safe(status)}</span></div>
          <div class="detect-device-meta">IP：${safe(device.dev_ip || '-')}；任务：${safe(device.task_id || '-')}；更新时间：${safe(device.updated_at || '-')}</div>
          <div class="detect-device-range"><b>频率范围</b>${rangeText(cfg.freq_range)}</div>
          <div class="detect-device-range"><b>采样率范围</b>${rangeText(cfg.sample_rate_range)}</div>
          <div class="detect-device-range"><b>带宽范围</b>${rangeText(cfg.bandwidth_range)}</div>
          <div class="detect-device-range"><b>增益范围</b>${rangeText(cfg.gain_range, 'dB')}</div>
          ${Array.isArray(cfg.rx_antennas) ? `<div class="detect-device-range"><b>天线</b>${safe(cfg.rx_antennas.join('、'))}</div>` : ''}
        </label>`;
      }).join('');
      selectedDevice = devices[0];
      if (startBtn) startBtn.disabled = !selectedDevice;
      if (readyTitle) readyTitle.textContent = selectedDevice ? '设备已选择' : '等待选择设备';
      if (readyText && selectedDevice) readyText.textContent = `已选择 ${selectedDevice.dev_id || '设备'}。点击开始执行后将进入采集智能体并自动生成采集计划。`;
      listEl.querySelectorAll('input[name="selectedDetectDevice"]').forEach(input => {
        input.addEventListener('change', () => {
          selectedDevice = devices.find(device => String(device.dev_id || '') === input.value) || null;
          listEl.querySelectorAll('.detect-device-card').forEach(card => card.classList.remove('active'));
          input.closest('.detect-device-card')?.classList.add('active');
          if (startBtn) startBtn.disabled = !selectedDevice;
          if (readyTitle) readyTitle.textContent = selectedDevice ? '设备已选择' : '等待选择设备';
          if (readyText && selectedDevice) readyText.textContent = `已选择 ${selectedDevice.dev_id || '设备'}。点击开始执行后将进入采集智能体并自动生成采集计划。`;
        });
      });
    }

    async function loadDevicePlatform() {
      try {
        const [scanResult, paramsResult] = await Promise.all([
          api('/api/devices/scan', { method: 'POST' }).catch(async error => {
            const statusResult = await api('/api/devices/status').catch(() => ({ devices: [], error: error.message }));
            return { ...statusResult, scan_error: error.message };
          }),
          api('/api/devices/params').catch(() => ({})),
        ]);
        devicesPayload = scanResult;
        paramsPayload = paramsResult;
        renderParams(paramsResult);
        renderDevices(scanResult.devices || [], scanResult.scan_error || scanResult.error || '');
      } catch (error) {
        if (summaryEl) summaryEl.textContent = `调用设备平台接口失败：${error.message || '网络错误'}`;
        if (listEl) listEl.innerHTML = '<div class="deem-report-box compact">无法读取设备状态与能力，请检查后端设备接口。</div>';
      }
    }

    startBtn?.addEventListener('click', async () => {
      if (!selectedDevice) { alert('请先选择设备。'); return; }
      const cfg = selectedDevice.dev_config || {};
      const deviceBrief = {
        dev_id: selectedDevice.dev_id,
        status: selectedDevice.status,
        dev_ip: selectedDevice.dev_ip,
        capability_mhz: {
          freq_range: cfg.freq_range || null,
          sample_rate_range: cfg.sample_rate_range || null,
          bandwidth_range: cfg.bandwidth_range || null,
          gain_range: cfg.gain_range || null,
          rx_antennas: cfg.rx_antennas || null,
        },
        current_config_mhz: selectedDevice.current_config || {},
        platform_params_mhz: paramsPayload || {},
      };
      try {
        await api('/api/devices/params', { method: 'POST', body: { dev_id: selectedDevice.dev_id } });
      } catch (_) { /* 跳转前尽力保存设备选择，不阻断采集智能体生成计划 */ }
      const templateLine = (taskContext.template_names || []).length ? `已选择模板：${taskContext.template_names.join('、')}。` : '未选择模板。';
      const instruction = [
        `智能检测任务：${name}`,
        taskContext.place ? `任务地点：${taskContext.place}` : '',
        taskContext.operator ? `操作员：${taskContext.operator}` : '',
        templateLine,
        `用户任务指令：${taskContext.instruction || '根据任务模板完成园区电磁环境智能检测。'}`,
        '设备平台接口已返回当前设备状态、能力与参数范围；请全部按 MHz 理解与生成采集参数。',
        `选择设备与能力数据：${JSON.stringify(deviceBrief, null, 2)}`,
        '请结合用户意图或模板、当前设备状态、设备能力与参数范围生成采集计划，等待用户确认任务计划后再执行信号采集。',
      ].filter(Boolean).join('\n');
      localStorage.setItem(CAPTURE_PENDING_KEY, JSON.stringify({
        source: 'detect_flow',
        task_name: name,
        template_ids: taskContext.template_ids || [],
        instruction,
        device: deviceBrief,
        devices_payload: devicesPayload,
        created_at: new Date().toISOString(),
      }));
      window.location.href = '/capture-agent?from=detect';
    });

    loadDevicePlatform();
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
