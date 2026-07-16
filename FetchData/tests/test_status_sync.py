import asyncio
import unittest
from typing import AsyncIterator, List, Sequence
from unittest.mock import patch

from fetch_data.models import (
    MatchBasicInfo,
    OddsMarketRequest,
    OddsMarketResult,
    OddsSnapshot,
)
from fetch_data.status_sync import MatchSynchronizer


class FakeStore:
    def __init__(self) -> None:
        self.initialized = False
        self.closed = False
        self.matches = {}
        self.details = {}
        self.detail_batch_sizes: List[int] = []
        self.dynamic_details = {}
        self.dynamic_pending_ids = [50]
        self.dynamic_attempts: List[int] = []
        self.dynamic_failures = {}

    async def initialize(self) -> None:
        self.initialized = True

    async def upsert_match_list(self, match_ids: Sequence[int]) -> None:
        for match_id in match_ids:
            self.matches[int(match_id)] = True

    async def fetch_pending_detail_ids(self) -> List[int]:
        return list(range(1, 13))

    async def fetch_pending_dynamic_ids(self, limit: int) -> List[int]:
        selected = self.dynamic_pending_ids[:limit]
        self.dynamic_pending_ids = self.dynamic_pending_ids[limit:]
        return selected

    async def begin_dynamic_attempts(self, match_ids: Sequence[int]) -> None:
        self.dynamic_attempts.extend(match_ids)

    async def upsert_match_details(
        self,
        details: Sequence[MatchBasicInfo],
    ) -> None:
        self.detail_batch_sizes.append(len(details))
        for detail in details:
            self.details[detail.match_id] = detail

    async def upsert_match_dynamics(
        self,
        details: Sequence[MatchBasicInfo],
    ) -> None:
        for detail in details:
            self.dynamic_details[detail.match_id] = detail

    async def record_dynamic_failures(
        self,
        match_ids: Sequence[int],
        error: str,
    ) -> None:
        for match_id in match_ids:
            self.dynamic_failures[match_id] = error

    async def close(self) -> None:
        self.closed = True


class FakeMatchList:
    async def fetch_match_ids(self) -> List[int]:
        return [100]


class FakeMatchDetails:
    def __init__(self) -> None:
        self.requested_match_ids: List[int] = []
        self.requests: List[List[int]] = []
        self.batch_size = 0

    async def fetch_match_detail_batches(
        self,
        match_ids: Sequence[int],
        *,
        batch_size: int,
    ) -> AsyncIterator[List[MatchBasicInfo]]:
        self.requested_match_ids = list(match_ids)
        self.requests.append(list(match_ids))
        self.batch_size = batch_size
        for start in range(0, len(match_ids), batch_size):
            yield [
                MatchBasicInfo(
                    source="titan007",
                    match_id=match_id,
                    league="测试联赛",
                    home_team=f"主队{match_id}",
                    away_team=f"客队{match_id}",
                    scheduled_time="2026-07-13 20:00",
                    home_score=None,
                    away_score=None,
                    status_text="未开始",
                )
                for match_id in match_ids[start : start + batch_size]
            ]


class FailingDynamicDetails(FakeMatchDetails):
    async def fetch_match_detail_batches(
        self,
        match_ids: Sequence[int],
        *,
        batch_size: int,
    ) -> AsyncIterator[List[MatchBasicInfo]]:
        self.requests.append(list(match_ids))
        raise RuntimeError("proxy unavailable")
        yield []


class FakeOddsStore:
    def __init__(self) -> None:
        self.initialized = False
        self.closed = False
        self.requested_limits: List[int] = []
        self.attempted_match_ids: List[int] = []
        self.succeeded_match_ids: List[int] = []
        self.failed_matches = {}
        self.snapshots: List[OddsSnapshot] = []
        self.market_outcomes: List[OddsSnapshot] = []
        self.pending_ids = [200]

    async def initialize(self) -> None:
        self.initialized = True

    async def fetch_pending_match_ids(self, limit: int) -> List[int]:
        self.requested_limits.append(limit)
        selected = self.pending_ids[:limit]
        self.pending_ids = self.pending_ids[limit:]
        return selected

    async def count_pending_match_ids(self) -> int:
        return 1

    async def upsert_snapshot(self, snapshot: OddsSnapshot) -> None:
        self.snapshots.append(snapshot)

    async def begin_match_attempt(self, match_id: int) -> List[OddsMarketRequest]:
        self.attempted_match_ids.append(match_id)
        return [OddsMarketRequest(3, "handicap")]

    async def record_market_outcomes(self, snapshot: OddsSnapshot) -> None:
        self.market_outcomes.append(snapshot)
        if not snapshot.failed_markets:
            self.succeeded_match_ids.append(snapshot.match_id)

    async def record_market_failures(
        self,
        match_id: int,
        requests: Sequence[OddsMarketRequest],
        error: str,
    ) -> None:
        self.failed_matches[match_id] = error

    async def close(self) -> None:
        self.closed = True


