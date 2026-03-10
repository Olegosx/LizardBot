/**
 * LizardBot — SPA клиент
 * WebSocket: status / stats / markets / positions / history / logs / logs_append
 * REST: start, stop, GET/POST/PATCH config
 */

// ── Состояние ────────────────────────────────────────────────────────────────

const state = {
  markets: {},       // condition_id → market object
  charts: {},        // condition_id → Chart.js instance
  logLines: [],      // UI-буфер логов
  simMode: false,
  active:  false,
};

const MAX_LOG = 500;
const WS_RECONNECT_MS = 3000;
const THEME_KEY = 'lz-theme';

// ── Auth ──────────────────────────────────────────────────────────────────────

async function fetchWhoami() {
  const resp = await fetch('/api/whoami');
  if (resp.status === 401) { location.href = '/login'; return; }
  if (resp.ok) {
    const data = await resp.json();
    const el = document.getElementById('nav-username');
    if (el) el.textContent = data.username;
  }
}

// ── Тема ──────────────────────────────────────────────────────────────────────

function applyTheme(theme) {
  document.documentElement.setAttribute('data-bs-theme', theme);
  const icon = document.getElementById('theme-icon');
  if (icon) {
    icon.className = theme === 'dark'
      ? 'bi bi-moon-stars-fill'
      : 'bi bi-sun-fill';
  }
  // Chart.js: перерисовать все графики с новыми цветами
  const gridColor = theme === 'dark' ? '#2d3139' : '#e2e8f0';
  const tickColor = theme === 'dark' ? '#6b7280' : '#94a3b8';
  Object.values(state.charts).forEach(chart => {
    chart.options.scales.y.grid.color  = gridColor;
    chart.options.scales.y.ticks.color = tickColor;
    chart.options.scales.x.ticks.color = tickColor;
    chart.update('none');
  });
}

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-bs-theme') || 'dark';
  const next = current === 'dark' ? 'light' : 'dark';
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
}

function initTheme() {
  const saved = localStorage.getItem(THEME_KEY) || 'dark';
  applyTheme(saved);
}

// ── WebSocket ─────────────────────────────────────────────────────────────────

let ws = null;

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen    = () => setWsStatus(true);
  ws.onclose   = () => { setWsStatus(false); setTimeout(connectWS, WS_RECONNECT_MS); };
  ws.onerror   = () => ws.close();
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    handleEvent(msg.event, msg.data);
  };
}

function handleEvent(event, data) {
  switch (event) {
    case 'status':      renderStatus(data);    break;
    case 'stats':       renderStats(data);     break;
    case 'markets':     renderMarkets(data);   break;
    case 'positions':   renderPositions(data); break;
    case 'history':     renderHistory(data);   break;
    case 'logs':
      state.logLines = [];
      document.getElementById('log-container').innerHTML = '';
      appendLogs(data);
      break;
    case 'logs_append': appendLogs(data); break;
  }
}

// ── REST ──────────────────────────────────────────────────────────────────────

async function apiFetch(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  if (resp.status === 401) {
    location.href = '/login';
    throw new Error('Сессия истекла');
  }
  return resp;
}

// ── Управление ────────────────────────────────────────────────────────────────

async function cmdStart() {
  const resp = await apiFetch('POST', '/api/start');
  if (!resp.ok) console.error('start error:', await resp.text());
}

async function cmdStop() {
  const resp = await apiFetch('POST', '/api/stop');
  if (!resp.ok) console.error('stop error:', await resp.text());
}

async function toggleSimMode(checked) {
  const resp = await apiFetch('PATCH', '/api/config', { simulation_mode: checked });
  if (!resp.ok) {
    console.error('sim toggle error:', await resp.text());
    document.getElementById('sim-toggle').checked = !checked;
  }
}

// ── Render: status ────────────────────────────────────────────────────────────

function renderStatus(data) {
  const badge  = document.getElementById('status-badge');
  const text   = document.getElementById('status-text');
  const balEl  = document.getElementById('balance-val');
  const simBnr = document.getElementById('sim-banner');
  const simChk = document.getElementById('sim-toggle');

  state.active  = data.running;
  state.simMode = data.simulation_mode;

  badge.className = 'badge fs-6 ' + (state.active ? 'running' : 'stopped');
  text.textContent = state.active ? 'Активен' : 'Остановлен';

  balEl.textContent = data.balance !== null ? fmtUSD(data.balance) : '–';

  simBnr.classList.toggle('d-none', !state.simMode);
  simChk.checked = state.simMode;

  document.getElementById('btn-start').disabled = state.active;
  document.getElementById('btn-stop').disabled  = !state.active;
}

