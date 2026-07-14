"""持续同步的调度层。

本模块只决定“何时抓、何时写”，不关心网页和 SQL 的实现细节。五个 Protocol
描述它所需要的最小接口，因此测试时可以传入简单的假对象。
"""

import asyncio
import logging
import time
from typing import AsyncIterator, List, Optional, Protocol, Sequence

from .models import Match, MatchBasicInfo, OddsSnapshot
from .observability import RuntimeObservability


logger = logging.getLogger(__name__)


class MatchStore(Protocol):
    """同步器所需的持久化接口；当前正式实现是 PostgresMatchStore。"""

    async def initialize(self) -> None:
        ...

    async def upsert_match_list(self, matches: Sequence[Match]) -> None:
        ...

    async def fetch_pending_detail_ids(self) -> List[int]:
        ...

    async def fetch_final_status_repair_ids(self) -> List[int]:
        ...

    async def upsert_match_details(
        self,
        details: Sequence[MatchBasicInfo],
    ) -> None:
        ...

    async def repair_final_statuses(
        self,
        details: Sequence[MatchBasicInfo],
    ) -> None:
        ...

    async def close(self) -> None:
        ...


class MatchListSnapshot(Protocol):
    """比赛列表抓取器需要满足的最小接口。"""

    async def fetch_matches(self) -> List[Match]:
        ...


class MatchDetailSnapshot(Protocol):
    """比赛详情抓取器需要满足的最小接口。"""

    def fetch_match_detail_batches(
        self,
        match_ids: Sequence[int],
        *,
        batch_size: int,
    ) -> AsyncIterator[List[MatchBasicInfo]]:
        ...


class OddsStore(Protocol):
    """赔率循环所需的持久化接口；正式实现是 PostgresOddsStore。"""

    async def initialize(self) -> None:
        ...

    async def fetch_pending_match_ids(self, limit: int) -> List[int]:
        ...

    async def count_pending_match_ids(self) -> int:
        ...

    async def touch_match_attempt(self, match_id: int) -> None:
        ...

    async def upsert_snapshot(self, snapshot: OddsSnapshot) -> None:
        ...

    async def close(self) -> None:
        ...


class MatchOddsSnapshot(Protocol):
    """完整抓取一场比赛六家公司、三个市场的最小接口。"""

    async def fetch_match_odds(self, match_id: int) -> OddsSnapshot:
        ...