class FakeMatchOdds:
    def __init__(self) -> None:
        self.requested_match_ids: List[int] = []
        self.proxy_refreshes = 0

    async def fetch_match_odds(
        self,
        match_id: int,
        market_requests: Sequence[OddsMarketRequest],
    ) -> OddsSnapshot:
        self.requested_match_ids.append(match_id)
        return OddsSnapshot(
            match_id=match_id,
            companies={3: "Crow*"},
            handicap_changes=[],
            one_x_two_changes=[],
            over_under_changes=[],
            market_results=[
                OddsMarketResult(request, True)
                for request in market_requests
            ],
        )

    async def refresh_proxy(self) -> None:
        self.proxy_refreshes += 1


class StopAfterAllTasksSleep:
    def __init__(self) -> None:
        self.calls = 0
        self.all_tasks_waiting = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.calls += 1
        if self.calls == 4:
            self.all_tasks_waiting.set()
        await self.all_tasks_waiting.wait()
        raise asyncio.CancelledError


class MatchSyncTests(unittest.TestCase):
    def test_dynamic_batch_exception_records_failure_for_every_claimed_match(
        self,
    ) -> None:
        store = FakeStore()
        store.dynamic_pending_ids = [50, 51]
        synchronizer = MatchSynchronizer(
            store=store,
            match_list=FakeMatchList(),
            match_details=FailingDynamicDetails(),
            odds_store=FakeOddsStore(),
            match_odds=FakeMatchOdds(),
            dynamic_batch_size=10,
        )

        async def stop_after_failure(seconds: float) -> None:
            raise asyncio.CancelledError

        async def run_one_iteration() -> None:
            with patch(
                "fetch_data.status_sync.asyncio.sleep",
                new=stop_after_failure,
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await synchronizer._run_match_dynamic_task()

        asyncio.run(run_one_iteration())

        self.assertEqual(store.dynamic_attempts, [50, 51])
        self.assertEqual(
            store.dynamic_failures,
            {50: "proxy unavailable", 51: "proxy unavailable"},
        )

    def test_list_detail_and_odds_tasks_run_independently(self) -> None:
        store = FakeStore()
        match_details = FakeMatchDetails()
        odds_store = FakeOddsStore()
        match_odds = FakeMatchOdds()
        synchronizer = MatchSynchronizer(
            store=store,
            match_list=FakeMatchList(),
            match_details=match_details,
            odds_store=odds_store,
            match_odds=match_odds,
            list_refresh_seconds=60,
            detail_refresh_seconds=60,
            detail_batch_size=10,
            odds_refresh_seconds=5,
            odds_batch_size=1,
        )

        async def run_one_iteration_of_each_task() -> None:
            sleep = StopAfterAllTasksSleep()
            with patch("fetch_data.status_sync.asyncio.sleep", new=sleep):
                with self.assertRaises(asyncio.CancelledError):
                    await synchronizer.run()

        asyncio.run(run_one_iteration_of_each_task())

        self.assertTrue(store.initialized)
        self.assertTrue(store.closed)
        self.assertTrue(odds_store.initialized)
        self.assertTrue(odds_store.closed)
        self.assertEqual(list(store.matches), [100])
        self.assertIn(list(range(1, 13)), match_details.requests)
        self.assertIn([50], match_details.requests)
        self.assertEqual(match_details.batch_size, 10)
        self.assertEqual(store.detail_batch_sizes, [10, 2])
        self.assertEqual(store.details[12].home_team, "主队12")
        self.assertEqual(store.dynamic_details[50].home_team, "主队50")
        self.assertEqual(store.dynamic_attempts, [50])
        self.assertEqual(odds_store.requested_limits, [1, 1])
        self.assertEqual(odds_store.attempted_match_ids, [200])
        self.assertEqual(odds_store.succeeded_match_ids, [200])
        self.assertEqual(match_odds.requested_match_ids, [200])
        self.assertEqual(odds_store.snapshots[0].match_id, 200)
        metrics = synchronizer.observability.render_metrics()
        self.assertIn(
            'football_queue_pending{queue="match_detail"} 12', metrics
        )
        self.assertIn(
            'football_queue_pending{queue="match_odds"} 1', metrics
        )
        self.assertIn(
            'football_queue_pending{queue="match_dynamic"} 0', metrics
        )

    def test_odds_matches_use_configured_concurrency(self) -> None:
        class ConcurrentOddsStore(FakeOddsStore):
            async def fetch_pending_match_ids(self, limit: int) -> List[int]:
                self.requested_limits.append(limit)
                selected = self.pending_ids[:limit]
                self.pending_ids = self.pending_ids[limit:]
                return selected

            async def count_pending_match_ids(self) -> int:
                return 6

        class ConcurrentMatchOdds(FakeMatchOdds):
            def __init__(self) -> None:
                super().__init__()
                self.active = 0
                self.maximum_active = 0
                self.three_started = None

            async def fetch_match_odds(
                self,
                match_id: int,
                market_requests: Sequence[OddsMarketRequest],
            ) -> OddsSnapshot:
                self.requested_match_ids.append(match_id)
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
                if self.three_started is None:
                    self.three_started = asyncio.Event()
                if self.active == 3:
                    self.three_started.set()
                await self.three_started.wait()
                self.active -= 1
                return OddsSnapshot(
                    match_id=match_id,
                    companies={3: "Crow*"},
                    handicap_changes=[],
                    one_x_two_changes=[],
                    over_under_changes=[],
                    market_results=[
                        OddsMarketResult(request, True)
                        for request in market_requests
                    ],
                )

        odds_store = ConcurrentOddsStore()
        odds_store.pending_ids = [1, 2, 3, 4, 5, 6]
        match_odds = ConcurrentMatchOdds()
        synchronizer = MatchSynchronizer(
            store=FakeStore(),
            match_list=FakeMatchList(),
            match_details=FakeMatchDetails(),
            odds_store=odds_store,
            match_odds=match_odds,
            odds_batch_size=6,
            odds_match_concurrency=3,
        )

        async def stop_after_one_iteration(seconds: float) -> None:
            raise asyncio.CancelledError

        async def run_one_iteration() -> None:
            with patch(
                "fetch_data.status_sync.asyncio.sleep",
                new=stop_after_one_iteration,
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await synchronizer._run_match_odds_task()

        asyncio.run(run_one_iteration())

        self.assertEqual(match_odds.maximum_active, 3)
        self.assertEqual(match_odds.requested_match_ids, [1, 2, 3, 4, 5, 6])
        self.assertEqual(sorted(odds_store.succeeded_match_ids), [1, 2, 3, 4, 5, 6])

    def test_slow_match_does_not_block_later_queue_refills(self) -> None:
        class RefillableOddsStore(FakeOddsStore):
            def __init__(self) -> None:
                super().__init__()
                self.pending_ids = list(range(1, 9))

            async def count_pending_match_ids(self) -> int:
                return len(self.pending_ids)

        class OneSlowMatch(FakeMatchOdds):
            def __init__(self) -> None:
                super().__init__()
                self.release_slow = None

            async def fetch_match_odds(
                self,
                match_id: int,
                market_requests: Sequence[OddsMarketRequest],
            ) -> OddsSnapshot:
                if self.release_slow is None:
                    self.release_slow = asyncio.Event()
                self.requested_match_ids.append(match_id)
                if match_id == 1:
                    await self.release_slow.wait()
                if match_id == 8:
                    self.release_slow.set()
                return OddsSnapshot(
                    match_id=match_id,
                    companies={3: "Crow*"},
                    handicap_changes=[],
                    one_x_two_changes=[],
                    over_under_changes=[],
                    market_results=[
                        OddsMarketResult(request, True)
                        for request in market_requests
                    ],
                )

        odds_store = RefillableOddsStore()
        match_odds = OneSlowMatch()
        synchronizer = MatchSynchronizer(
            store=FakeStore(),
            match_list=FakeMatchList(),
            match_details=FakeMatchDetails(),
            odds_store=odds_store,
            match_odds=match_odds,
            odds_batch_size=6,
            odds_match_concurrency=3,
        )

        async def stop_when_idle(seconds: float) -> None:
            raise asyncio.CancelledError

        async def run_until_idle() -> None:
            with patch(
                "fetch_data.status_sync.asyncio.sleep",
                new=stop_when_idle,
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await synchronizer._run_match_odds_task()

        asyncio.run(run_until_idle())

        self.assertIn(8, match_odds.requested_match_ids)
        self.assertEqual(sorted(odds_store.succeeded_match_ids), list(range(1, 9)))

    def test_match_level_timeout_releases_stuck_worker(self) -> None:
        class HangingMatchOdds(FakeMatchOdds):
            async def fetch_match_odds(
                self,
                match_id: int,
                market_requests: Sequence[OddsMarketRequest],
            ) -> OddsSnapshot:
                self.requested_match_ids.append(match_id)
                await asyncio.Future()
                raise AssertionError("unreachable")

        odds_store = FakeOddsStore()
        match_odds = HangingMatchOdds()
        synchronizer = MatchSynchronizer(
            store=FakeStore(),
            match_list=FakeMatchList(),
            match_details=FakeMatchDetails(),
            odds_store=odds_store,
            match_odds=match_odds,
            odds_match_timeout_seconds=0.001,
        )

        async def stop_when_idle(seconds: float) -> None:
            raise asyncio.CancelledError

        async def run_until_idle() -> None:
            with patch(
                "fetch_data.status_sync.asyncio.sleep",
                new=stop_when_idle,
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await synchronizer._run_match_odds_task()

        asyncio.run(run_until_idle())

        self.assertIn("超过", odds_store.failed_matches[200])

    def test_three_consecutive_full_failures_force_proxy_refresh(self) -> None:
        class FailingMatchOdds(FakeMatchOdds):
            async def fetch_match_odds(
                self,
                match_id: int,
                market_requests: Sequence[OddsMarketRequest],
            ) -> OddsSnapshot:
                self.requested_match_ids.append(match_id)
                raise RuntimeError("proxy unavailable")

        odds_store = FakeOddsStore()
        odds_store.pending_ids = [1, 2, 3]
        match_odds = FailingMatchOdds()
        synchronizer = MatchSynchronizer(
            store=FakeStore(),
            match_list=FakeMatchList(),
            match_details=FakeMatchDetails(),
            odds_store=odds_store,
            match_odds=match_odds,
            odds_match_concurrency=3,
        )

        async def stop_when_idle(seconds: float) -> None:
            raise asyncio.CancelledError

        async def run_until_idle() -> None:
            with patch(
                "fetch_data.status_sync.asyncio.sleep",
                new=stop_when_idle,
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await synchronizer._run_match_odds_task()

        asyncio.run(run_until_idle())

        self.assertEqual(match_odds.proxy_refreshes, 1)

    def test_database_failures_do_not_force_proxy_refresh(self) -> None:
        class FailingStore(FakeOddsStore):
            async def upsert_snapshot(self, snapshot: OddsSnapshot) -> None:
                raise RuntimeError("database unavailable")

        odds_store = FailingStore()
        odds_store.pending_ids = [1, 2, 3]
        match_odds = FakeMatchOdds()
        synchronizer = MatchSynchronizer(
            store=FakeStore(),
            match_list=FakeMatchList(),
            match_details=FakeMatchDetails(),
            odds_store=odds_store,
            match_odds=match_odds,
            odds_match_concurrency=3,
        )

        async def stop_when_idle(seconds: float) -> None:
            raise asyncio.CancelledError

        async def run_until_idle() -> None:
            with patch(
                "fetch_data.status_sync.asyncio.sleep",
                new=stop_when_idle,
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await synchronizer._run_match_odds_task()

        asyncio.run(run_until_idle())

        self.assertEqual(match_odds.proxy_refreshes, 0)


if __name__ == "__main__":
    unittest.main()
