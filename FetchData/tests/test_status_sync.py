import asyncio
import unittest
from typing import AsyncIterator, List, Sequence
from unittest.mock import patch

from fetch_data.models import Match, MatchBasicInfo, MatchStatus
from fetch_data.status_sync import MatchSynchronizer


class FakeStore:
    def __init__(self) -> None:
        self.initialized = False
        self.closed = False
        self.matches = {}
        self.details = {}
        self.detail_batch_sizes: List[int] = []

    async def initialize(self) -> None:
        self.initialized = True

    async def upsert_match_list(self, matches: Sequence[Match]) -> None:
        for match in matches:
            self.matches[int(match.match_id)] = match

    async def fetch_pending_match_ids(self) -> List[int]:
        return list(range(1, 13))

    async def upsert_match_details(
        self,
        details: Sequence[MatchBasicInfo],
    ) -> None:
        self.detail_batch_sizes.append(len(details))
        for detail in details:
            self.details[detail.match_id] = detail

    async def close(self) -> None:
        self.closed = True


class FakeMatchList:
    async def fetch_matches(self) -> List[Match]:
        return [
            Match(
                source="titan007",
                match_id="100",
                league="测试联赛",
                home_team="主队",
                away_team="客队",
                score=None,
                home_score=None,
                away_score=None,
                status=MatchStatus.SCHEDULED,
                status_text="",
                scheduled_time="20:00",
            )
        ]


class FakeMatchDetails:
    def __init__(self) -> None:
        self.requested_match_ids: List[int] = []
        self.batch_size = 0

    async def fetch_match_detail_batches(
        self,
        match_ids: Sequence[int],
        *,
        batch_size: int,
    ) -> AsyncIterator[List[MatchBasicInfo]]:
        self.requested_match_ids = list(match_ids)
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


class StopAfterBothTasksSleep:
    def __init__(self) -> None:
        self.calls = 0
        self.both_tasks_waiting = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.calls += 1
        if self.calls == 2:
            self.both_tasks_waiting.set()
        await self.both_tasks_waiting.wait()
        raise asyncio.CancelledError


class MatchSyncTests(unittest.TestCase):
    def test_list_and_detail_tasks_run_independently(self) -> None:
        store = FakeStore()
        match_details = FakeMatchDetails()
        synchronizer = MatchSynchronizer(
            store=store,
            match_list=FakeMatchList(),
            match_details=match_details,
            list_refresh_seconds=60,
            detail_refresh_seconds=60,
            detail_batch_size=10,
        )

        async def run_one_iteration_of_each_task() -> None:
            sleep = StopAfterBothTasksSleep()
            with patch("fetch_data.status_sync.asyncio.sleep", new=sleep):
                with self.assertRaises(asyncio.CancelledError):
                    await synchronizer.run()

        asyncio.run(run_one_iteration_of_each_task())

        self.assertTrue(store.initialized)
        self.assertTrue(store.closed)
        self.assertEqual(list(store.matches), [100])
        self.assertEqual(match_details.requested_match_ids, list(range(1, 13)))
        self.assertEqual(match_details.batch_size, 10)
        self.assertEqual(store.detail_batch_sizes, [10, 2])
        self.assertEqual(store.details[12].home_team, "主队12")


if __name__ == "__main__":
    unittest.main()
