import asyncio
import unittest

from concurrent_pages import async_proxy_lease, iter_bounded


class FakeLease:
    def __init__(self) -> None:
        self.failed = None

    def __enter__(self):
        return "proxy"

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.failed = exc_type is not None


class FakeProxyClient:
    def __init__(self) -> None:
        self.last_lease = None
        self.page_assignments = None

    def lease(
        self,
        *,
        min_remaining_seconds: float,
        page_assignments: int = 1,
    ) -> FakeLease:
        self.page_assignments = page_assignments
        self.last_lease = FakeLease()
        return self.last_lease


class BoundedSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_limits_active_jobs_and_yields_completion_order(self) -> None:
        active = 0
        maximum_active = 0

        async def handle(job: int) -> int:
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                await asyncio.sleep((4 - job) * 0.001)
                return job * 10
            finally:
                active -= 1

        outcomes = [
            outcome
            async for outcome in iter_bounded([1, 2, 3], 2, handle)
        ]

        self.assertEqual(maximum_active, 2)
        self.assertEqual(outcomes[0].job, 2)
        self.assertEqual({outcome.job for outcome in outcomes}, {1, 2, 3})
        self.assertEqual(
            {outcome.job: outcome.result for outcome in outcomes},
            {1: 10, 2: 20, 3: 30},
        )

    async def test_one_failure_does_not_stop_remaining_jobs(self) -> None:
        async def handle(job: int) -> int:
            if job == 2:
                raise RuntimeError("broken page")
            return job

        outcomes = [
            outcome
            async for outcome in iter_bounded([1, 2, 3], 2, handle)
        ]
        by_job = {outcome.job: outcome for outcome in outcomes}

        self.assertIsNone(by_job[1].error)
        self.assertIsInstance(by_job[2].error, RuntimeError)
        self.assertIsNone(by_job[3].error)

    async def test_rejects_non_positive_concurrency(self) -> None:
        async def handle(job: int) -> int:
            return job

        with self.assertRaisesRegex(ValueError, "greater than zero"):
            async for _ in iter_bounded([1], 0, handle):
                pass

    async def test_proxy_lease_marks_failed_page(self) -> None:
        client = FakeProxyClient()

        with self.assertRaisesRegex(RuntimeError, "page failed"):
            async with async_proxy_lease(
                client,
                min_remaining_seconds=5,
            ) as proxy:
                self.assertEqual(proxy, "proxy")
                raise RuntimeError("page failed")

        self.assertTrue(client.last_lease.failed)

    async def test_proxy_lease_forwards_page_assignments(self) -> None:
        client = FakeProxyClient()

        async with async_proxy_lease(
            client,
            min_remaining_seconds=5,
            page_assignments=3,
        ):
            pass

        self.assertEqual(client.page_assignments, 3)


if __name__ == "__main__":
    unittest.main()