// ── Render: stats ─────────────────────────────────────────────────────────────

function renderStats(data) {
  document.getElementById('total-pnl').innerHTML   = fmtPnl(data.total_pnl);
  document.getElementById('total-trades').textContent = data.total_trades ?? '–';
  document.getElementById('win-rate').textContent  =
    data.win_rate !== null ? `${(data.win_rate * 100).toFixed(1)}%` : '–';
}

// ── Render: markets ───────────────────────────────────────────────────────────

function renderMarkets(list) {
  const container = document.getElementById('markets-container');
  const empty     = document.getElementById('markets-empty');
  const countEl   = document.getElementById('markets-count');

  if (!list || list.length === 0) {
    empty.classList.remove('d-none');
    countEl.textContent = 0;
    return;
  }
  empty.classList.add('d-none');
  countEl.textContent = list.length;

  // Активные (уже открытые) — выше ожидающих открытия
  const sorted = [...list].sort((a, b) => {
    const aPending = a.start_time && new Date(a.start_time) > new Date();
    const bPending = b.start_time && new Date(b.start_time) > new Date();
    if (aPending !== bPending) return aPending ? 1 : -1;
    return new Date(a.close_time) - new Date(b.close_time);
  });

  const seen = new Set();
  sorted.forEach(m => {
    seen.add(m.condition_id);
    if (!state.markets[m.condition_id]) _createMarketCard(container, m);
    _updateMarketCard(m);
    state.markets[m.condition_id] = m;
  });

  // Переставляем карточки в DOM в нужный порядок
  sorted.forEach(m => {
    const card = container.querySelector(`.market-card[data-cid="${m.condition_id}"]`);
    if (card) container.appendChild(card);
  });

  Object.keys(state.markets).forEach(cid => {
    if (!seen.has(cid)) _removeMarketCard(cid);
  });
}

function _createMarketCard(container, m) {
  const tpl  = document.getElementById('market-card-tpl');
  const node = tpl.content.cloneNode(true);
  node.querySelector('.market-card').dataset.cid = m.condition_id;
  container.appendChild(node);

  const canvas = container.querySelector(
    `.market-card[data-cid="${m.condition_id}"] .market-chart`
  );
  if (canvas) state.charts[m.condition_id] = _makeChart(canvas);
}

function _updateMarketCard(m) {
  const card = document.querySelector(`.market-card[data-cid="${m.condition_id}"]`);
  if (!card) return;

  card.querySelector('.market-question').textContent = m.question || m.slug;

  const sb = card.querySelector('.market-status-badge');
  sb.textContent = _statusLabel(m.status);
  sb.className = 'badge ms-2 market-status-badge ' + _statusClass(m.status);

  card.querySelector('.market-signal-badge').classList.toggle('d-none', !m.signal_fired);

  // Время закрытия
  const closeEl = card.querySelector('.market-close-time');
  if (m.close_time) {
    const diff = _minutesUntil(m.close_time);
    closeEl.textContent = diff !== null
      ? (diff > 0 ? `через ${diff} мин` : 'закрыт')
      : fmtDateTime(m.close_time);
  }

  // Проверяем: рынок ещё не открылся?
  const pending = m.start_time && new Date(m.start_time) > new Date();
  const pendingEl = card.querySelector('.market-pending');
  const bodyEl    = card.querySelector('.market-body');
  pendingEl.classList.toggle('d-none', !pending);
  bodyEl.classList.toggle('d-none', pending);
  card.classList.toggle('is-pending', pending);

  if (pending) {
    const minsUntilOpen = _minutesUntil(m.start_time);
    const opensEl = card.querySelector('.market-opens-in');
    opensEl.textContent = minsUntilOpen !== null && minsUntilOpen > 0
      ? `открытие через ${minsUntilOpen} мин`
      : `открывается ${fmtDateTime(m.start_time)}`;
    return;
  }

  // Вероятность
  const probEl  = card.querySelector('.market-prob');
  const labelEl = card.querySelector('.market-outcome-label');
  if (m.latest_prob !== null && m.latest_prob !== undefined) {
    probEl.textContent = `${(m.latest_prob * 100).toFixed(1)}%`;
    _colorProb(probEl, m.latest_prob);
    if (m.outcomes && m.outcomes.length > 0) {
      labelEl.textContent = m.latest_prob >= 0.5 ? m.outcomes[0] : (m.outcomes[1] ?? '');
    }
  } else {
    probEl.textContent = '–';
  }

  // График с временной осью X
  const chart = state.charts[m.condition_id];
  if (chart && m.prob_history && m.prob_history.length > 0) {
    _updateChartWithTimeRange(chart, m);
  }
}

