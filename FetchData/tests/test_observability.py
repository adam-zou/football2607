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


if __name__ == "__main__":
    unittest.main()
