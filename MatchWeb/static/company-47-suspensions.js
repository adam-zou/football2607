const REFRESH_INTERVAL_MS = 60_000;
const dateInput = document.getElementById('match-date');
const queryButton = document.getElementById('query-button');
const rows = document.getElementById('match-rows');
const emptyState = document.getElementById('empty-state');
const errorState = document.getElementById('error-state');
const resultSummary = document.getElementById('result-summary');
const updatedAt = document.getElementById('updated-at');
const refreshState = document.getElementById('refresh-state');
const totalCount = document.getElementById('total-count');
const invalidCount = document.getElementById('invalid-count');
const followedCount = document.getElementById('followed-count');
const sessionUsername = document.getElementById('session-username');
const homeLink = document.getElementById('home-link');
const betDialog = document.getElementById('bet-dialog');
const betForm = document.getElementById('bet-form');
const betEntries = document.getElementById('bet-entries');
const betMatchLabel = document.getElementById('bet-match-label');
const betFormError = document.getElementById('bet-form-error');
const addBetButton = document.getElementById('add-bet-button');
const betDialogSave = document.getElementById('bet-dialog-save');
const ONE_X_TWO_VALUES = ['主', '平', '客'];
const TOTAL_VALUES = Array.from({ length: 35 }, (_, index) => String((index + 2) / 4));
const HANDICAP_VALUES = Array.from({ length: 41 }, (_, index) => {
  const value = (index - 20) / 4;
  if (value === 0) return '0';
  return value > 0 ? `+${value}` : String(value);
});
const HANDICAP_ZERO_INDEX = HANDICAP_VALUES.indexOf('0');
let refreshTimer;
let activeBetMatch;

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

function selectedStatuses() {
  return Array.from(document.querySelectorAll('input[name="status"]:checked'), ({ value }) => value);
}

function statusClass(status) {
  if (status === '完') return 'finished';
  if (status === '未开始') return 'pending';
  if (['推迟', '取消', '待定'].includes(status)) return 'other';
  return 'live';
}

function createMatchup(match) {
  const matchup = document.createElement('span');
  const home = document.createElement('span');
  home.className = 'team-name';
  home.textContent = text(match.home_team);
  const versus = document.createElement('span');
  versus.className = 'vs-text';
  versus.textContent = 'vs';
  const away = document.createElement('span');
  away.className = 'team-name';
  away.textContent = text(match.away_team);
  matchup.append(home, versus, away);
  return matchup;
}

function createStatus(status) {
  const badge = document.createElement('span');
  badge.className = `realtime ${statusClass(status)}`;
  badge.textContent = text(status);
  return badge;
}

function formatScheduledTime(value) {
  const match = String(value || '').match(/(\d{2}:\d{2})$/);
  return match ? match[1] : text(value);
}

function applyPBStatus(row, buttons, status) {
  row.classList.toggle('pb-followed', status === '关注');
  row.classList.toggle('pb-invalid', status === '作废');
  for (const button of buttons) {
    button.setAttribute('aria-pressed', String(button.dataset.status === status));
  }
}

async function savePBStatus(match, row, buttons, status) {
  buttons.forEach((button) => { button.disabled = true; });
  errorState.hidden = true;
  try {
    const response = await fetch(
      `/api/company-47-suspensions/${encodeURIComponent(match.match_id)}/status`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status }),
      },
    );
    if (response.status === 401) {
      location.href = '/login';
      return;
    }
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || '保存失败');
    match.pb_status = payload.status;
    applyPBStatus(row, buttons, payload.status);
  } catch (error) {
    errorState.hidden = false;
    errorState.textContent = error.message || '保存 PB 状态失败';
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
  }
}

