(function () {
  const app = window.DeepEMApp;

  var STATUS_COLORS = {
    OFFLINE: { color: '#cf1322', label: '离线', cssClass: 'dev-status-offline' },
    IDLE: { color: '#52c41a', label: '在线空闲', cssClass: 'dev-status-idle' },
    BUSY: { color: '#1890ff', label: '采集中', cssClass: 'dev-status-busy' },
    ERROR: { color: '#d48806', label: '异常', cssClass: 'dev-status-error' },
    UNKNOWN: { color: '#8c8c8c', label: '未知', cssClass: 'dev-status-unknown' }
  };

  function setValue(id, value) {
    var el = document.getElementById(id);
    if (el && value !== undefined && value !== null) el.value = value;
  }

  function getValue(id) {
    var el = document.getElementById(id);
    return el ? el.value : '';
  }

  function openOverlay() {
    var overlay = document.getElementById('deviceMgmtOverlay');
    if (!overlay) return;
    overlay.classList.remove('hidden');
    overlay.setAttribute('aria-hidden', 'false');
    loadParams();
    scanDevices();
  }

  function closeOverlay() {
    var overlay = document.getElementById('deviceMgmtOverlay');
    if (!overlay) return;
    overlay.classList.add('hidden');
    overlay.setAttribute('aria-hidden', 'true');
  }

  function loadParams() {
    app.api('/api/devices/params').then(function (params) {
      setValue('paramDevId', params.dev_id || '');
      setValue('paramFreq', params.freq || 2400);
      setValue('paramChannel', params.channel !== undefined ? params.channel : 0);
      setValue('paramAntenna', params.antenna || 'RX2');
      setValue('paramSampleRate', params.sample_rate || 1);
      setValue('paramBandwidth', params.bandwidth || 1);
      setValue('paramGain', params.gain || 40);
      setValue('paramSliceDuration', params.slice_duration || 1);
      setValue('paramDuration', params.duration === null ? '' : (params.duration || 3));
    }).catch(function (err) {
      console.error('Failed to load device params:', err);
    });
  }

  function scanDevices() {
    var listEl = document.getElementById('deviceStatusList');
    var scanBtn = document.getElementById('scanDevicesBtn');
    var oldErr = document.getElementById('deviceScanError');
    if (oldErr) oldErr.remove();
    if (!listEl) return;
    listEl.innerHTML = '<div class="device-mgmt-loading">正在扫描设备...</div>';
    if (scanBtn) {
      scanBtn.disabled = true;
      scanBtn.textContent = '扫描中...';
    }

    app.api('/api/devices/scan', { method: 'POST' }).then(function (result) {
      if (scanBtn) {
        scanBtn.disabled = false;
        scanBtn.textContent = '扫描设备';
      }
      var devices = result.devices || [];
      if (result.scan_error) {
        var errEl = document.getElementById('deviceScanError');
        if (!errEl) {
          errEl = document.createElement('div');
          errEl.id = 'deviceScanError';
          errEl.className = 'device-mgmt-warning';
          listEl.parentNode.insertBefore(errEl, listEl);
        }
        errEl.textContent = '扫描警告：' + result.scan_error;
      }
      if (!devices.length) {
        listEl.innerHTML = '<div class="device-mgmt-empty">未发现设备</div>';
        return;
      }
      renderDeviceList(devices);
    }).catch(function (err) {
      if (scanBtn) {
        scanBtn.disabled = false;
        scanBtn.textContent = '扫描设备';
      }
      listEl.innerHTML = '<div class="device-mgmt-error">扫描失败：' + app.escapeHtml(err.message || '网络错误') + '</div>';
    });
  }

  function renderRange(label, range, suffix) {
    if (!range || range.min === undefined || range.max === undefined) return '';
    return '<div class="device-info-row"><span class="info-label">' + label + '：</span><span>' + app.escapeHtml(range.min) + ' ~ ' + app.escapeHtml(range.max) + (suffix || '') + '</span></div>';
  }

  function renderDeviceList(devices) {
    var listEl = document.getElementById('deviceStatusList');
    var html = '';
    for (var i = 0; i < devices.length; i++) {
      var device = devices[i];
      var status = (device.status || 'UNKNOWN').toUpperCase();
      var sc = STATUS_COLORS[status] || STATUS_COLORS.UNKNOWN;
      var devId = app.escapeHtml(device.dev_id || '-');
      var devIp = app.escapeHtml(device.dev_ip || '-');
      var taskId = app.escapeHtml(device.task_id || '-');
      var updatedAt = app.escapeHtml(device.updated_at || '-');
      var devConfig = device.dev_config || {};
      var currentConfig = device.current_config || {};

      html += '<div class="device-item">';
      html += '  <div class="device-item-header">';
      html += '    <span class="device-status-indicator ' + sc.cssClass + '" title="' + sc.label + '"></span>';
      html += '    <span class="device-name">' + devId + '</span>';
      html += '    <span class="device-status-tag" style="color:' + sc.color + ';border-color:' + sc.color + '">' + sc.label + '</span>';
      if (device.dev_id) html += '    <button type="button" class="btn btn-detail use-device-btn" data-dev-id="' + app.escapeHtml(device.dev_id) + '">选择此设备</button>';
      html += '  </div>';
      html += '  <div class="device-item-info">';
      html += '    <div class="device-info-row"><span class="info-label">IP 地址：</span><span>' + devIp + '</span></div>';
      html += '    <div class="device-info-row"><span class="info-label">连接状态：</span><span>' + sc.label + '</span></div>';
      html += '    <div class="device-info-row"><span class="info-label">工作状态：</span><span>' + (status === 'BUSY' ? '采集中' : status === 'IDLE' ? '空闲' : status === 'ERROR' ? '异常' : '离线') + '</span></div>';
      html += '    <div class="device-info-row"><span class="info-label">任务ID：</span><span>' + taskId + '</span></div>';
      html += '    <div class="device-info-row"><span class="info-label">更新时间：</span><span>' + updatedAt + '</span></div>';
      html += renderRange('频率范围', devConfig.freq_range, ' MHz');
      html += renderRange('采样率范围', devConfig.sample_rate_range, ' MHz');
      html += renderRange('带宽范围', devConfig.bandwidth_range, ' MHz');
      html += renderRange('增益范围', devConfig.gain_range, ' dB');
      if (Array.isArray(devConfig.rx_antennas) && devConfig.rx_antennas.length) {
        html += '    <div class="device-info-row"><span class="info-label">可用天线：</span><span>' + app.escapeHtml(devConfig.rx_antennas.join(', ')) + '</span></div>';
      }
      if (currentConfig && Object.keys(currentConfig).length) {
        html += '    <div class="device-info-row"><span class="info-label">当前配置：</span><span class="device-config-json">' + app.escapeHtml(JSON.stringify(currentConfig)) + '</span></div>';
      }
      html += '  </div>';
      html += '</div>';
    }
    listEl.innerHTML = html;
  }

  function saveParams() {
    var msgEl = document.getElementById('deviceParamsMsg');
    var payload = {
      dev_id: getValue('paramDevId').trim() || undefined,
      freq: parseFloat(getValue('paramFreq')) || undefined,
      channel: parseInt(getValue('paramChannel'), 10),
      antenna: getValue('paramAntenna') || undefined,
      sample_rate: parseFloat(getValue('paramSampleRate')) || undefined,
      bandwidth: parseFloat(getValue('paramBandwidth')) || undefined,
      gain: parseFloat(getValue('paramGain')),
      slice_duration: parseFloat(getValue('paramSliceDuration')) || undefined,
      duration: getValue('paramDuration') === '' ? null : parseFloat(getValue('paramDuration'))
    };

    var cleaned = {};
    ['freq', 'sample_rate', 'bandwidth', 'slice_duration'].forEach(function (key) {
      if (payload[key] !== undefined && !isNaN(payload[key]) && payload[key] > 0) cleaned[key] = payload[key];
    });
    if (payload.gain !== undefined && !isNaN(payload.gain) && payload.gain >= 0) cleaned.gain = payload.gain;
    if (payload.duration === null || (!isNaN(payload.duration) && payload.duration > 0)) cleaned.duration = payload.duration;
    if (payload.channel !== undefined && !isNaN(payload.channel)) cleaned.channel = payload.channel;
    if (payload.antenna !== undefined && payload.antenna !== '') cleaned.antenna = payload.antenna;
    if (payload.dev_id !== undefined && payload.dev_id !== '') cleaned.dev_id = payload.dev_id;

    app.api('/api/devices/params', { method: 'POST', body: JSON.stringify(cleaned) }).then(function (saved) {
      app.state.deviceParams = saved;
      if (saved.dev_id) setValue('paramDevId', saved.dev_id);
      if (saved.configure_error) {
        msgEl.textContent = '参数已本地保存；configure 校验未通过：' + saved.configure_error;
        msgEl.className = 'device-params-msg error';
      } else {
        msgEl.textContent = '参数已保存，并已调用 USRP configure 校验通过';
        msgEl.className = 'device-params-msg success';
      }
      setTimeout(function () { msgEl.textContent = ''; }, 5000);
    }).catch(function (err) {
      msgEl.textContent = '保存失败：' + (err.message || '网络错误');
      msgEl.className = 'device-params-msg error';
    });
  }

  app.openDeviceMgmt = openOverlay;
  app.closeDeviceMgmt = closeOverlay;
  app.scanDevices = scanDevices;
  app.saveDeviceParams = saveParams;
  app.loadDeviceParams = loadParams;

  app.loadDeviceParamsToState = function () {
    app.api('/api/devices/params').then(function (params) {
      app.state.deviceParams = {
        dev_id: params.dev_id || undefined,
        freq: params.freq || 2400,
        channel: params.channel !== undefined ? params.channel : 0,
        antenna: params.antenna || 'RX2',
        sample_rate: params.sample_rate || 1,
        bandwidth: params.bandwidth || 1,
        gain: params.gain || 40,
        slice_duration: params.slice_duration || 1,
        duration: params.duration === undefined ? 3 : params.duration
      };
    }).catch(function () {
      app.state.deviceParams = {
        freq: 2400,
        channel: 0,
        antenna: 'RX2',
        sample_rate: 1000000,
        bandwidth: 1000000,
        gain: 40,
        slice_duration: 1,
        duration: 3
      };
    });
  };

  app.bindDeviceMgmtEvents = function () {
    var on = function (id, eventName, handler) {
      var el = document.getElementById(id);
      if (el) el.addEventListener(eventName, handler);
    };

    on('deviceMgmtBtn', 'click', openOverlay);
    on('closeDeviceMgmt', 'click', closeOverlay);
    on('scanDevicesBtn', 'click', scanDevices);
    on('saveDeviceParams', 'click', saveParams);

    var listEl = document.getElementById('deviceStatusList');
    if (listEl) {
      listEl.addEventListener('click', function (event) {
        var btn = event.target.closest('.use-device-btn');
        if (!btn) return;
        setValue('paramDevId', btn.dataset.devId || '');
        var msgEl = document.getElementById('deviceParamsMsg');
        if (msgEl) {
          msgEl.textContent = '已选择设备：' + (btn.dataset.devId || '');
          msgEl.className = 'device-params-msg success';
        }
      });
    }

    var backdrop = document.querySelector('#deviceMgmtOverlay .device-mgmt-backdrop');
    if (backdrop) backdrop.addEventListener('click', closeOverlay);

    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') return;
      var overlay = document.getElementById('deviceMgmtOverlay');
      if (overlay && !overlay.classList.contains('hidden')) {
        closeOverlay();
      }
    });
  };
})();
