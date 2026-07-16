"""In-memory runtime state and HTTP dashboard for SimpleCrawler."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Deque, Dict, Iterable, Optional, Tuple


COMPONENTS: Tuple[Tuple[str, str], ...] = (
    ("proxy_scheduler", "代理服务"),
    ("fetch_match_ids", "比赛 ID"),
    ("fetch_match_details", "比赛详情"),
    ("fetch_odds_pages", "赔率变化"),
    ("check_match_completion", "完成核验"),
)


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
                        "message": component.message,
                        "logs": list(component.logs),
                    }
                )
        return {"generated_at": time.time(), "components": components}


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
    main { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px;
      padding:20px 24px 32px; }
    article { min-width:0; overflow:hidden; background:rgba(13,26,36,.94);
      border:1px solid var(--line); border-radius:12px; box-shadow:0 18px 45px #0004; }
    .card-head { display:flex; justify-content:space-between; align-items:flex-start;
      gap:12px; padding:14px 16px; border-bottom:1px solid var(--line); }
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
    @media (max-width:900px) { main { grid-template-columns:1fr; padding:14px; }
      header { padding:16px; } pre { height:260px; } }
  </style>
</head>
<body>
  <header><div><h1>SimpleCrawler 总监控</h1><div class="sub">各采集脚本运行状态与滚动日志</div></div>
    <div><button id="follow">自动滚动：开</button> <span id="clock" class="meta">连接中…</span></div></header>
  <main id="grid"></main>
  <script>
    const grid=document.querySelector('#grid'), clock=document.querySelector('#clock');
    const followButton=document.querySelector('#follow'); let follow=true;
    followButton.onclick=()=>{follow=!follow;followButton.textContent=`自动滚动：${follow?'开':'关'}`};
    const statusText={starting:'启动中',running:'运行中',waiting:'等待下轮',error:'异常',stopped:'已停止'};
    const fmt=t=>t?new Date(t*1000).toLocaleTimeString('zh-CN',{hour12:false}):'—';
    function card(c){let el=document.querySelector(`[data-key="${c.key}"]`);
      if(!el){el=document.createElement('article');el.dataset.key=c.key;
        el.innerHTML='<div class="card-head"><div><h2></h2><div class="meta message"></div></div><span class="badge"></span></div><pre></pre><footer><span class="timing"></span><span class="exit"></span></footer>';
        grid.appendChild(el)}
      el.querySelector('h2').textContent=c.name; const badge=el.querySelector('.badge');
      badge.className=`badge ${c.status}`;badge.textContent=statusText[c.status]||c.status;
      el.querySelector('.message').textContent=c.message||'';
      const log=el.querySelector('pre'), atBottom=log.scrollHeight-log.scrollTop-log.clientHeight<36;
      log.textContent=c.logs.length?c.logs.join('\\n'):'尚无日志';
      if(follow&&atBottom)log.scrollTop=log.scrollHeight;
      el.querySelector('.timing').textContent=['waiting','error'].includes(c.status)?`下轮 ${fmt(c.next_run_at)}`:`开始 ${fmt(c.started_at)}`;
      el.querySelector('.exit').textContent=c.exit_code===null?'':`退出码 ${c.exit_code} · ${Number(c.duration_seconds||0).toFixed(1)}s`;
    }
    async function refresh(){try{const response=await fetch('/api/status',{cache:'no-store'});
      if(!response.ok)throw new Error(`HTTP ${response.status}`);const data=await response.json();
      data.components.forEach(card);clock.textContent=`更新 ${fmt(data.generated_at)}`;
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
