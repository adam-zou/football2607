"""In-memory runtime state and HTTP dashboard for SimpleCrawler."""

from __future__ import annotations

import json
import re
import threading
import time
from copy import deepcopy
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Deque, Dict, Iterable, Optional, Tuple

from .dashboard_statistics import empty_odds_counts


COMPONENTS: Tuple[Tuple[str, str], ...] = (
    ("proxy_scheduler", "代理服务"),
    ("fetch_match_ids", "比赛 ID"),
    ("fetch_match_details", "比赛详情"),
    ("fetch_odds_pages", "赔率变化"),
    ("check_match_completion", "完成核验"),
    ("push_wecom_matches", "企业微信通知"),
)

ROUND_MATCH_COUNT_PATTERN = re.compile(r"本轮比赛数量：(\d+) 场。?$")


def format_round_match_count(task_prefix: str, count: int) -> str:
    return f"{task_prefix} 本轮比赛数量：{count} 场。"


def parse_round_match_count(line: str) -> Optional[int]:
    match = ROUND_MATCH_COUNT_PATTERN.search(line)
    return int(match.group(1)) if match else None


@dataclass
class ComponentState:
    key: str
    name: str
    status: str = "starting"
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    next_run_at: Optional[float] = None
    duration_seconds: Optional[float] = None
    exit_code: Optional[int] = None
    round_match_count: Optional[int] = None
    message: str = "等待启动"
    logs: Deque[str] = field(default_factory=deque)


class RuntimeMonitor:
    """Thread-safe bounded state shared by workers and the dashboard."""

    def __init__(
        self,
        components: Iterable[Tuple[str, str]] = COMPONENTS,
        *,
        max_log_lines: int = 400,
    ) -> None:
        if max_log_lines <= 0:
            raise ValueError("max_log_lines 必须大于 0")
        self._lock = threading.Lock()
        self._components: Dict[str, ComponentState] = {
            key: ComponentState(
                key=key,
                name=name,
                logs=deque(maxlen=max_log_lines),
            )
            for key, name in components
        }
        self._daily_statistics: Dict[str, object] = {
            "date": None,
            "match_count": 0,
            "not_started_count": 0,
            "finished_count": 0,
            "in_progress_count": 0,
            "postponed_count": 0,
            "cancelled_count": 0,
            "pending_count": 0,
            "other_status_count": 0,
            "crawl_unfinished_count": 0,
            "crawl_completed_count": 0,
            "abnormal_count": 0,
            "paused_count": 0,
            "finished_unfinished_count": 0,
            "unfinished_not_started_count": 0,
            "unfinished_in_progress_count": 0,
            "unfinished_finished_count": 0,
            "unfinished_postponed_count": 0,
            "unfinished_cancelled_count": 0,
            "unfinished_pending_count": 0,
            "unfinished_other_status_count": 0,
            "historical_match_count": 0,
            "historical_not_started_count": 0,
            "historical_in_progress_count": 0,
            "historical_finished_count": 0,
            "historical_postponed_count": 0,
            "historical_cancelled_count": 0,
            "historical_pending_count": 0,
            "historical_other_status_count": 0,
            "historical_unfinished_count": 0,
            "historical_completed_count": 0,
            "historical_paused_count": 0,
            "historical_abnormal_count": 0,
            "historical_finished_unfinished_count": 0,
            "historical_unfinished_not_started_count": 0,
            "historical_unfinished_in_progress_count": 0,
            "historical_unfinished_finished_count": 0,
            "historical_unfinished_postponed_count": 0,
            "historical_unfinished_cancelled_count": 0,
            "historical_unfinished_pending_count": 0,
            "historical_unfinished_other_status_count": 0,
            "missing_details_count": 0,
            "invalid_scheduled_time_count": 0,
            "backlog": {
                "pending_detail_count": 0,
                "odds_match_count": 0,
                "finished_pending_count": 0,
                "final_page_count": 0,
                "oldest_pending_at": None,
            },
            "problem_matches": [],
            "odds_counts": empty_odds_counts(),
            "updated_at": None,
            "error": "正在读取当日统计",
        }
        self._proxy_health: Dict[str, object] = {
            "pool_size": 0,
            "leased": 0,
            "available_proxies": 0,
            "retired_proxies": 0,
            "available_page_slots": 0,
            "last_batch_received": 0,
            "last_batch_validated": 0,
            "validation_rate": None,
            "updated_at": None,
            "error": "等待代理健康检查",
        }

    def append_log(self, key: str, message: str) -> None:
        timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
        clean_message = message.rstrip("\r\n")
        with self._lock:
            component = self._components[key]
            component.logs.append(f"{timestamp}  {clean_message}")

    def update(self, key: str, **values: object) -> None:
        with self._lock:
            component = self._components[key]
            for name, value in values.items():
                if not hasattr(component, name):
                    raise AttributeError(name)
                setattr(component, name, value)

    def update_daily_statistics(self, statistics: Dict[str, object]) -> None:
        with self._lock:
            self._daily_statistics = {
                **statistics,
                "updated_at": time.time(),
                "error": None,
            }

    def set_daily_statistics_error(self, message: str) -> None:
        with self._lock:
            self._daily_statistics["error"] = message

    def update_proxy_health(self, payload: Dict[str, object]) -> None:
        fields = (
            "pool_size",
            "leased",
            "available_proxies",
            "retired_proxies",
            "available_page_slots",
            "last_batch_received",
            "last_batch_validated",
        )
        values = {name: int(payload.get(name, 0) or 0) for name in fields}
        received = values["last_batch_received"]
        validated = values["last_batch_validated"]
        with self._lock:
            self._proxy_health = {
                **values,
                "validation_rate": validated / received if received else None,
                "updated_at": time.time(),
                "error": None,
            }

    def set_proxy_health_error(self, message: str) -> None:
        with self._lock:
            self._proxy_health["updated_at"] = time.time()
            self._proxy_health["error"] = message

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            components = []
            for component in self._components.values():
                components.append(
                    {
                        "key": component.key,
                        "name": component.name,
                        "status": component.status,
                        "started_at": component.started_at,
                        "finished_at": component.finished_at,
                        "next_run_at": component.next_run_at,
                        "duration_seconds": component.duration_seconds,
                        "exit_code": component.exit_code,
                        "round_match_count": component.round_match_count,
                        "message": component.message,
                        "logs": list(component.logs),
                    }
                )
            daily_statistics = deepcopy(self._daily_statistics)
            proxy_health = deepcopy(self._proxy_health)
        return {
            "generated_at": time.time(),
            "components": components,
            "daily_statistics": daily_statistics,
            "proxy_health": proxy_health,
        }


DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>SimpleCrawler 监控</title>
  <style>
    :root { color-scheme: dark; --bg:#071018; --panel:#0d1a24; --line:#203442;
      --text:#dce9ef; --muted:#8da3ad; --ok:#4ad295; --run:#53b7ff;
      --warn:#f5c451; --bad:#ff6b78; }
    * { box-sizing:border-box; }
    body { margin:0; background:radial-gradient(circle at top,#102634 0,#071018 45%);
      color:var(--text); font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; }
    header { position:sticky; top:0; z-index:2; display:flex; align-items:center;
      justify-content:space-between; gap:16px; padding:18px 24px;
      background:rgba(7,16,24,.9); border-bottom:1px solid var(--line);
      backdrop-filter:blur(12px); }
    h1 { margin:0; font:700 20px/1.2 system-ui,sans-serif; letter-spacing:.02em; }
    .sub,.meta { color:var(--muted); }
    .alerts { display:flex; align-items:center; gap:12px; margin:16px 24px 0;
      padding:10px 12px; border:1px solid var(--line); border-radius:10px;
      background:rgba(13,26,36,.94); }
    .alerts.ok { border-color:#265f49; } .alerts.warn { border-color:#6b5425; }
    .alerts.bad { border-color:#76313a; }
    .alert-title { flex:none; font:650 14px/1.2 system-ui,sans-serif; }
    .alerts.ok .alert-title { color:var(--ok); } .alerts.warn .alert-title { color:var(--warn); }
    .alerts.bad .alert-title { color:var(--bad); }
    .alert-items { display:flex; flex-wrap:wrap; gap:6px; min-width:0; }
    .alert-chip { padding:3px 7px; border-radius:6px; background:#071018; color:var(--muted);
      font-size:12px; }
    .operations { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px;
      align-items:start;
      margin:10px 24px 0; }
    .ops-panel { min-width:0; padding:12px; background:rgba(13,26,36,.94);
      border:1px solid var(--line); border-radius:12px; box-shadow:0 18px 45px #0004; }
    .ops-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:6px; }
    .ops-metric { min-width:0; padding:7px 8px; border-radius:8px; background:#071018;
      border:1px solid var(--line); }
    .ops-metric strong { display:block; margin-top:1px; overflow:hidden; text-overflow:ellipsis;
      color:var(--text); font-size:18px; white-space:nowrap; }
    main { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px;
      padding:16px 24px 28px; }
    article { min-width:0; overflow:hidden; background:rgba(13,26,36,.94);
      border:1px solid var(--line); border-radius:12px; box-shadow:0 18px 45px #0004; }
    .summary { margin:16px 24px 0; padding:12px; background:rgba(13,26,36,.94);
      border:1px solid var(--line); border-radius:12px; box-shadow:0 18px 45px #0004; }
    .summary-head { display:flex; justify-content:space-between; gap:12px;
      align-items:baseline; margin-bottom:8px; }
    .quality,.periods { display:grid; gap:8px; }
    .quality { grid-template-columns:repeat(2,minmax(0,1fr)); margin-bottom:10px; }
    .periods { grid-template-columns:repeat(2,minmax(0,1fr)); }
    .period { padding:10px; border:1px solid var(--line); border-radius:10px;
      background:#09141c; }
    .period-title { display:flex; align-items:baseline; justify-content:space-between;
      gap:8px; margin-bottom:8px; }
    .period-title strong { font:650 15px/1.2 system-ui,sans-serif; }
    .metrics { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:6px; }
    .metric { padding:8px 10px; border:1px solid var(--line); border-radius:9px;
      background:#09141c; }
    .metric strong { display:block; margin-top:1px; color:var(--text); font-size:20px; }
    .period .metric { padding:6px 8px; background:#071018; }
    .period .metric strong { font-size:17px; }
    .unfinished-metrics .metric { border-color:#4b4128; }
    .unfinished-metrics .metric strong { color:var(--warn); }
    .group-title { margin:9px 0 5px; color:var(--muted); font-size:12px; }
    .warning-stat { display:flex; justify-content:space-between; margin-top:7px;
      padding:6px 8px; border:1px solid #6b5425; border-radius:8px; color:var(--warn); }
    table { width:100%; border-collapse:collapse; }
    th,td { padding:8px 10px; border-top:1px solid var(--line); text-align:right; }
    th:first-child,td:first-child { text-align:left; }
    .problem-table th,.problem-table td { text-align:left; vertical-align:top; }
    .problem-table td { padding-top:7px; padding-bottom:7px; }
    .match-id { color:var(--run); font-weight:650; }
    .tags { display:flex; flex-wrap:wrap; gap:4px; }
    .tag { padding:2px 6px; border:1px solid #6b5425; border-radius:99px;
      color:var(--warn); font-size:11px; white-space:nowrap; }
    .problem-error { max-width:360px; margin-top:3px; color:var(--bad); font-size:11px;
      overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .empty-row { padding:16px!important; color:var(--muted); text-align:center!important; }
    .card-head { display:flex; justify-content:space-between; align-items:flex-start;
      gap:12px; padding:14px 16px; border-bottom:1px solid var(--line); }
    .round-count { margin-top:4px; color:var(--run); font-size:12px; }
    h2 { margin:0 0 4px; font:650 16px/1.2 system-ui,sans-serif; }
    .badge { flex:none; padding:4px 9px; border-radius:99px; border:1px solid currentColor;
      font-size:12px; }
    .running { color:var(--run); } .waiting { color:var(--ok); }
    .error { color:var(--bad); } .stopped { color:var(--muted); }
    .starting { color:var(--warn); }
    pre { height:300px; margin:0; padding:14px 16px; overflow:auto; white-space:pre-wrap;
      overflow-wrap:anywhere; background:#050c12; color:#bdd0d8; tab-size:2; }
    footer { display:flex; justify-content:space-between; padding:9px 16px;
      border-top:1px solid var(--line); color:var(--muted); font-size:12px; }
    button { border:1px solid var(--line); border-radius:7px; padding:6px 10px;
      background:#10222e; color:var(--text); cursor:pointer; }
    @media (max-width:1200px) { main { grid-template-columns:repeat(2,minmax(0,1fr)); }
      .ops-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } }
    @media (max-width:900px) { main { grid-template-columns:1fr; padding:14px; }
      header { padding:16px; } .alerts { margin:14px 14px 0; align-items:flex-start; }
      .operations { grid-template-columns:1fr; margin:10px 14px 0; }
      .summary { margin:14px 14px 0; overflow:auto; }
      .periods { grid-template-columns:1fr; } .metrics { grid-template-columns:repeat(3,minmax(0,1fr)); }
      table { min-width:620px; } .problem-table { min-width:850px; } pre { height:260px; } }
  </style>
</head>
<body>
  <header><div><h1>SimpleCrawler 总监控</h1><div class="sub">各采集脚本运行状态与滚动日志</div></div>
    <div><button id="follow">自动滚动：开</button> <span id="clock" class="meta">连接中…</span></div></header>
  <section id="alerts" class="alerts warn"><strong id="alert-title" class="alert-title">状态读取中</strong>
    <div id="alert-items" class="alert-items"><span class="alert-chip">正在汇总监控状态…</span></div></section>
  <section class="operations">
    <div class="ops-panel"><div class="summary-head"><h2>采集积压</h2><span id="backlog-meta" class="meta">读取中…</span></div>
      <div class="ops-grid">
        <div class="ops-metric"><span class="meta">待获取详情</span><strong id="backlog-detail-count">—</strong></div>
        <div class="ops-metric"><span class="meta">待抓赔率比赛</span><strong id="backlog-odds-count">—</strong></div>
        <div class="ops-metric"><span class="meta">完场待核验</span><strong id="backlog-finished-count">—</strong></div>
        <div class="ops-metric"><span class="meta">待补最终页面</span><strong id="backlog-final-page-count">—</strong></div>
      </div></div>
    <div class="ops-panel"><div class="summary-head"><h2>代理池状态</h2><span id="proxy-meta" class="meta">等待检查…</span></div>
      <div class="ops-grid">
        <div class="ops-metric"><span class="meta">当前代理</span><strong id="proxy-pool-size">—</strong></div>
        <div class="ops-metric"><span class="meta">可用 / 已租用</span><strong id="proxy-availability">—</strong></div>
        <div class="ops-metric"><span class="meta">隔离代理</span><strong id="proxy-retired">—</strong></div>
        <div class="ops-metric"><span class="meta">可用页面槽位</span><strong id="proxy-slots">—</strong></div>
        <div class="ops-metric"><span class="meta">最近获取</span><strong id="proxy-received">—</strong></div>
        <div class="ops-metric"><span class="meta">最近验证通过</span><strong id="proxy-validated">—</strong></div>
        <div class="ops-metric"><span class="meta">验证通过率</span><strong id="proxy-rate">—</strong></div>
      </div></div>
  </section>
  <section class="summary"><div class="summary-head"><h2>比赛数据统计</h2><span id="stats-meta" class="meta">读取中…</span></div>
    <div class="quality"><div class="metric"><span class="meta">待获取详情</span><strong id="missing-details-count">—</strong></div>
      <div class="metric"><span class="meta">时间异常</span><strong id="invalid-scheduled-time-count">—</strong></div></div>
    <div class="periods">
      <div class="period"><div class="period-title"><strong>今日比赛</strong><span id="today-date" class="meta">—</span></div>
        <div class="metric"><span class="meta">比赛总数</span><strong id="match-count">—</strong></div>
        <div class="group-title">比赛状态</div><div class="metrics">
          <div class="metric"><span class="meta">未开始</span><strong id="not-started-count">—</strong></div>
          <div class="metric"><span class="meta">进行中</span><strong id="in-progress-count">—</strong></div>
          <div class="metric"><span class="meta">完场</span><strong id="finished-count">—</strong></div>
          <div class="metric"><span class="meta">推迟</span><strong id="postponed-count">—</strong></div>
          <div class="metric"><span class="meta">取消</span><strong id="cancelled-count">—</strong></div>
          <div class="metric"><span class="meta">待定</span><strong id="pending-count">—</strong></div>
          <div class="metric"><span class="meta">其他状态</span><strong id="other-status-count">—</strong></div></div>
        <div class="group-title">爬取状态</div><div class="metrics">
          <div class="metric"><span class="meta">未完成</span><strong id="crawl-unfinished-count">—</strong></div>
          <div class="metric"><span class="meta">已完成</span><strong id="crawl-completed-count">—</strong></div>
          <div class="metric"><span class="meta">暂停爬取</span><strong id="paused-count">—</strong></div>
          <div class="metric"><span class="meta">异常</span><strong id="abnormal-count">—</strong></div></div>
        <div class="group-title">未完成对应比赛状态</div><div class="metrics unfinished-metrics">
          <div class="metric"><span class="meta">未开始</span><strong id="unfinished-not-started-count">—</strong></div>
          <div class="metric"><span class="meta">进行中</span><strong id="unfinished-in-progress-count">—</strong></div>
          <div class="metric"><span class="meta">完场</span><strong id="unfinished-finished-count">—</strong></div>
          <div class="metric"><span class="meta">推迟</span><strong id="unfinished-postponed-count">—</strong></div>
          <div class="metric"><span class="meta">取消</span><strong id="unfinished-cancelled-count">—</strong></div>
          <div class="metric"><span class="meta">待定</span><strong id="unfinished-pending-count">—</strong></div>
          <div class="metric"><span class="meta">其他状态</span><strong id="unfinished-other-status-count">—</strong></div></div>
        <div class="warning-stat"><span>完场但爬取未完成</span><strong id="finished-unfinished-count">—</strong></div></div>
      <div class="period"><div class="period-title"><strong>历史比赛</strong><span id="historical-date" class="meta">今日之前</span></div>
        <div class="metric"><span class="meta">比赛总数</span><strong id="historical-match-count">—</strong></div>
        <div class="group-title">比赛状态</div><div class="metrics">
          <div class="metric"><span class="meta">未开始</span><strong id="historical-not-started-count">—</strong></div>
          <div class="metric"><span class="meta">进行中</span><strong id="historical-in-progress-count">—</strong></div>
          <div class="metric"><span class="meta">完场</span><strong id="historical-finished-count">—</strong></div>
          <div class="metric"><span class="meta">推迟</span><strong id="historical-postponed-count">—</strong></div>
          <div class="metric"><span class="meta">取消</span><strong id="historical-cancelled-count">—</strong></div>
          <div class="metric"><span class="meta">待定</span><strong id="historical-pending-count">—</strong></div>
          <div class="metric"><span class="meta">其他状态</span><strong id="historical-other-status-count">—</strong></div></div>
        <div class="group-title">爬取状态</div><div class="metrics">
          <div class="metric"><span class="meta">未完成</span><strong id="historical-unfinished-count">—</strong></div>
          <div class="metric"><span class="meta">已完成</span><strong id="historical-completed-count">—</strong></div>
          <div class="metric"><span class="meta">暂停爬取</span><strong id="historical-paused-count">—</strong></div>
          <div class="metric"><span class="meta">异常</span><strong id="historical-abnormal-count">—</strong></div></div>
        <div class="group-title">未完成对应比赛状态</div><div class="metrics unfinished-metrics">
          <div class="metric"><span class="meta">未开始</span><strong id="historical-unfinished-not-started-count">—</strong></div>
          <div class="metric"><span class="meta">进行中</span><strong id="historical-unfinished-in-progress-count">—</strong></div>
          <div class="metric"><span class="meta">完场</span><strong id="historical-unfinished-finished-count">—</strong></div>
          <div class="metric"><span class="meta">推迟</span><strong id="historical-unfinished-postponed-count">—</strong></div>
          <div class="metric"><span class="meta">取消</span><strong id="historical-unfinished-cancelled-count">—</strong></div>
          <div class="metric"><span class="meta">待定</span><strong id="historical-unfinished-pending-count">—</strong></div>
          <div class="metric"><span class="meta">其他状态</span><strong id="historical-unfinished-other-status-count">—</strong></div></div>
        <div class="warning-stat"><span>完场但爬取未完成</span><strong id="historical-finished-unfinished-count">—</strong></div></div>
    </div>
    <div class="group-title">今日赔率变动记录</div>
    <table><thead><tr><th>公司</th><th>亚让</th><th>胜平负</th><th>进球数</th><th>合计</th></tr></thead>
      <tbody id="odds-counts"></tbody><tfoot id="odds-totals"></tfoot></table>
  </section>
  <section class="summary"><div class="summary-head"><h2>问题比赛</h2><span class="meta">最多显示 20 场</span></div>
    <table class="problem-table"><thead><tr><th>比赛 ID</th><th>对阵</th><th>开赛时间</th><th>比赛状态</th><th>爬取状态</th><th>问题</th></tr></thead>
      <tbody id="problem-matches"><tr><td class="empty-row" colspan="6">读取中…</td></tr></tbody></table></section>
  <main id="grid"></main>
  <script>
    const grid=document.querySelector('#grid'), clock=document.querySelector('#clock');
    const followButton=document.querySelector('#follow'); let follow=true;
    followButton.onclick=()=>{follow=!follow;followButton.textContent=`自动滚动：${follow?'开':'关'}`};
    const statusText={starting:'启动中',running:'运行中',waiting:'等待下轮',error:'异常',stopped:'已停止'};
    const fmt=t=>t?new Date(t*1000).toLocaleTimeString('zh-CN',{hour12:false}):'—';
    const number=value=>Number(value||0).toLocaleString('zh-CN');
    const setText=(id,value)=>document.querySelector(`#${id}`).textContent=value;
    function backlog(s){const b=s.backlog||{};
      setText('backlog-detail-count',number(b.pending_detail_count));
      setText('backlog-odds-count',number(b.odds_match_count));
      setText('backlog-finished-count',number(b.finished_pending_count));
      setText('backlog-final-page-count',number(b.final_page_count));
      const oldest=b.oldest_pending_at?new Date(b.oldest_pending_at).toLocaleString('zh-CN',{hour12:false}):'无积压';
      setText('backlog-meta',`最早 ${oldest}`);
    }
    function proxyHealth(h){h=h||{};
      setText('proxy-pool-size',number(h.pool_size));
      setText('proxy-availability',`${number(h.available_proxies)} / ${number(h.leased)}`);
      setText('proxy-retired',number(h.retired_proxies));setText('proxy-slots',number(h.available_page_slots));
      setText('proxy-received',number(h.last_batch_received));setText('proxy-validated',number(h.last_batch_validated));
      setText('proxy-rate',h.validation_rate===null||h.validation_rate===undefined?'—':`${(h.validation_rate*100).toFixed(0)}%`);
      setText('proxy-meta',h.error?`检查失败：${h.error}`:`更新 ${fmt(h.updated_at)}`);
    }
    function problemMatches(rows){const body=document.querySelector('#problem-matches');body.replaceChildren();
      if(!rows||!rows.length){const tr=document.createElement('tr'),td=document.createElement('td');
        td.colSpan=6;td.className='empty-row';td.textContent='当前没有问题比赛';tr.appendChild(td);body.appendChild(tr);return}
      rows.forEach(row=>{const tr=document.createElement('tr');
        const add=(value,className='')=>{const td=document.createElement('td');td.textContent=value||'—';td.className=className;tr.appendChild(td);return td};
        add(String(row.match_id),'match-id');add([row.league,[row.home_team,row.away_team].filter(Boolean).join(' vs ')].filter(Boolean).join(' · '));
        add(row.scheduled_time);add(row.status_text);add(row.crawl_status);
        const issue=document.createElement('td');issue.className='tags';tr.appendChild(issue);(row.problems||[]).forEach(text=>{const tag=document.createElement('span');tag.className='tag';tag.textContent=text;issue.appendChild(tag)});
        if(row.last_error){const error=document.createElement('div');error.className='problem-error';error.textContent=row.last_error;error.title=row.last_error;issue.appendChild(error)}
        body.appendChild(tr)});
    }
    function alerts(data){const s=data.daily_statistics||{},b=s.backlog||{},h=data.proxy_health||{},items=[];let severity='ok';
      const add=(text,level='warn')=>{items.push(text);if(level==='bad'||severity==='ok')severity=level};
      (data.components||[]).filter(c=>c.status==='error').forEach(c=>add(`${c.name}异常`,'bad'));
      if(s.error)add(`数据库统计失败：${s.error}`,'bad');if(h.error)add(`代理检查失败：${h.error}`,'bad');
      const terminal=Number(s.abnormal_count||0)+Number(s.paused_count||0)+Number(s.historical_abnormal_count||0)+Number(s.historical_paused_count||0);
      if(terminal)add(`${number(terminal)} 场处于异常或暂停`,'bad');
      if(b.finished_pending_count)add(`${number(b.finished_pending_count)} 场完场待核验`);
      if(b.pending_detail_count)add(`${number(b.pending_detail_count)} 场缺少详情`);
      if(s.invalid_scheduled_time_count)add(`${number(s.invalid_scheduled_time_count)} 场时间异常`);
      if(!h.error&&h.updated_at&&Number(h.available_proxies||0)===0)add('当前没有可用代理','bad');
      const box=document.querySelector('#alerts'),list=document.querySelector('#alert-items');box.className=`alerts ${severity}`;list.replaceChildren();
      setText('alert-title',items.length?`${items.length} 项需要关注`:'运行正常');
      (items.length?items:['任务、数据库与代理状态正常']).forEach(text=>{const chip=document.createElement('span');chip.className='alert-chip';chip.textContent=text;list.appendChild(chip)});
    }
    function statistics(s){
      const show=(id,key)=>document.querySelector(`#${id}`).textContent=Number(s[key]||0).toLocaleString('zh-CN');
      const fields={
        'match-count':'match_count','not-started-count':'not_started_count','in-progress-count':'in_progress_count',
        'finished-count':'finished_count','postponed-count':'postponed_count','cancelled-count':'cancelled_count',
        'pending-count':'pending_count','other-status-count':'other_status_count','crawl-unfinished-count':'crawl_unfinished_count',
        'crawl-completed-count':'crawl_completed_count','paused-count':'paused_count','abnormal-count':'abnormal_count',
        'finished-unfinished-count':'finished_unfinished_count','historical-match-count':'historical_match_count',
        'historical-not-started-count':'historical_not_started_count','historical-in-progress-count':'historical_in_progress_count',
        'historical-finished-count':'historical_finished_count','historical-postponed-count':'historical_postponed_count',
        'historical-cancelled-count':'historical_cancelled_count','historical-pending-count':'historical_pending_count',
        'historical-other-status-count':'historical_other_status_count','historical-unfinished-count':'historical_unfinished_count',
        'historical-completed-count':'historical_completed_count','historical-paused-count':'historical_paused_count',
        'historical-abnormal-count':'historical_abnormal_count','historical-finished-unfinished-count':'historical_finished_unfinished_count',
        'unfinished-not-started-count':'unfinished_not_started_count','unfinished-in-progress-count':'unfinished_in_progress_count',
        'unfinished-finished-count':'unfinished_finished_count','unfinished-postponed-count':'unfinished_postponed_count',
        'unfinished-cancelled-count':'unfinished_cancelled_count','unfinished-pending-count':'unfinished_pending_count',
        'unfinished-other-status-count':'unfinished_other_status_count',
        'historical-unfinished-not-started-count':'historical_unfinished_not_started_count',
        'historical-unfinished-in-progress-count':'historical_unfinished_in_progress_count',
        'historical-unfinished-finished-count':'historical_unfinished_finished_count',
        'historical-unfinished-postponed-count':'historical_unfinished_postponed_count',
        'historical-unfinished-cancelled-count':'historical_unfinished_cancelled_count',
        'historical-unfinished-pending-count':'historical_unfinished_pending_count',
        'historical-unfinished-other-status-count':'historical_unfinished_other_status_count',
        'missing-details-count':'missing_details_count','invalid-scheduled-time-count':'invalid_scheduled_time_count'};
      Object.entries(fields).forEach(([id,key])=>show(id,key));
      document.querySelector('#today-date').textContent=s.date||'—';
      document.querySelector('#stats-meta').textContent=s.error?`读取失败：${s.error}`:`${s.date} · 更新 ${fmt(s.updated_at)}`;
      const odds=s.odds_counts||[], n=value=>Number(value||0), f=value=>n(value).toLocaleString('zh-CN');
      document.querySelector('#odds-counts').innerHTML=odds.map(c=>
        `<tr><td>公司 ${c.company_id}（${c.company_name}）</td><td>${f(c.handicap)}</td><td>${f(c.one_x_two)}</td><td>${f(c.over_under)}</td><td>${f(n(c.handicap)+n(c.one_x_two)+n(c.over_under))}</td></tr>`).join('');
      const totals=odds.reduce((a,c)=>[a[0]+n(c.handicap),a[1]+n(c.one_x_two),a[2]+n(c.over_under)],[0,0,0]);
      document.querySelector('#odds-totals').innerHTML=`<tr><th>合计</th><th>${f(totals[0])}</th><th>${f(totals[1])}</th><th>${f(totals[2])}</th><th>${f(totals.reduce((a,b)=>a+b,0))}</th></tr>`;
      backlog(s);problemMatches(s.problem_matches||[]);
    }
    function card(c){let el=document.querySelector(`[data-key="${c.key}"]`);
      if(!el){el=document.createElement('article');el.dataset.key=c.key;
        el.innerHTML='<div class="card-head"><div><h2></h2><div class="meta message"></div><div class="round-count" hidden></div></div><span class="badge"></span></div><pre></pre><footer><span class="timing"></span><span class="exit"></span></footer>';
        grid.appendChild(el)}
      el.querySelector('h2').textContent=c.name; const badge=el.querySelector('.badge');
      badge.className=`badge ${c.status}`;badge.textContent=statusText[c.status]||c.status;
      el.querySelector('.message').textContent=c.message||'';
      const roundCount=el.querySelector('.round-count'), hasRoundCount=c.round_match_count!==null&&c.round_match_count!==undefined;
      roundCount.hidden=!hasRoundCount;roundCount.textContent=hasRoundCount?`本轮比赛 ${number(c.round_match_count)} 场`:'';
      const log=el.querySelector('pre'), atBottom=log.scrollHeight-log.scrollTop-log.clientHeight<36;
      log.textContent=c.logs.length?c.logs.join('\\n'):'尚无日志';
      if(follow&&atBottom)log.scrollTop=log.scrollHeight;
      el.querySelector('.timing').textContent=['waiting','error'].includes(c.status)?`下轮 ${fmt(c.next_run_at)}`:`开始 ${fmt(c.started_at)}`;
      el.querySelector('.exit').textContent=c.exit_code===null?'':`退出码 ${c.exit_code} · ${Number(c.duration_seconds||0).toFixed(1)}s`;
    }
    async function refresh(){try{const response=await fetch('api/status',{cache:'no-store'});
      if(!response.ok)throw new Error(`HTTP ${response.status}`);const data=await response.json();
      data.components.forEach(card);statistics(data.daily_statistics);proxyHealth(data.proxy_health);alerts(data);clock.textContent=`更新 ${fmt(data.generated_at)}`;
    }catch(error){clock.textContent=`连接失败：${error.message}`}}
    refresh();setInterval(refresh,1000);
  </script>
</body>
</html>"""


class DashboardServer:
    """Serve the dashboard and JSON state on a background thread."""

    def __init__(self, monitor: RuntimeMonitor, host: str, port: int) -> None:
        self.monitor = monitor
        handler = self._handler_type(monitor)
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread: Optional[threading.Thread] = None

    @staticmethod
    def _handler_type(monitor: RuntimeMonitor):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                if self.path == "/":
                    self._send(200, "text/html; charset=utf-8", DASHBOARD_HTML.encode())
                elif self.path == "/api/status":
                    payload = json.dumps(
                        monitor.snapshot(), ensure_ascii=False
                    ).encode("utf-8")
                    self._send(200, "application/json; charset=utf-8", payload)
                else:
                    self._send(404, "text/plain; charset=utf-8", "Not found".encode())

            def _send(self, status: int, content_type: str, payload: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        return Handler

    @property
    def address(self) -> Tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="simple-crawler-dashboard",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        if self._thread is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
        self._thread = None