function _removeMarketCard(cid) {
  const card = document.querySelector(`.market-card[data-cid="${cid}"]`);
  if (card) card.remove();
  if (state.charts[cid]) { state.charts[cid].destroy(); delete state.charts[cid]; }
  delete state.markets[cid];
}

function _makeChart(canvas) {
  const isDark = document.documentElement.getAttribute('data-bs-theme') !== 'light';
  const gridColor = isDark ? '#2d3139' : '#e2e8f0';
  const tickColor = isDark ? '#6b7280' : '#94a3b8';

  return new Chart(canvas, {
    type: 'line',
    data: {
      datasets: [{
        data: [],              // формат {x: Date, y: number}
        borderColor: '#3b82f6',
        backgroundColor: 'rgba(59,130,246,0.08)',
        borderWidth: 1.5,
        pointRadius: 0,
        tension: 0.3,
        fill: true,
      }]
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: {
        x: {
          type: 'time',
          display: true,
          time: { unit: 'minute', displayFormats: { minute: 'HH:mm' } },
          ticks: {
            color: tickColor,
            font: { size: 9 },
            maxTicksLimit: 6,
            maxRotation: 0,
          },
          grid: { display: false },
        },
        y: {
          min: 0, max: 100, display: true,
          grid: { color: gridColor },
          ticks: { color: tickColor, font: { size: 9 }, maxTicksLimit: 4,
                   callback: v => v + '%' },
        }
      }
    }
  });
}

function _updateChartWithTimeRange(chart, m) {
  const now       = new Date();
  const startTime = new Date(m.start_time);
  const closeTime = new Date(m.close_time);
  const rangeEnd  = closeTime < now ? closeTime : now;

  // Данные: {x: Date, y: number}
  chart.data.datasets[0].data = m.prob_history.map(p => ({
    x: new Date(p.timestamp),
    y: +(p.probability * 100).toFixed(2),
  }));

  // Фиксируем диапазон X: от начала рынка до закрытия (или сейчас)
  chart.options.scales.x.min = startTime;
  chart.options.scales.x.max = closeTime < now ? closeTime : closeTime;

  // Единица времени — авто по длине окна
  const durationMin = (closeTime - startTime) / 60000;
  chart.options.scales.x.time.unit = durationMin <= 120 ? 'minute' : 'hour';

  chart.update('none');
}

// ── Render: positions ─────────────────────────────────────────────────────────

function renderPositions(list) {
  const tbody = document.getElementById('positions-tbody');
  if (!list || list.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" class="text-center py-3 small" style="color:var(--lz-text-dim);">Нет открытых позиций</td></tr>';
    return;
  }
  tbody.innerHTML = list.map(p => `
    <tr>
      <td class="text-truncate" style="max-width:180px;" title="${esc(p.condition_id)}">${esc(p.slug || p.condition_id)}</td>
      <td><span class="badge bg-secondary">${esc(p.outcome)}</span></td>
      <td>${fmtUSD(p.amount)}</td>
      <td>${(p.entry_price * 100).toFixed(1)}¢</td>
      <td style="color:var(--lz-text-muted);">${fmtDateTime(p.opened_at)}</td>
    </tr>`).join('');
}

// ── Render: history ───────────────────────────────────────────────────────────

function renderHistory(list) {
  const tbody = document.getElementById('history-tbody');
  if (!list || list.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="text-center py-3 small" style="color:var(--lz-text-dim);">Нет завершённых сделок</td></tr>';
    return;
  }
  const sorted = [...list].sort((a, b) => new Date(b.closed_at) - new Date(a.closed_at));
  tbody.innerHTML = sorted.map(t => `
    <tr>
      <td style="color:var(--lz-text-muted);">${fmtDateTime(t.closed_at)}</td>
      <td class="text-truncate" style="max-width:120px;" title="${esc(t.condition_id)}">${esc(t.slug || t.condition_id)}</td>
      <td><span class="badge bg-secondary">${esc(t.outcome)}</span></td>
      <td>${fmtUSD(t.amount)}</td>
      <td>${fmtPnl(t.pnl)}</td>
      <td>${t.won
        ? '<span class="badge bg-success">Победа</span>'
        : '<span class="badge bg-danger">Поражение</span>'}</td>
    </tr>`).join('');
}

// ── Render: logs ──────────────────────────────────────────────────────────────

