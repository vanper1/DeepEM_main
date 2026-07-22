(function () {
  const PAGE_SIZE = 200;

  const dbList = () => document.getElementById('dbManagerList');
  const dbStatus = () => document.getElementById('dbManagerStatus');
  const panelHeader = () => document.getElementById('dbManagerPanelHeader');
  const tableInfo = () => document.getElementById('dbManagerTableInfo');
  const tableContainer = () => document.getElementById('dbManagerTableContainer');
  const pagination = () => document.getElementById('dbManagerPagination');

  const state = {
    databases: [],
    expandedDbId: null,
    selectedDbId: null,
    selectedTable: null,
    currentPage: 0,
    totalRows: 0,
  };

  function formatTime(iso) {
    if (!iso) return '-';
    try {
      const d = new Date(iso);
      const pad = (n) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    } catch {
      return iso;
    }
  }

  async function loadDatabases() {
    dbStatus().textContent = '正在加载...';
    try {
      const resp = await fetch('/api/nl2sql/databases');
      if (!resp.ok) throw new Error('加载失败');
      const data = await resp.json();
      state.databases = data.items || [];
      dbStatus().textContent = `共 ${state.databases.length} 个数据库`;
      renderDatabaseList();
    } catch (err) {
      console.error(err);
      dbStatus().textContent = '加载失败';
      dbList().innerHTML = '<div class="empty-state">加载数据库列表失败。</div>';
    }
  }

  function renderDatabaseList() {
    if (!state.databases.length) {
      dbList().innerHTML = '<div class="empty-state">暂无已导入的数据库。</div>';
      return;
    }

    dbList().innerHTML = state.databases.map(function (db) {
      var isExpanded = state.expandedDbId === db.database_id;
      var isActive = state.selectedDbId === db.database_id;
      var headerClass = 'db-card-header';
      if (isExpanded) headerClass += ' expanded';
      if (isActive) headerClass += ' active';
      var tableCount = (db.tables || []).length;

      var html = '<div class="db-card">';
      html += '<div class="' + headerClass + '" data-db-id="' + db.database_id + '">';
      html += '<div class="db-card-info">';
      html += '<div class="db-card-name">' + escapeHtml(db.display_name || db.file_name || db.database_id) + '</div>';
      html += '<div class="db-card-time">' + (db.created_at ? formatTime(db.created_at) : '') + ' · ' + tableCount + ' 张表</div>';
      html += '</div>';
      html += '<span class="db-card-arrow">▶</span>';
      html += '</div>';

      html += '<div class="db-card-tables' + (isExpanded ? ' open' : '') + '" data-db-tables="' + db.database_id + '">';
      (db.tables || []).forEach(function (tableName) {
        var selClass = 'db-table-item';
        if (state.selectedDbId === db.database_id && state.selectedTable === tableName) {
          selClass += ' selected';
        }
        html += '<button class="' + selClass + '" data-db-id="' + db.database_id + '" data-table="' + escapeHtml(tableName) + '" type="button">' + escapeHtml(tableName) + '</button>';
      });
      html += '</div>';
      html += '</div>';

      return html;
    }).join('');

    dbList().querySelectorAll('.db-card-header').forEach(function (header) {
      header.addEventListener('click', function () {
        var dbId = header.dataset.dbId;
        if (state.expandedDbId === dbId) {
          state.expandedDbId = null;
        } else {
          state.expandedDbId = dbId;
        }
        renderDatabaseList();
      });
    });

    dbList().querySelectorAll('.db-table-item').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        var dbId = btn.dataset.dbId;
        var table = btn.dataset.table;
        selectTable(dbId, table);
      });
    });
  }

  function selectTable(dbId, tableName) {
    state.selectedDbId = dbId;
    state.selectedTable = tableName;
    state.currentPage = 0;
    renderDatabaseList();
    loadTableData();
  }

  async function loadTableData() {
    var dbId = state.selectedDbId;
    var table = state.selectedTable;
    if (!dbId || !table) return;

    var offset = state.currentPage * PAGE_SIZE;

    tableContainer().innerHTML = '<div class="empty-state">正在加载...</div>';
    tableInfo().classList.add('hidden');
    pagination().classList.add('hidden');

    panelHeader().innerHTML =
      '<div class="db-manager-panel-title">' + escapeHtml(table) + '</div>' +
      '<div class="db-manager-panel-subtitle">数据库 ' + escapeHtml(dbId) + '</div>';

    try {
      var url = '/api/nl2sql/databases/' + encodeURIComponent(dbId) + '/tables/' + encodeURIComponent(table) + '/data?limit=' + PAGE_SIZE + '&offset=' + offset;
      var resp = await fetch(url);
      if (!resp.ok) {
        var errData = await resp.json().catch(function () { return {}; });
        throw new Error(errData.detail || '请求失败');
      }
      var data = await resp.json();
      state.totalRows = data.total || 0;
      renderTableData(data);
    } catch (err) {
      console.error(err);
      tableContainer().innerHTML = '<div class="empty-state">加载数据失败：' + escapeHtml(err.message) + '</div>';
    }
  }

  function renderTableData(data) {
    var columns = data.columns || [];
    var rows = data.rows || [];
    var total = data.total || 0;
    var offset = data.offset || 0;
    var limit = data.limit || PAGE_SIZE;

    tableInfo().classList.remove('hidden');
    tableInfo().innerHTML =
      '<span class="info-text">共 ' + total + ' 行，显示第 ' + (offset + 1) + ' – ' + Math.min(offset + rows.length, total) + ' 行</span>' +
      '<span class="info-text">' + columns.length + ' 列</span>';

    if (!columns.length || !rows.length) {
      tableContainer().innerHTML = '<div class="empty-state">该表为空。</div>';
      pagination().classList.add('hidden');
      return;
    }

    var html = '<table class="db-manager-table"><thead><tr>';
    columns.forEach(function (col) {
      html += '<th>' + escapeHtml(col) + '</th>';
    });
    html += '</tr></thead><tbody>';

    rows.forEach(function (row) {
      html += '<tr>';
      row.forEach(function (cell) {
        if (cell === null || cell === undefined) {
          html += '<td class="null-cell">NULL</td>';
        } else {
          html += '<td>' + escapeHtml(String(cell)) + '</td>';
        }
      });
      html += '</tr>';
    });

    html += '</tbody></table>';
    tableContainer().innerHTML = html;

    renderPagination(total, offset, limit);
  }

  function renderPagination(total, offset, limit) {
    var totalPages = Math.ceil(total / limit) || 1;
    if (totalPages <= 1) {
      pagination().classList.add('hidden');
      return;
    }

    pagination().classList.remove('hidden');
    var currentPage = Math.floor(offset / limit);
    var html = '';
    html += '<button class="page-btn" data-page="0" type="button"' + (currentPage === 0 ? ' disabled' : '') + '>首页</button>';
    html += '<button class="page-btn" data-page="' + (currentPage - 1) + '" type="button"' + (currentPage === 0 ? ' disabled' : '') + '>上一页</button>';
    html += '<span class="page-info">第 ' + (currentPage + 1) + ' / ' + totalPages + ' 页</span>';
    html += '<button class="page-btn" data-page="' + (currentPage + 1) + '" type="button"' + (currentPage >= totalPages - 1 ? ' disabled' : '') + '>下一页</button>';
    html += '<button class="page-btn" data-page="' + (totalPages - 1) + '" type="button"' + (currentPage >= totalPages - 1 ? ' disabled' : '') + '>末页</button>';
    pagination().innerHTML = html;

    pagination().querySelectorAll('.page-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var page = parseInt(btn.dataset.page, 10);
        if (!isNaN(page) && page >= 0 && page < totalPages) {
          state.currentPage = page;
          loadTableData();
        }
      });
    });
  }

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  loadDatabases();
})();