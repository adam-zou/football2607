"""In-process metrics and health HTTP endpoint for the long-running collector."""

import asyncio
import json
import threading
import time
from dataclasses import dataclass
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

    async def start_server(self, host: str, port: int) -> asyncio.AbstractServer:
        return await asyncio.start_server(self._handle_http, host, port)

    async def _handle_http(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            parts = request_line.decode("ascii", errors="replace").split()
            path = parts[1] if len(parts) >= 2 else ""
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