function appendLogs(entries) {
  if (!entries || entries.length === 0) return;
  const container = document.getElementById('log-container');
  entries.forEach(e => {
    state.logLines.push(e);
    const div = document.createElement('div');
    div.className = 'log-line level-' + (e.level || 'info').toLowerCase();
    div.textContent = `${fmtTime(e.timestamp)} ${e.message}`;
    container.insertBefore(div, container.firstChild);
  });
  while (state.logLines.length > MAX_LOG) {
    state.logLines.shift();
    if (container.lastChild) container.removeChild(container.lastChild);
  }
  const countEl = document.getElementById('log-count');
  if (countEl) countEl.textContent = state.logLines.length;
}

function clearLogs() {
  state.logLines = [];
  document.getElementById('log-container').innerHTML = '';
  const countEl = document.getElementById('log-count');
  if (countEl) countEl.textContent = 0;
}

// ── Настройки: загрузка / сохранение ─────────────────────────────────────────

let _rawConfig = null;

async function loadConfigForm() {
  try {
    const resp = await apiFetch('GET', '/api/config');
    if (!resp.ok) throw new Error(await resp.text());
    _rawConfig = await resp.json();
    _fillForm(_rawConfig);
    cfgAlert('', '');
  } catch (e) {
    cfgAlert('danger', `Не удалось загрузить конфиг: ${e.message}`);
  }
}

function _fillForm(cfg) {
  _set('cfg-vol_threshold',             cfg.vol_threshold);
  _set('cfg-lookback_minutes',          cfg.lookback_minutes);
  _set('cfg-danger_zone_action',        cfg.danger_zone_action);
  _set('cfg-danger_zone_reduce_factor', cfg.danger_zone_reduce_factor);
  _set('cfg-recovery_action',           cfg.recovery_action);
  _set('cfg-bet_mode',                  cfg.bet_mode);
  _set('cfg-bet_amount',                cfg.bet_amount);
  _set('cfg-bet_percent',               cfg.bet_percent);
  _set('cfg-config_reload_interval',    cfg.config_reload_interval);
  _set('cfg-log_level',                 cfg.log_level);
  _set('cfg-server_host',               cfg.server?.host);
  _set('cfg-server_port',               cfg.server?.port);
  _set('cfg-private_key',               cfg.private_key);
  _set('cfg-api_key',                   cfg.api_key);
  _set('cfg-api_secret',                cfg.api_secret);
  _set('cfg-api_passphrase',            cfg.api_passphrase);
  _set('cfg-funder_address',            cfg.funder_address);
  _renderMarketFilters(cfg.market_filters || []);
}

function _set(id, value) {
  const el = document.getElementById(id);
  if (el && value !== undefined && value !== null) el.value = value;
}

function _renderMarketFilters(filters) {
  const list = document.getElementById('market-filters-list');
  if (!filters.length) {
    list.innerHTML = '<p class="small mb-0" style="color:var(--lz-text-dim);">Нет фильтров. Нажмите «Добавить».</p>';
    return;
  }
  list.innerHTML = filters.map((f, i) => `
    <div class="row g-2 align-items-center mb-2" id="mf-row-${i}">
      <div class="col-4">
        <input type="text" class="form-control form-control-sm"
               id="mf-name-${i}" placeholder="Название" value="${esc(f.name)}" />
      </div>
      <div class="col-5">
        <input type="text" class="form-control form-control-sm font-monospace"
               id="mf-ticker-${i}" placeholder="series_ticker" value="${esc(f.series_ticker)}" />
      </div>
      <div class="col-2 d-flex align-items-center gap-1">
        <div class="form-check form-switch mb-0">
          <input class="form-check-input" type="checkbox" id="mf-enabled-${i}"
                 ${f.enabled ? 'checked' : ''} />
          <label class="form-check-label small" for="mf-enabled-${i}">Вкл</label>
        </div>
      </div>
      <div class="col-1">
        <button class="btn btn-outline-danger btn-sm" onclick="removeMarketFilter(${i})">
          <i class="bi bi-trash3"></i>
        </button>
      </div>
    </div>`).join('');
}

function addMarketFilter() {
  const filters = _collectMarketFilters();
  filters.push({ name: '', series_ticker: '', enabled: true });
  _renderMarketFilters(filters);
}

function removeMarketFilter(i) {
  const filters = _collectMarketFilters();
  filters.splice(i, 1);
  _renderMarketFilters(filters);
}

function _collectMarketFilters() {
  const rows = document.querySelectorAll('[id^="mf-row-"]');
  return Array.from(rows).map((_, i) => ({
    name:          document.getElementById(`mf-name-${i}`)?.value    || '',
    series_ticker: document.getElementById(`mf-ticker-${i}`)?.value  || '',
    enabled:       document.getElementById(`mf-enabled-${i}`)?.checked ?? true,
  }));
}

