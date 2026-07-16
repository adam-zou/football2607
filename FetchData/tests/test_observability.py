import asyncio
import json
import unittest

from fetch_data.observability import RuntimeObservability


class RuntimeObservabilityTests(unittest.TestCase):
    def test_metrics_include_counters_gauges_durations_and_health(self) -> None:
        observability = RuntimeObservability()
        observability.increment("fetch_attempts_total", task="match_list")
        observability.set_gauge("queue_pending", 12, queue="match_detail")
        observability.observe("fetch_duration_seconds", 1.5, task="match_list")
        observability.record_health("database", True)

        metrics = observability.render_metrics()

        self.assertIn(
            'football_fetch_attempts_total{task="match_list"} 1', metrics
        )
        self.assertIn(
            'football_queue_pending{queue="match_detail"} 12', metrics
        )
        self.assertIn(
            'football_fetch_duration_seconds_latest{task="match_list"} 1.5',
            metrics,
        )
        self.assertIn(
            'football_component_healthy{component="database"} 1', metrics
        )
        self.assertIn(
            'football_component_consecutive_failures{component="database"} 0',
            metrics,
        )

    def test_health_is_starting_then_degraded_then_ok(self) -> None:
        observability = RuntimeObservability()
        self.assertEqual(observability.health()[0], 503)

        observability.record_health("database", False, "connection failed")
        status, payload = observability.health()
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(
            payload["components"]["database"]["consecutive_failures"], 1
        )

        observability.record_health("database", True)
        status, payload = observability.health()
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_http_server_exposes_health_and_metrics(self) -> None:
        async def exercise() -> None:
            observability = RuntimeObservability()
            observability.record_health("database", True)
            server = await observability.start_server("127.0.0.1", 0)
            try:
                port = server.sockets[0].getsockname()[1]
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                response = await reader.read()
                writer.close()
                await writer.wait_closed()
            finally:
                server.close()
                await server.wait_closed()

            headers, body = response.split(b"\r\n\r\n", 1)
            self.assertIn(b"200 OK", headers)
            self.assertEqual(json.loads(body)["status"], "ok")

        asyncio.run(exercise())

    def test_http_server_exposes_human_readable_dashboard(self) -> None:
        async def exercise() -> None:
            observability = RuntimeObservability()
            observability.record_health("database", True)
            observability.record_health("match_list", False, "列表页面超时")
            observability.increment(
                "fetch_success_total", amount=3, task="match_list"
            )
            observability.increment("fetch_failure_total", task="match_list")
            observability.set_gauge("queue_pending", 12, queue="match_detail")
            server = await observability.start_server("127.0.0.1", 0)
            try:
                port = server.sockets[0].getsockname()[1]
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                response = await reader.read()
                writer.close()
                await writer.wait_closed()
            finally:
                server.close()
                await server.wait_closed()

            headers, body = response.split(b"\r\n\r\n", 1)
            page = body.decode("utf-8")
            self.assertIn(b"200 OK", headers)
            self.assertIn(b"text/html", headers)
            self.assertIn("足球数据采集状态", page)
            self.assertIn("部分异常", page)
            self.assertIn("比赛列表", page)
            self.assertIn("从列表页发现新的比赛 ID", page)
            self.assertIn("首次补充联赛和球队等基础信息", page)
            self.assertIn("列表页面超时", page)
            self.assertIn("成功 3 次", page)
            self.assertIn("失败 1 次", page)
            self.assertIn("待处理 12 场", page)

        asyncio.run(exercise())

    def test_idle_http_connection_does_not_raise_unhandled_timeout(self) -> None:
        async def exercise() -> None:
            loop = asyncio.get_running_loop()
            unhandled = []
            previous_handler = loop.get_exception_handler()
            loop.set_exception_handler(
                lambda _loop, context: unhandled.append(context)
            )
            observability = RuntimeObservability()
            server = await observability.start_server("127.0.0.1", 0)
            try:
                port = server.sockets[0].getsockname()[1]
                _reader, writer = await asyncio.open_connection(
                    "127.0.0.1", port
                )
                await asyncio.sleep(5.1)
                writer.close()
                await writer.wait_closed()
                await asyncio.sleep(0)
            finally:
                server.close()
                await server.wait_closed()
                loop.set_exception_handler(previous_handler)

            self.assertEqual(unhandled, [])

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