class MatchSynchronizer:
    """让列表、详情和赔率抓取作为三个互不等待的任务运行。"""

    def __init__(
        self,
        store: MatchStore,
        match_list: MatchListSnapshot,
        match_details: MatchDetailSnapshot,
        odds_store: OddsStore,
        match_odds: MatchOddsSnapshot,
        *,
        list_refresh_seconds: float = 60.0,
        detail_refresh_seconds: float = 60.0,
        detail_batch_size: int = 10,
        odds_refresh_seconds: float = 5.0,
        odds_batch_size: int = 1,
        observability: Optional[RuntimeObservability] = None,
    ) -> None:
        if list_refresh_seconds <= 0:
            raise ValueError("list_refresh_seconds must be greater than zero")
        if detail_refresh_seconds <= 0:
            raise ValueError("detail_refresh_seconds must be greater than zero")
        if detail_batch_size <= 0:
            raise ValueError("detail_batch_size must be greater than zero")
        if odds_refresh_seconds <= 0:
            raise ValueError("odds_refresh_seconds must be greater than zero")
        if odds_batch_size <= 0:
            raise ValueError("odds_batch_size must be greater than zero")
        self.store = store
        self.match_list = match_list
        self.match_details = match_details
        self.odds_store = odds_store
        self.match_odds = match_odds
        self.list_refresh_seconds = list_refresh_seconds
        self.detail_refresh_seconds = detail_refresh_seconds
        self.detail_batch_size = detail_batch_size
        self.odds_refresh_seconds = odds_refresh_seconds
        self.odds_batch_size = odds_batch_size
        self.observability = observability or RuntimeObservability()

    async def run(self) -> None:
        """初始化数据库，启动三个常驻任务，并保证退出时释放资源。"""

        for component in ("database", "match_list", "match_detail", "match_odds"):
            self.observability.record_health(component, False, "not started")
        try:
            await self.store.initialize()
            await self.odds_store.initialize()
        except Exception as error:
            self.observability.record_health("database", False, str(error))
            raise
        self.observability.record_health("database", True)
        try:
            # 三个无限循环由事件循环独立推进；详情或赔率页慢，不会阻塞列表刷新。
            tasks = [
                asyncio.create_task(self._run_match_list_task()),
                asyncio.create_task(self._run_match_detail_task()),
                asyncio.create_task(self._run_match_odds_task()),
            ]
            try:
                await asyncio.gather(*tasks)
            finally:
                # 任一任务异常退出或程序收到取消信号时，其余任务也必须停止。
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            try:
                await self.odds_store.close()
            finally:
                await self.store.close()

    async def _run_match_list_task(self) -> None:
        """周期抓列表：发现新 ID，并刷新已有详情的时间、比分和状态。"""

        while True:
            started = time.monotonic()
            self.observability.increment("fetch_attempts_total", task="match_list")
            try:
                matches = await self.match_list.fetch_matches()
                await self.store.upsert_match_list(matches)
                self.observability.increment(
                    "fetch_success_total", task="match_list"
                )
                self.observability.set_gauge(
                    "matches_last_snapshot", len(matches)
                )
                self.observability.record_health("match_list", True)
                logger.info("refreshed %d matches from the list page", len(matches))
            except asyncio.CancelledError:
                # CancelledError 是正常关机信号，不能被下面的兜底异常吞掉。
                raise
            except Exception as error:
                self.observability.increment(
                    "fetch_failure_total", task="match_list"
                )
                self.observability.record_health("match_list", False, str(error))
                logger.exception("match-list refresh failed")
            finally:
                self.observability.observe(
                    "fetch_duration_seconds",
                    time.monotonic() - started,
                    task="match_list",
                )
            # 间隔从本轮结束后计算，并非固定整点调度。
            await asyncio.sleep(self.list_refresh_seconds)

    async def _run_match_detail_task(self) -> None:
        """周期处理数据库中仍标记为“未完成”的详情任务。"""

        while True:
            started = time.monotonic()
            self.observability.increment("fetch_attempts_total", task="match_detail")
            try:
                match_ids = await self.store.fetch_pending_detail_ids()
                stored = 0
                self.observability.set_gauge(
                    "queue_pending", len(match_ids), queue="match_detail"
                )
                if match_ids:
                    # Provider 分批产出结果，成功一批就落库一批，避免必须等所有
                    # 详情页全部抓完才保存。
                    async for details in (
                        self.match_details.fetch_match_detail_batches(
                            match_ids,
                            batch_size=self.detail_batch_size,
                        )
                    ):
                        await self.store.upsert_match_details(details)
                        stored += len(details)
                        logger.info(
                            "stored detail batch: %d rows (%d/%d total)",
                            len(details),
                            stored,
                            len(match_ids),
                        )
                    logger.info(
                        "stored %d of %d pending match details",
                        stored,
                        len(match_ids),
                    )
                repair_ids = await self.store.fetch_final_status_repair_ids()
                self.observability.set_gauge(
                    "queue_pending",
                    len(repair_ids),
                    queue="final_status_repair",
                )
                selected_repairs = repair_ids[: self.detail_batch_size]
                repaired = 0
                if selected_repairs:
                    async for details in (
                        self.match_details.fetch_match_detail_batches(
                            selected_repairs,
                            batch_size=self.detail_batch_size,
                        )
                    ):
                        await self.store.repair_final_statuses(details)
                        repaired += len(details)
                    logger.info(
                        "checked %d of %d stale final statuses",
                        repaired,
                        len(repair_ids),
                    )

                detail_healthy = stored == len(match_ids)
                repair_healthy = repaired == len(selected_repairs)
                if detail_healthy and repair_healthy:
                    self.observability.increment(
                        "fetch_success_total", task="match_detail"
                    )
                    self.observability.record_health("match_detail", True)
                else:
                    self.observability.increment(
                        "fetch_failure_total", task="match_detail"
                    )
                    self.observability.record_health(
                        "match_detail",
                        False,
                        f"stored {stored} of {len(match_ids)} details; "
                        f"repaired {repaired} of {len(selected_repairs)} finals",
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.observability.increment(
                    "fetch_failure_total", task="match_detail"
                )
                self.observability.record_health("match_detail", False, str(error))
                logger.exception("match-detail refresh failed")
            finally:
                self.observability.observe(
                    "fetch_duration_seconds",
                    time.monotonic() - started,
                    task="match_detail",
                )
            await asyncio.sleep(self.detail_refresh_seconds)

    async def _run_match_odds_task(self) -> None:
        """周期处理赔率尚未完成核验的比赛。"""

        while True:
            started = time.monotonic()
            self.observability.increment("fetch_attempts_total", task="match_odds")
            try:
                pending_count = await self.odds_store.count_pending_match_ids()
                self.observability.set_gauge(
                    "queue_pending", pending_count, queue="match_odds"
                )
                match_ids = await self.odds_store.fetch_pending_match_ids(
                    self.odds_batch_size
                )
                all_succeeded = True
                for match_id in match_ids:
                    try:
                        # 抓取失败也要轮转队列，避免同一坏页面永久占住队首。
                        await self.odds_store.touch_match_attempt(match_id)
                        snapshot = await self.match_odds.fetch_match_odds(match_id)
                        await self.odds_store.upsert_snapshot(snapshot)
                        self.observability.increment(
                            "odds_matches_success_total"
                        )
                        if snapshot.failed_companies:
                            all_succeeded = False
                            self.observability.record_health(
                                "match_odds",
                                False,
                                "partial company failure: "
                                + ",".join(
                                    str(value)
                                    for value in snapshot.failed_companies
                                ),
                            )
                        logger.info(
                            "stored odds for match %d: companies=%s, failed=%s, "
                            "handicap=%d, 1x2=%d, over_under=%d",
                            match_id,
                            list(snapshot.companies),
                            list(snapshot.failed_companies),
                            len(snapshot.handicap_changes),
                            len(snapshot.one_x_two_changes),
                            len(snapshot.over_under_changes),
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        all_succeeded = False
                        self.observability.increment(
                            "odds_matches_failure_total"
                        )
                        self.observability.record_health(
                            "match_odds", False, str(error)
                        )
                        logger.exception("match-odds refresh failed for %d", match_id)
                if all_succeeded:
                    self.observability.record_health("match_odds", True)
                    self.observability.increment(
                        "fetch_success_total", task="match_odds"
                    )
                else:
                    self.observability.increment(
                        "fetch_failure_total", task="match_odds"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.observability.increment(
                    "fetch_failure_total", task="match_odds"
                )
                self.observability.record_health("match_odds", False, str(error))
                logger.exception("failed to load pending match-odds work")
            finally:
                self.observability.observe(
                    "fetch_duration_seconds",
                    time.monotonic() - started,
                    task="match_odds",
                )
            await asyncio.sleep(self.odds_refresh_seconds)