function _buildConfigPayload() {
  return {
    ..._rawConfig,
    vol_threshold:             parseFloat(document.getElementById('cfg-vol_threshold').value),
    lookback_minutes:          parseInt(document.getElementById('cfg-lookback_minutes').value),
    danger_zone_action:        document.getElementById('cfg-danger_zone_action').value,
    danger_zone_reduce_factor: parseFloat(document.getElementById('cfg-danger_zone_reduce_factor').value),
    recovery_action:           document.getElementById('cfg-recovery_action').value,
    bet_mode:                  document.getElementById('cfg-bet_mode').value,
    bet_amount:                parseFloat(document.getElementById('cfg-bet_amount').value),
    bet_percent:               parseFloat(document.getElementById('cfg-bet_percent').value),
    config_reload_interval:    parseInt(document.getElementById('cfg-config_reload_interval').value),
    log_level:                 document.getElementById('cfg-log_level').value,
    server: {
      host: document.getElementById('cfg-server_host').value,
      port: parseInt(document.getElementById('cfg-server_port').value),
    },
    private_key:    document.getElementById('cfg-private_key').value,
    api_key:        document.getElementById('cfg-api_key').value,
    api_secret:     document.getElementById('cfg-api_secret').value,
    api_passphrase: document.getElementById('cfg-api_passphrase').value,
    funder_address: document.getElementById('cfg-funder_address').value,
    market_filters: _collectMarketFilters(),
  };
}

async function saveConfig() {
  if (!_rawConfig) { cfgAlert('warning', 'Конфиг ещё не загружен'); return; }
  const payload = _buildConfigPayload();
  try {
    const resp = await apiFetch('POST', '/api/config', payload);
    if (!resp.ok) {
      const err = await resp.json().catch(async () => ({ detail: await resp.text() }));
      throw new Error(err.detail || resp.statusText);
    }
    _rawConfig = payload;
    cfgAlert('success', 'Настройки сохранены');
  } catch (e) {
    cfgAlert('danger', `Ошибка сохранения: ${e.message}`);
  }
}

function cfgAlert(type, message) {
  const el = document.getElementById('cfg-alert');
  if (!message) { el.className = 'd-none'; return; }
  el.className = `alert alert-${type} py-2 small`;
  el.textContent = message;
}

function onSettingsTabOpen() {
  if (!_rawConfig) loadConfigForm();
}

// ── WS status ─────────────────────────────────────────────────────────────────

function setWsStatus(online) {
  const el = document.getElementById('ws-status');
  el.innerHTML = online
    ? '<i class="bi bi-wifi text-success"></i>'
    : '<i class="bi bi-wifi-off" style="color:#f87171;"></i>';
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtUSD(v) {
  if (v === null || v === undefined) return '–';
  return '$' + (+v).toFixed(2);
}

function fmtPnl(v) {
  if (v === null || v === undefined) return '–';
  const n = +v;
  const cls = n > 0 ? 'pnl-pos' : n < 0 ? 'pnl-neg' : 'pnl-zero';
  return `<span class="${cls}">${n > 0 ? '+' : ''}${n.toFixed(2)}</span>`;
}

function fmtDateTime(iso) {
  if (!iso) return '–';
  return new Date(iso).toLocaleString('ru-RU', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'
  });
}

function fmtTime(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}

function _minutesUntil(iso) {
  if (!iso) return null;
  return Math.round((new Date(iso) - Date.now()) / 60000);
}

function esc(s) {
  if (!s) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function _colorProb(el, prob) {
  if      (prob >= 0.90) el.className = 'fw-bold fs-5 market-prob prob-high';
  else if (prob >= 0.80) el.className = 'fw-bold fs-5 market-prob prob-danger';
  else if (prob >= 0.60) el.className = 'fw-bold fs-5 market-prob prob-mid';
  else                   el.className = 'fw-bold fs-5 market-prob prob-low';
}

function _statusLabel(s) {
  return { monitoring: 'мониторинг', bet_placed: 'ставка',
           closed: 'закрыт', skipped: 'пропущен' }[s] || s || '–';
}

function _statusClass(s) {
  return { monitoring: 'bg-primary', bet_placed: 'bg-warning text-dark',
           closed: 'bg-secondary', skipped: 'bg-secondary' }[s] || 'bg-secondary';
}

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  document.getElementById('theme-btn')?.addEventListener('click', toggleTheme);
  fetchWhoami();
  connectWS();
});
