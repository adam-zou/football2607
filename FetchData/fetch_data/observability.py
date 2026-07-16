"""In-process metrics and health HTTP endpoint for the long-running collector."""

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from html import escape
from typing import Dict, Mapping, Optional, Tuple


LabelKey = Tuple[Tuple[str, str], ...]
MetricKey = Tuple[str, LabelKey]


@dataclass(frozen=True)
class ComponentHealth:
    healthy: bool
    checked_at: float
    message: str = ""
    last_success_at: Optional[float] = None
    consecutive_failures: int = 0


class RuntimeObservability:
    """Hide metric storage, Prometheus rendering, and HTTP handling behind one seam."""

    def __init__(self) -> None:
        self.started_at = time.time()
        self._counters: Dict[MetricKey, float] = {}
        self._gauges: Dict[MetricKey, float] = {}
        self._components: Dict[str, ComponentHealth] = {}
        self._lock = threading.Lock()

    def increment(
        self,
        name: str,
        amount: float = 1.0,
        **labels: str,
    ) -> None:
        key = self._metric_key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + amount

    def set_gauge(self, name: str, value: float, **labels: str) -> None:
        key = self._metric_key(name, labels)
        with self._lock:
            self._gauges[key] = float(value)

    def observe(self, name: str, value: float, **labels: str) -> None:
        self.increment(f"{name}_count", **labels)
        self.increment(f"{name}_sum", value, **labels)
        self.set_gauge(f"{name}_latest", value, **labels)

    def record_health(
        self,
        component: str,
        healthy: bool,
        message: str = "",
    ) -> None:
        with self._lock:
            previous = self._components.get(component)
            now = time.time()
            self._components[component] = ComponentHealth(
                healthy=healthy,
                checked_at=now,
                message=message,
                last_success_at=(
                    now
                    if healthy
                    else previous.last_success_at
                    if previous is not None
                    else None
                ),
                consecutive_failures=(
                    0
                    if healthy
                    else (previous.consecutive_failures if previous else 0) + 1
                ),
            )

    def render_metrics(self) -> str:
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            components = dict(self._components)

        lines = [
            "# HELP football_process_uptime_seconds Collector process uptime.",
            "# TYPE football_process_uptime_seconds gauge",
            f"football_process_uptime_seconds {time.time() - self.started_at:.3f}",
        ]
        emitted_types = set()
        for (name, labels), value in sorted(counters.items()):
            if name not in emitted_types:
                lines.append(f"# TYPE {name} counter")
                emitted_types.add(name)
            lines.append(f"{name}{self._render_labels(labels)} {value:g}")
        for (name, labels), value in sorted(gauges.items()):
            if name not in emitted_types:
                lines.append(f"# TYPE {name} gauge")
                emitted_types.add(name)
            lines.append(f"{name}{self._render_labels(labels)} {value:g}")
        if components:
            lines.extend(
                (
                    "# TYPE football_component_healthy gauge",
                    "# TYPE football_component_last_check_unixtime gauge",
                    "# TYPE football_component_consecutive_failures gauge",
                    "# TYPE football_component_last_success_unixtime gauge",
                )
            )
        for component, health in sorted(components.items()):
            labels = (("component", component),)
            lines.append(
                "football_component_healthy"
                f"{self._render_labels(labels)} {1 if health.healthy else 0}"
            )
            lines.append(
                "football_component_last_check_unixtime"
                f"{self._render_labels(labels)} {health.checked_at:.3f}"
            )
            lines.append(
                "football_component_consecutive_failures"
                f"{self._render_labels(labels)} {health.consecutive_failures}"
            )
            if health.last_success_at is not None:
                lines.append(
                    "football_component_last_success_unixtime"
                    f"{self._render_labels(labels)} {health.last_success_at:.3f}"
                )
        return "\n".join(lines) + "\n"

    def health(self) -> Tuple[int, Dict[str, object]]:
        with self._lock:
            components = dict(self._components)
        if not components:
            status = "starting"
            code = 503
        elif all(item.healthy for item in components.values()):
            status = "ok"
            code = 200
        else:
            status = "degraded"
            code = 503
        return code, {
            "status": status,
            "uptime_seconds": round(time.time() - self.started_at, 3),
            "components": {
                name: {
                    "healthy": item.healthy,
                    "checked_at": item.checked_at,
                    "last_success_at": item.last_success_at,
                    "consecutive_failures": item.consecutive_failures,
                    "message": item.message,
                }
                for name, item in sorted(components.items())
            },
        }

    def render_dashboard(self) -> str:
        """Render a dependency-free status page intended for operators."""

        _status_code, payload = self.health()
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)

        components = payload["components"]
        unhealthy = [
            item
            for item in components.values()
            if not item["healthy"] and item["message"] != "not started"
        ]
        waiting = [
            item
            for item in components.values()
            if not item["healthy"] and item["message"] == "not started"
        ]
        if payload["status"] == "ok":
            overall_class, overall_title, overall_text = (
                "ok",
                "运行正常",
                "所有组件最近一次检查均成功",
            )
        elif waiting and not unhealthy:
            overall_class, overall_title, overall_text = (
                "waiting",
                "正在启动",
                "部分任务正在进行首次检查，请稍候",
            )
        else:
            overall_class, overall_title, overall_text = (
                "error",
                "部分异常",
                "采集进程仍在运行，请查看下方异常组件",
            )

        component_labels = {
            "database": ("数据库", "PostgreSQL 连接与写入"),
            "proxy": ("代理", "代理获取与连通性验证"),
            "match_list": ("比赛列表", "从列表页发现新的比赛 ID"),
            "match_detail": ("比赛详情", "首次补充联赛和球队等基础信息"),
            "match_dynamic": ("动态信息", "更新开赛时间、比分和比赛状态"),
            "match_odds": ("赔率数据", "抓取并保存三类赔率变化"),
        }
        cards = []
        now = time.time()
        for name in component_labels:
            label, description = component_labels[name]
            item = components.get(name)
            if item is None or item["message"] == "not started":
                card_class = "waiting"
                state = "启动中"
                message = "等待首次检查"
                checked = "尚未检查"
                failures = 0 if item is None else item["consecutive_failures"]
            elif item["healthy"]:
                card_class = "ok"
                state = "正常"
                message = "最近一次检查成功"
                checked = self._relative_time(now, item["checked_at"])
                failures = item["consecutive_failures"]
            else:
                card_class = "error"
                state = "异常"
                message = item["message"] or "最近一次检查失败"
                checked = self._relative_time(now, item["checked_at"])
                failures = item["consecutive_failures"]
            cards.append(
                f"""
                <article class="component-card {card_class}">
                  <div class="component-heading">
                    <div><h3>{escape(label)}</h3><p>{escape(description)}</p></div>
                    <span class="badge">{state}</span>
                  </div>
                  <p class="message">{escape(str(message))}</p>
                  <div class="meta"><span>{escape(checked)}</span><span>连续失败 {failures} 次</span></div>
                </article>
                """
            )

        task_rows = []
        task_labels = {
            "match_list": ("比赛列表", None),
            "match_detail": ("比赛详情", "match_detail"),
            "match_dynamic": ("动态信息", "match_dynamic"),
            "match_odds": ("赔率数据", "match_odds"),
        }
        for task, (label, queue) in task_labels.items():
            successes = self._metric_value(
                counters, "fetch_success_total", task=task
            )
            failures = self._metric_value(
                counters, "fetch_failure_total", task=task
            )
            duration = self._metric_value(
                gauges, "fetch_duration_seconds_latest", task=task
            )
            pending = (
                self._metric_value(gauges, "queue_pending", queue=queue)
                if queue
                else None
            )
            pending_text = (
                "—" if pending is None else f"待处理 {pending:g} 场"
            )
            task_rows.append(
                "<tr>"
                f"<th>{escape(label)}</th>"
                f"<td class=\"success\">成功 {successes:g} 次</td>"
                f"<td class=\"failure\">失败 {failures:g} 次</td>"
                f"<td>{pending_text}</td>"
                f"<td>{duration:.2f} 秒</td>"
                "</tr>"
            )

        queue_labels = {
            "match_detail": "静态详情",
            "match_dynamic": "动态信息",
            "match_odds": "赔率核验",
        }
        queue_items = []
        for queue, label in queue_labels.items():
            value = self._metric_value(gauges, "queue_pending", queue=queue)
            queue_items.append(
                f'<div class="queue"><span>{label}</span><strong>{value:g}</strong><small>场待处理</small></div>'
            )

        uptime = self._format_duration(float(payload["uptime_seconds"]))
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="10">
  <title>足球数据采集状态</title>
  <style>
    :root {{ color-scheme: light; --bg:#f4f7fb; --card:#fff; --text:#172033; --muted:#68738a; --line:#e4e9f2; --ok:#168a55; --ok-bg:#eaf8f1; --waiting:#ad6b00; --waiting-bg:#fff6df; --error:#c43d4b; --error-bg:#fff0f1; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
    main {{ width:min(1120px,calc(100% - 32px)); margin:32px auto 56px; }}
    header {{ display:flex; justify-content:space-between; align-items:flex-end; gap:20px; margin-bottom:20px; }}
    h1 {{ margin:0 0 4px; font-size:28px; }} h2 {{ margin:0 0 16px; font-size:19px; }} h3 {{ margin:0; font-size:17px; }}
    p {{ margin:0; }} .muted,.component-heading p,.meta {{ color:var(--muted); }}
    .overall {{ padding:24px; border-radius:18px; background:var(--card); border:1px solid var(--line); display:flex; align-items:center; gap:18px; margin-bottom:20px; box-shadow:0 8px 30px rgba(26,45,80,.06); }}
    .overall::before {{ content:""; width:14px; height:64px; flex:none; border-radius:99px; background:var(--ok); }}
    .overall.waiting::before {{ background:var(--waiting); }} .overall.error::before {{ background:var(--error); }}
    .overall strong {{ display:block; font-size:24px; margin-bottom:2px; }} .overall .runtime {{ margin-left:auto; text-align:right; color:var(--muted); }} .runtime b {{ display:block; color:var(--text); font-size:18px; }}
    .section {{ background:var(--card); border:1px solid var(--line); border-radius:18px; padding:22px; margin-top:20px; }}
    .components {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:14px; }}
    .component-card {{ border:1px solid var(--line); border-radius:14px; padding:17px; min-width:0; }}
    .component-card.ok {{ border-color:#bde7d1; }} .component-card.waiting {{ border-color:#f0d59c; }} .component-card.error {{ border-color:#f0b6bc; }}
    .component-heading {{ display:flex; justify-content:space-between; gap:12px; }} .component-heading p {{ font-size:13px; margin-top:3px; }}
    .badge {{ height:25px; padding:2px 9px; border-radius:99px; font-size:13px; color:var(--ok); background:var(--ok-bg); white-space:nowrap; }}
    .waiting .badge {{ color:var(--waiting); background:var(--waiting-bg); }} .error .badge {{ color:var(--error); background:var(--error-bg); }}
    .message {{ margin:16px 0; min-height:24px; overflow-wrap:anywhere; }} .meta {{ border-top:1px solid var(--line); padding-top:11px; display:flex; justify-content:space-between; gap:12px; font-size:12px; }}
    .queues {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-bottom:20px; }} .queue {{ background:#f7f9fc; border-radius:12px; padding:15px; }} .queue span,.queue small {{ display:block; color:var(--muted); }} .queue strong {{ font-size:26px; margin-right:5px; }} .queue small {{ display:inline; }}
    .table-wrap {{ overflow-x:auto; }} table {{ width:100%; border-collapse:collapse; min-width:680px; }} th,td {{ text-align:left; padding:12px; border-top:1px solid var(--line); }} thead th {{ border-top:0; color:var(--muted); font-weight:500; }} tbody th {{ font-weight:600; }} .success {{ color:var(--ok); }} .failure {{ color:var(--error); }}
    footer {{ display:flex; justify-content:space-between; gap:16px; margin-top:18px; color:var(--muted); font-size:13px; }} a {{ color:#3867d6; text-decoration:none; }}
    @media(max-width:650px) {{ main {{ width:min(100% - 20px,1120px); margin-top:18px; }} header,.overall,footer {{ align-items:flex-start; flex-direction:column; }} .overall .runtime {{ margin-left:32px; text-align:left; }} .queues {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
<main>
  <header><div><h1>足球数据采集状态</h1><p class="muted">自动汇总数据库、代理和采集任务状态</p></div><p class="muted">页面每 10 秒自动刷新</p></header>
  <section class="overall {overall_class}"><div><strong>{overall_title}</strong><p>{overall_text}</p></div><div class="runtime">已连续运行<b>{uptime}</b></div></section>
  <section class="section"><h2>组件状态</h2><div class="components">{''.join(cards)}</div></section>
  <section class="section"><h2>待处理队列</h2><div class="queues">{''.join(queue_items)}</div><h2>本次运行统计</h2><div class="table-wrap"><table><thead><tr><th>任务</th><th>成功</th><th>失败</th><th>队列</th><th>最近耗时</th></tr></thead><tbody>{''.join(task_rows)}</tbody></table></div></section>
  <footer><span>只要顶部显示“运行正常”，采集服务就处于健康状态。</span><span><a href="/healthz">原始健康数据</a> · <a href="/metrics">Prometheus 指标</a></span></footer>
</main>
</body>
</html>"""

    async def start_server(self, host: str, port: int) -> asyncio.AbstractServer:
        return await asyncio.start_server(self._handle_http, host, port)

    async def _handle_http(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            try:
                request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            except asyncio.TimeoutError:
                return
            parts = request_line.decode("ascii", errors="replace").split()
            path = parts[1].partition("?")[0] if len(parts) >= 2 else ""
            if path == "/metrics":
                status, content_type, body = (
                    200,
                    "text/plain; version=0.0.4; charset=utf-8",
                    self.render_metrics().encode("utf-8"),
                )
            elif path == "/healthz":
                status, payload = self.health()
                content_type = "application/json; charset=utf-8"
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            elif path in {"/", "/dashboard"}:
                status = 200
                content_type = "text/html; charset=utf-8"
                body = self.render_dashboard().encode("utf-8")
            else:
                status = 404
                content_type = "text/plain; charset=utf-8"
                body = b"not found\n"
            reason = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}[status]
            writer.write(
                f"HTTP/1.1 {status} {reason}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n".encode("ascii")
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    @classmethod
    def _metric_value(
        cls,
        metrics: Mapping[MetricKey, float],
        name: str,
        **labels: str,
    ) -> float:
        return metrics.get(cls._metric_key(name, labels), 0.0)

    @staticmethod
    def _relative_time(now: float, checked_at: float) -> str:
        seconds = max(0, int(now - checked_at))
        if seconds < 5:
            return "刚刚检查"
        if seconds < 60:
            return f"{seconds} 秒前检查"
        return f"{seconds // 60} 分钟前检查"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, remaining_seconds = divmod(remainder, 60)
        if hours:
            return f"{hours} 小时 {minutes} 分"
        if minutes:
            return f"{minutes} 分 {remaining_seconds} 秒"
        return f"{remaining_seconds} 秒"

    @staticmethod
    def _metric_key(name: str, labels: Mapping[str, str]) -> MetricKey:
        if not name.startswith("football_"):
            name = f"football_{name}"
        return name, tuple(sorted((key, str(value)) for key, value in labels.items()))

    @staticmethod
    def _render_labels(labels: LabelKey) -> str:
        if not labels:
            return ""
        rendered = ",".join(
            f'{key}="{RuntimeObservability._escape_label(value)}"'
            for key, value in labels
        )
        return "{" + rendered + "}"

    @staticmethod
    def _escape_label(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