function createActions(match, row) {
  const actions = document.createElement('div');
  actions.className = 'pb-actions';
  const buttons = ['关注', '作废'].map((status) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = status === '关注' ? 'pb-action follow' : 'pb-action invalid';
    button.dataset.status = status;
    button.textContent = status;
    button.addEventListener('click', () => savePBStatus(match, row, buttons, status));
    actions.append(button);
    return button;
  });
  applyPBStatus(row, buttons, match.pb_status);
  return actions;
}

function createSuspensionMarker(points) {
  if (!Array.isArray(points) || points.length === 0) return '—';
  const marker = document.createElement('span');
  marker.className = 'filter-marker';
  marker.tabIndex = 0;
  marker.textContent = '详情';
  marker.setAttribute('aria-label', `封盘时间点 ${points.length} 条，聚焦后查看详情`);

  const tooltip = document.createElement('span');
  tooltip.className = 'filter-tooltip';
  tooltip.setAttribute('role', 'tooltip');
  for (const point of points) {
    const line = document.createElement('span');
    const matchMinute = point.match_minute === null || point.match_minute === undefined
      ? '-'
      : point.match_minute;
    line.textContent = `${point.change_time} · 比赛分钟：${matchMinute}`;
    tooltip.append(line);
  }
  marker.append(tooltip);
  return marker;
}

function marketValues(marketType) {
  if (marketType === '大小球') return TOTAL_VALUES;
  return ONE_X_TWO_VALUES;
}

function replaceSelectOptions(
  select, values, selectedValue, includePlaceholder = true, placeholderIndex = 0,
) {
  select.replaceChildren();
  let placeholder;
  if (includePlaceholder) {
    placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = '请选择';
    placeholder.selected = !selectedValue;
  }
  values.forEach((value, index) => {
    if (placeholder && index === placeholderIndex) select.append(placeholder);
    const option = document.createElement('option');
    option.value = value;
    option.textContent = value;
    option.selected = value === selectedValue;
    select.append(option);
  });
  if (placeholder && placeholderIndex >= values.length) select.append(placeholder);
}

function appendBetEntry(bet = {
  bet_period: '', market_type: '大小球', market_value: '',
  home_handicap: '', away_handicap: '',
}) {
  const entry = document.createElement('section');
  entry.className = 'bet-entry';

  const heading = document.createElement('div');
  heading.className = 'bet-entry-heading';
  const title = document.createElement('strong');
  heading.append(title);
  const removeButton = document.createElement('button');
  removeButton.type = 'button';
  removeButton.className = 'text-button danger-text';
  removeButton.textContent = '删除';
  removeButton.addEventListener('click', () => {
    entry.remove();
    renumberBetEntries();
  });
  heading.append(removeButton);

  const periodLabel = document.createElement('label');
  periodLabel.className = 'form-field';
  periodLabel.append('大类');
  const periodSelect = document.createElement('select');
  periodSelect.className = 'bet-period';
  replaceSelectOptions(periodSelect, ['全场', '半场'], bet.bet_period);
  periodLabel.append(periodSelect);

  const marketLabel = document.createElement('label');
  marketLabel.className = 'form-field';
  marketLabel.append('盘口类型');
  const marketSelect = document.createElement('select');
  marketSelect.className = 'bet-market-type';
  replaceSelectOptions(marketSelect, ['胜平负', '大小球', '让球'], bet.market_type, false);
  marketLabel.append(marketSelect);

  const valueLabel = document.createElement('label');
  valueLabel.className = 'form-field bet-standard-value';
  valueLabel.append('盘口值');
  const valueSelect = document.createElement('select');
  valueSelect.className = 'bet-market-value';
  replaceSelectOptions(valueSelect, marketValues(bet.market_type), bet.market_value);
  valueLabel.append(valueSelect);

  const homeHandicapLabel = document.createElement('label');
  homeHandicapLabel.className = 'form-field bet-handicap-value';
  homeHandicapLabel.append('主队让');
  const homeHandicapSelect = document.createElement('select');
  homeHandicapSelect.className = 'bet-home-handicap';
  replaceSelectOptions(
    homeHandicapSelect, HANDICAP_VALUES, bet.home_handicap, true,
    HANDICAP_ZERO_INDEX,
  );
  homeHandicapLabel.append(homeHandicapSelect);

  const awayHandicapLabel = document.createElement('label');
  awayHandicapLabel.className = 'form-field bet-handicap-value';
  awayHandicapLabel.append('客队让');
  const awayHandicapSelect = document.createElement('select');
  awayHandicapSelect.className = 'bet-away-handicap';
  replaceSelectOptions(
    awayHandicapSelect, HANDICAP_VALUES, bet.away_handicap, true,
    HANDICAP_ZERO_INDEX,
  );
  awayHandicapLabel.append(awayHandicapSelect);

  function syncMarketFields() {
    const isHandicap = marketSelect.value === '让球';
    valueLabel.hidden = isHandicap;
    homeHandicapLabel.hidden = !isHandicap;
    awayHandicapLabel.hidden = !isHandicap;
  }

  marketSelect.addEventListener('change', () => {
    replaceSelectOptions(valueSelect, marketValues(marketSelect.value), '');
    replaceSelectOptions(
      homeHandicapSelect, HANDICAP_VALUES, '', true, HANDICAP_ZERO_INDEX,
    );
    replaceSelectOptions(
      awayHandicapSelect, HANDICAP_VALUES, '', true, HANDICAP_ZERO_INDEX,
    );
    syncMarketFields();
  });

  entry.append(
    heading, periodLabel, marketLabel, valueLabel,
    homeHandicapLabel, awayHandicapLabel,
  );
  betEntries.append(entry);
  syncMarketFields();
  renumberBetEntries();
}

