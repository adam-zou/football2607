const REFRESH_INTERVAL_MS = 60_000;
const dateInput = document.getElementById('match-date');
const oddsFilter = document.getElementById('odds-filter');
const queryButton = document.getElementById('query-button');
const rows = document.getElementById('match-rows');
const emptyState = document.getElementById('empty-state');
const errorState = document.getElementById('error-state');
const resultSummary = document.getElementById('result-summary');
const updatedAt = document.getElementById('updated-at');
const refreshState = document.getElementById('refresh-state');
let refreshTimer;

async function revealAdminNavigation() {
  try {
    const response = await fetch('/api/session', { cache: 'no-store' });
    if (!response.ok) return;
    const session = await response.json();
    document.getElementById('user-management-link').hidden = !session.is_admin;
  } catch (_) {
    // Match loading remains available if the optional session badge cannot load.
  }
}

function localDateValue() {
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit'
  }).formatToParts(new Date());
  const value = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  return `${value.year}-${value.month}-${value.day}`;
}

function selectedStatuses() {
  return Array.from(document.querySelectorAll('input[name="status"]:checked'), ({ value }) => value);
}

function text(value) {
  return value === null || value === undefined || value === '' ? '—' : String(value);
}

function statusClass(status) {
  if (status === '完') return 'finished';
  if (status === '未开始') return 'pending';
  if (['推迟', '取消', '待定'].includes(status)) return 'other';
  return 'live';
}

function createFilterMarker(markers) {
  if (!Array.isArray(markers) || markers.length === 0) return '—';
  const marker = document.createElement('span');
  marker.className = 'filter-marker';
  marker.tabIndex = 0;
  marker.textContent = '详情';
  marker.setAttribute('aria-label', `筛选命中 ${markers.length} 条，聚焦后查看详情`);

  const tooltip = document.createElement('span');
  tooltip.className = 'filter-tooltip';
  tooltip.setAttribute('role', 'tooltip');
  for (const item of markers) {
    const line = document.createElement('span');
    line.textContent = `${item.company_name} · ${item.change_time}`;
    tooltip.append(line);
  }
  marker.append(tooltip);
  return marker;
}

function renderMatches(matches) {
  rows.replaceChildren();
  for (const match of matches) {
    const row = document.createElement('tr');
    const link = document.createElement('a');
    link.href = `https://live.nowscore.com/odds/3in1Odds.aspx?companyid=3&id=${encodeURIComponent(match.match_id)}`;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = match.match_id;

    const cells = [
      link,
      text(match.league),
      text(match.scheduled_time),
      text(match.status_text),
      text(match.home_team),
      match.home_score == null || match.away_score == null ? '—' : `${match.home_score} : ${match.away_score}`,
      text(match.away_team),
      createFilterMarker(match.filter_markers),
    ];
    cells.forEach((content, index) => {
      const cell = document.createElement('td');
      if (content instanceof Node) cell.append(content);
      else cell.textContent = content;
      if (index === 3) cell.className = `match-status ${statusClass(match.status_text)}`;
      if (index === 5) cell.classList.add('score');
      row.append(cell);
    });
    rows.append(row);
  }
}

async function loadMatches() {
  clearTimeout(refreshTimer);
  queryButton.disabled = true;
  refreshState.textContent = '正在刷新…';
  errorState.hidden = true;
  try {
    const params = new URLSearchParams({
      date: dateInput.value,
      odds_filter: oddsFilter.checked ? '1' : '0',
    });
    selectedStatuses().forEach((status) => params.append('status', status));
    const response = await fetch(`/api/matches?${params}`, { cache: 'no-store' });
    if (response.status === 401) {
      location.href = '/login';
      return;
    }
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || '读取失败');
    renderMatches(payload.matches);
    emptyState.hidden = payload.matches.length !== 0;
    resultSummary.textContent = `${payload.date} · ${payload.statuses.join('、')} · 共 ${payload.total} 场`;
    updatedAt.textContent = `更新于 ${new Date(payload.refreshed_at).toLocaleTimeString('zh-CN', { hour12: false })}`;
    refreshState.textContent = '每 60 秒自动刷新';
  } catch (error) {
    rows.replaceChildren();
    emptyState.hidden = true;
    errorState.hidden = false;
    errorState.textContent = error.message || '读取比赛数据失败';
    resultSummary.textContent = '暂时无法读取比赛数据';
    refreshState.textContent = '刷新失败，将自动重试';
  } finally {
    queryButton.disabled = false;
    refreshTimer = setTimeout(loadMatches, REFRESH_INTERVAL_MS);
  }
}

dateInput.value = localDateValue();
queryButton.addEventListener('click', loadMatches);
dateInput.addEventListener('change', loadMatches);
document.querySelectorAll('input[name="status"]').forEach((input) => input.addEventListener('change', () => {
  if (selectedStatuses().length === 0) input.checked = true;
  loadMatches();
}));
oddsFilter.addEventListener('change', loadMatches);
revealAdminNavigation();
loadMatches();
