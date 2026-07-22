const REFRESH_INTERVAL_MS = 60_000;
const dateInput = document.getElementById('match-date');
const queryButton = document.getElementById('query-button');
const rows = document.getElementById('match-rows');
const emptyState = document.getElementById('empty-state');
const errorState = document.getElementById('error-state');
const resultSummary = document.getElementById('result-summary');
const updatedAt = document.getElementById('updated-at');
const refreshState = document.getElementById('refresh-state');
let refreshTimer;

function localDateValue() {
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit'
  }).formatToParts(new Date());
  const value = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  return `${value.year}-${value.month}-${value.day}`;
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

function renderMatches(matches) {
  rows.replaceChildren();
  for (const match of matches) {
    const row = document.createElement('tr');
    const link = document.createElement('a');
    link.href = `https://live.nowscore.com/odds/3in1Odds.aspx?companyid=47&id=${encodeURIComponent(match.match_id)}`;
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
    const params = new URLSearchParams({ date: dateInput.value });
    const response = await fetch(`/api/company-47-suspensions?${params}`, { cache: 'no-store' });
    if (response.status === 401) {
      location.href = '/login';
      return;
    }
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || '读取失败');
    renderMatches(payload.matches);
    emptyState.hidden = payload.matches.length !== 0;
    resultSummary.textContent = `${payload.date} · 共 ${payload.total} 场`;
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
loadMatches();