function renumberBetEntries() {
  Array.from(betEntries.children).forEach((entry, index) => {
    entry.querySelector('strong').textContent = `下注${index + 1}`;
  });
}

function openBetDialog(match) {
  activeBetMatch = match;
  betMatchLabel.textContent = `比赛 ID：${match.match_id}`;
  betEntries.replaceChildren();
  betFormError.hidden = true;
  const savedBets = Array.isArray(match.bets) && match.bets.length > 0
    ? match.bets
    : [{
      bet_period: '', market_type: '大小球', market_value: '',
      home_handicap: '', away_handicap: '',
    }];
  savedBets.forEach((bet) => appendBetEntry(bet));
  betDialog.showModal();
}

function closeBetDialog() {
  betDialog.close();
  activeBetMatch = undefined;
}

function createBetButton(match) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'pb-action bet';
  button.textContent = '下注';
  button.setAttribute('aria-pressed', String(Array.isArray(match.bets) && match.bets.length > 0));
  button.addEventListener('click', () => openBetDialog(match));
  return button;
}

function renderMatches(matches) {
  rows.replaceChildren();
  for (const match of matches) {
    const row = document.createElement('tr');
    const link = document.createElement('a');
    link.href = `https://live.nowscore.com/odds/3in1Odds.aspx?companyid=47&id=${encodeURIComponent(match.match_id)}`;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.className = 'match-id-link';
    link.textContent = match.match_id;

    const cells = [
      link,
      text(match.league),
      createMatchup(match),
      formatScheduledTime(match.scheduled_time),
      createStatus(match.status_text),
      match.home_score == null || match.away_score == null ? '—' : `${match.home_score} : ${match.away_score}`,
      text(match.warning_line),
      createSuspensionMarker(match.suspension_points),
      createActions(match, row),
      createBetButton(match),
    ];
    cells.forEach((content, index) => {
      const cell = document.createElement('td');
      if (content instanceof Node) cell.append(content);
      else cell.textContent = content;
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
    selectedStatuses().forEach((status) => params.append('status', status));
    const response = await fetch(`/api/company-47-suspensions?${params}`, { cache: 'no-store' });
    if (response.status === 401) {
      location.href = '/login';
      return;
    }
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || '读取失败');
    renderMatches(payload.matches);
    emptyState.hidden = payload.matches.length !== 0;
    totalCount.textContent = payload.matches.length;
    invalidCount.textContent = payload.matches.filter(({ pb_status: status }) => status === '作废').length;
    followedCount.textContent = payload.matches.filter(({ pb_status: status }) => status === '关注').length;
    const statusLabels = payload.statuses.map((status) => ({
      未开始: '赛前预警',
      进行中: '滚球预警',
      完: '完场',
    })[status] || status);
    resultSummary.textContent = `${payload.date} · ${statusLabels.join('、')} · 共 ${payload.total} 场`;
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

async function loadSession() {
  try {
    const response = await fetch('/api/session', { cache: 'no-store' });
    if (!response.ok) return;
    const payload = await response.json();
    homeLink.hidden = !payload.username || payload.username.toLowerCase().includes('user');
    sessionUsername.textContent = payload.username || '—';
  } catch (_) {
    // The match list remains usable when the optional identity label cannot load.
  }
}

dateInput.value = localDateValue();
queryButton.addEventListener('click', loadMatches);
dateInput.addEventListener('change', loadMatches);
document.querySelectorAll('input[name="status"]').forEach((input) => input.addEventListener('change', () => {
  if (selectedStatuses().length === 0) input.checked = true;
  loadMatches();
}));
addBetButton.addEventListener('click', () => appendBetEntry());
document.getElementById('bet-dialog-close').addEventListener('click', closeBetDialog);
document.getElementById('bet-dialog-cancel').addEventListener('click', closeBetDialog);
betForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!activeBetMatch) return;
  const bets = Array.from(betEntries.children, (entry) => ({
    bet_period: entry.querySelector('.bet-period').value,
    market_type: entry.querySelector('.bet-market-type').value,
    market_value: entry.querySelector('.bet-market-value').value,
    home_handicap: entry.querySelector('.bet-home-handicap').value,
    away_handicap: entry.querySelector('.bet-away-handicap').value,
  }));
  if (bets.length === 0) {
    betFormError.hidden = false;
    betFormError.textContent = '请至少增加一个下注单';
    return;
  }
  const missingPeriodIndex = bets.findIndex((bet) => !bet.bet_period);
  if (missingPeriodIndex !== -1) {
    betFormError.hidden = false;
    betFormError.textContent = `下注${missingPeriodIndex + 1}请选择大类`;
    betEntries.children[missingPeriodIndex].querySelector('.bet-period').focus();
    return;
  }
  const missingValueIndex = bets.findIndex((bet) => (
    bet.market_type === '让球'
      ? !bet.home_handicap && !bet.away_handicap
      : !bet.market_value
  ));
  if (missingValueIndex !== -1) {
    betFormError.hidden = false;
    const missingBet = bets[missingValueIndex];
    betFormError.textContent = missingBet.market_type === '让球'
      ? `下注${missingValueIndex + 1}请选择主队让或客队让盘口值`
      : `下注${missingValueIndex + 1}请选择盘口值`;
    const selector = missingBet.market_type === '让球'
      ? '.bet-home-handicap'
      : '.bet-market-value';
    betEntries.children[missingValueIndex].querySelector(selector).focus();
    return;
  }
  betDialogSave.disabled = true;
  betFormError.hidden = true;
  try {
    const response = await fetch(
      `/api/company-47-suspensions/${encodeURIComponent(activeBetMatch.match_id)}/bets`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bets }),
      },
    );
    if (response.status === 401) {
      location.href = '/login';
      return;
    }
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || '保存失败');
    activeBetMatch.bets = payload.bets;
    closeBetDialog();
  } catch (error) {
    betFormError.hidden = false;
    betFormError.textContent = error.message || '保存注单失败';
  } finally {
    betDialogSave.disabled = false;
  }
});
loadSession();
loadMatches();
