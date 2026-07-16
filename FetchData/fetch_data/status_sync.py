"""持续同步的调度层。

本模块只决定“何时抓、何时写”，不关心网页和 SQL 的实现细节。五个 Protocol
描述它所需要的最小接口，因此测试时可以传入简单的假对象。
"""

import asyncio
import logging
import time
from typing import AsyncIterator, Dict, List, Optional, Protocol, Sequence, Tuple

from .models import MatchBasicInfo, OddsMarketRequest, OddsSnapshot
from .observability import RuntimeObservability


logger = logging.getLogger(__name__)


class MatchStore(Protocol):
    """同步器所需的持久化接口；当前正式实现是 PostgresMatchStore。"""

    async def initialize(self) -> None:
        ...

    async def upsert_match_list(self, match_ids: Sequence[int]) -> None:
        ...

    async def fetch_pending_detail_ids(self) -> List[int]:
        ...

    async def fetch_pending_dynamic_ids(self, limit: int) -> List[int]:
        ...

    async def begin_dynamic_attempts(self, match_ids: Sequence[int]) -> None:
        ...

    async def upsert_match_details(
        self,
        details: Sequence[MatchBasicInfo],
    ) -> None:
        ...

    async def upsert_match_dynamics(
        self,
        details: Sequence[MatchBasicInfo],
    ) -> None:
        ...

    async def record_dynamic_failures(
        self,
        match_ids: Sequence[int],
        error: str,
    ) -> None:
        ...

    async def close(self) -> None:
        ...


class MatchListSnapshot(Protocol):
    """比赛列表抓取器需要满足的最小接口。"""

    async def fetch_match_ids(self) -> List[int]:
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

    async def begin_match_attempt(
        self,
        match_id: int,
    ) -> List[OddsMarketRequest]:
        ...

    async def record_market_outcomes(self, snapshot: OddsSnapshot) -> None:
        ...

    async def record_market_failures(
        self,
        match_id: int,
        requests: Sequence[OddsMarketRequest],
        error: str,
    ) -> None:
        ...

    async def upsert_snapshot(self, snapshot: OddsSnapshot) -> None:
        ...

    async def close(self) -> None:
        ...


class MatchOddsSnapshot(Protocol):
    """按领取计划抓取一场比赛若干机构市场页面的最小接口。"""

    async def fetch_match_odds(
        self,
        match_id: int,
        market_requests: Sequence[OddsMarketRequest],
    ) -> OddsSnapshot:
        ...

    async def refresh_proxy(self) -> None:
        ...


class MatchSynchronizer:
    """让列表、基础详情、动态信息和赔率作为四个互不等待的任务运行。"""

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
        dynamic_refresh_seconds: float = 5.0,
        dynamic_batch_size: int = 10,
        odds_refresh_seconds: float = 5.0,
        odds_batch_size: int = 6,
        odds_match_concurrency: int = 3,
        odds_match_timeout_seconds: float = 60.0,
        observability: Optional[RuntimeObservability] = None,
    ) -> None:
        if list_refresh_seconds <= 0:
            raise ValueError("list_refresh_seconds must be greater than zero")
        if detail_refresh_seconds <= 0:
            raise ValueError("detail_refresh_seconds must be greater than zero")
        if detail_batch_size <= 0:
            raise ValueError("detail_batch_size must be greater than zero")
        if dynamic_refresh_seconds <= 0:
            raise ValueError("dynamic_refresh_seconds must be greater than zero")
        if dynamic_batch_size <= 0:
            raise ValueError("dynamic_batch_size must be greater than zero")
        if odds_refresh_seconds <= 0:
            raise ValueError("odds_refresh_seconds must be greater than zero")
        if odds_batch_size <= 0:
            raise ValueError("odds_batch_size must be greater than zero")
        if odds_match_concurrency <= 0:
            raise ValueError("odds_match_concurrency must be greater than zero")
        if odds_match_timeout_seconds <= 0:
            raise ValueError("odds_match_timeout_seconds must be greater than zero")
        self.store = store
        self.match_list = match_list
        self.match_details = match_details
        self.odds_store = odds_store
        self.match_odds = match_odds
        self.list_refresh_seconds = list_refresh_seconds
        self.detail_refresh_seconds = detail_refresh_seconds
        self.detail_batch_size = detail_batch_size
        self.dynamic_refresh_seconds = dynamic_refresh_seconds
        self.dynamic_batch_size = dynamic_batch_size
        self.odds_refresh_seconds = odds_refresh_seconds
        self.odds_batch_size = odds_batch_size
        self.odds_match_concurrency = odds_match_concurrency
        self.odds_match_timeout_seconds = odds_match_timeout_seconds
        self.observability = observability or RuntimeObservability()

    async def run(self) -> None:
        """初始化数据库，启动四个常驻任务，并保证退出时释放资源。"""

        for component in (
            "database",
            "match_list",
            "match_detail",
            "match_dynamic",
            "match_odds",
        ):
            self.observability.record_health(component, False, "not started")
        try:
            await self.store.initialize()
            await self.odds_store.initialize()
        except Exception as error:
            self.observability.record_health("database", False, str(error))
            raise
        self.observability.record_health("database", True)
        try:
            # 四个无限循环由事件循环独立推进，彼此不等待。
            tasks = [
                asyncio.create_task(self._run_match_list_task()),
                asyncio.create_task(self._run_match_detail_task()),
                asyncio.create_task(self._run_match_dynamic_task()),
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
        """周期抓列表，只发现新比赛 ID。"""

        while True:
            started = time.monotonic()
            self.observability.increment("fetch_attempts_total", task="match_list")
            try:
                match_ids = await self.match_list.fetch_match_ids()
                await self.store.upsert_match_list(match_ids)
                self.observability.increment(
                    "fetch_success_total", task="match_list"
                )
                self.observability.set_gauge(
                    "matches_last_snapshot", len(match_ids)
                )
                self.observability.record_health("match_list", True)
                logger.info("已从比赛列表发现 %d 个比赛 ID", len(match_ids))
            except asyncio.CancelledError:
                # CancelledError 是正常关机信号，不能被下面的兜底异常吞掉。
                raise
            except Exception as error:
                self.observability.increment(
                    "fetch_failure_total", task="match_list"
                )
                self.observability.record_health("match_list", False, str(error))
                logger.exception("比赛列表刷新失败")
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
                            "已保存比赛详情批次：%d 条（总进度 %d/%d）",
                            len(details),
                            stored,
                            len(match_ids),
                        )
                    logger.info(
                        "待处理比赛详情已保存 %d/%d 条",
                        stored,
                        len(match_ids),
                    )
                detail_healthy = stored == len(match_ids)
                if detail_healthy:
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
                        f"stored {stored} of {len(match_ids)} details",
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.observability.increment(
                    "fetch_failure_total", task="match_detail"
                )
                self.observability.record_health("match_detail", False, str(error))
                logger.exception("比赛详情刷新失败")
            finally:
                self.observability.observe(
                    "fetch_duration_seconds",
                    time.monotonic() - started,
                    task="match_detail",
                )
            await asyncio.sleep(self.detail_refresh_seconds)

    async def _run_match_dynamic_task(self) -> None:
        """从数据库领取到期比赛，更新开赛时间、比分和比赛状态。"""

        while True:
            started = time.monotonic()
            claimed_ids: List[int] = []
            self.observability.increment("fetch_attempts_total", task="match_dynamic")
            try:
                claimed_ids = await self.store.fetch_pending_dynamic_ids(
                    self.dynamic_batch_size
                )
                self.observability.set_gauge(
                    "queue_pending",
                    len(claimed_ids),
                    queue="match_dynamic",
                )
                if not claimed_ids:
                    await asyncio.sleep(self.dynamic_refresh_seconds)
                    continue

                await self.store.begin_dynamic_attempts(claimed_ids)
                collected_details: List[MatchBasicInfo] = []
                try:
                    async for details in (
                        self.match_details.fetch_match_detail_batches(
                            claimed_ids,
                            batch_size=self.dynamic_batch_size,
                        )
                    ):
                        collected_details.extend(details)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    reason = str(error) or error.__class__.__name__
                    await self.store.record_dynamic_failures(
                        claimed_ids,
                        reason,
                    )
                    self.observability.increment(
                        "fetch_failure_total", task="match_dynamic"
                    )
                    self.observability.record_health(
                        "match_dynamic", False, reason
                    )
                    logger.exception("动态比赛信息整批抓取失败")
                    continue

                stored_ids = set()
                if collected_details:
                    await self.store.upsert_match_dynamics(collected_details)
                    stored_ids.update(
                        detail.match_id for detail in collected_details
                    )

                failed_ids = [
                    match_id for match_id in claimed_ids if match_id not in stored_ids
                ]
                if failed_ids:
                    await self.store.record_dynamic_failures(
                        failed_ids,
                        "动态详情页未返回有效比赛信息",
                    )
                    self.observability.increment(
                        "fetch_failure_total", task="match_dynamic"
                    )
                    self.observability.record_health(
                        "match_dynamic",
                        False,
                        f"updated {len(stored_ids)} of {len(claimed_ids)} matches",
                    )
                else:
                    self.observability.increment(
                        "fetch_success_total", task="match_dynamic"
                    )
                    self.observability.record_health("match_dynamic", True)
                logger.info(
                    "已更新动态比赛信息 %d/%d 场",
                    len(stored_ids),
                    len(claimed_ids),
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.observability.increment(
                    "fetch_failure_total", task="match_dynamic"
                )
                self.observability.record_health(
                    "match_dynamic", False, str(error)
                )
                logger.exception("动态比赛信息刷新失败")
                await asyncio.sleep(self.dynamic_refresh_seconds)
            finally:
                self.observability.observe(
                    "fetch_duration_seconds",
                    time.monotonic() - started,
                    task="match_dynamic",
                )

    async def _run_match_odds_task(self) -> None:
        """用连续补位工作池处理赔率任务，不等待整批比赛全部结束。"""

        pending_ids: List[int] = []
        active: Dict[asyncio.Task, int] = {}
        consecutive_full_failures = 0

        async def record_failure(
            match_id: int,
            requests: Sequence[OddsMarketRequest],
            error: str,
        ) -> None:
            try:
                await self.odds_store.record_market_failures(
                    match_id,
                    requests,
                    error,
                )
            except Exception:
                logger.exception("记录比赛 %d 的赔率退避状态失败", match_id)
            self.observability.increment("odds_matches_failure_total")
            self.observability.increment(
                "fetch_failure_total", task="match_odds"
            )
            self.observability.record_health("match_odds", False, error)

        async def fetch_one_match(match_id: int) -> Tuple[str, float]:
            started = time.monotonic()
            collection_started = False
            collection_completed = False
            market_requests: List[OddsMarketRequest] = []
            try:
                # 领取当前到期的机构市场页面并写入 5 分钟租约；成功页面按正常
                # 周期调度，失败页面单独退避，下一轮不会重复抓成功页面。
                market_requests = await self.odds_store.begin_match_attempt(match_id)
                if not market_requests:
                    return "skipped", time.monotonic()
                collection_started = True
                snapshot = await asyncio.wait_for(
                    self.match_odds.fetch_match_odds(
                        match_id,
                        market_requests=market_requests,
                    ),
                    timeout=self.odds_match_timeout_seconds,
                )
                collection_completed = True
                await self.odds_store.upsert_snapshot(snapshot)
                await self.odds_store.record_market_outcomes(snapshot)
                if snapshot.failed_markets:
                    reason = "部分市场页面采集失败：" + ",".join(
                        f"{request.company_id}/{request.market}"
                        for request in snapshot.failed_markets
                    )
                    self.observability.increment("odds_matches_failure_total")
                    self.observability.increment(
                        "fetch_failure_total", task="match_odds"
                    )
                    self.observability.record_health(
                        "match_odds", False, reason
                    )
                    result = (
                        "failure"
                        if not snapshot.successful_markets
                        else "partial"
                    )
                else:
                    self.observability.increment("odds_matches_success_total")
                    self.observability.increment(
                        "fetch_success_total", task="match_odds"
                    )
                    self.observability.record_health("match_odds", True)
                    result = "success"
                logger.info(
                    "已保存比赛 %d 的赔率：成功页面=%d，失败页面=%d，"
                    "亚让=%d，胜平负=%d，进球数=%d",
                    match_id,
                    len(snapshot.successful_markets),
                    len(snapshot.failed_markets),
                    len(snapshot.handicap_changes),
                    len(snapshot.one_x_two_changes),
                    len(snapshot.over_under_changes),
                )
                return result, time.monotonic()
            except asyncio.TimeoutError:
                reason = (
                    "整场赔率采集超过 "
                    f"{self.odds_match_timeout_seconds:g} 秒"
                )
                await record_failure(match_id, market_requests, reason)
                logger.error("比赛 %d 的赔率采集超时", match_id)
                return "failure", time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                reason = str(error) or error.__class__.__name__
                await record_failure(match_id, market_requests, reason)
                logger.exception("比赛 %d 的赔率采集失败", match_id)
                # 只有 Provider 在整场采集期间失败才说明代理可能失效。
                result = (
                    "failure"
                    if collection_started and not collection_completed
                    else "internal"
                )
                return result, time.monotonic()
            finally:
                self.observability.observe(
                    "fetch_duration_seconds",
                    time.monotonic() - started,
                    task="match_odds",
                )

        try:
            while True:
                # 只在本地待处理缓冲区用完时查询下一批；已经运行的慢任务不会
                # 阻止空闲名额领取新比赛。
                if not pending_ids and len(active) < self.odds_match_concurrency:
                    query_started = time.monotonic()
                    self.observability.increment(
                        "fetch_attempts_total", task="match_odds"
                    )
                    try:
                        pending_count = (
                            await self.odds_store.count_pending_match_ids()
                        )
                        self.observability.set_gauge(
                            "queue_pending",
                            pending_count,
                            queue="match_odds",
                        )
                        active_ids = set(active.values())
                        pending_ids.extend(
                            match_id
                            for match_id in (
                                await self.odds_store.fetch_pending_match_ids(
                                    self.odds_batch_size
                                )
                            )
                            if match_id not in active_ids
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        self.observability.increment(
                            "fetch_failure_total", task="match_odds"
                        )
                        self.observability.record_health(
                            "match_odds", False, str(error)
                        )
                        logger.exception("读取待处理赔率任务失败")
                    finally:
                        self.observability.observe(
                            "fetch_duration_seconds",
                            time.monotonic() - query_started,
                            task="match_odds_queue",
                        )

                while (
                    pending_ids
                    and len(active) < self.odds_match_concurrency
                ):
                    match_id = pending_ids.pop(0)
                    task = asyncio.create_task(fetch_one_match(match_id))
                    active[task] = match_id

                if active:
                    done, _ = await asyncio.wait(
                        active,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    completed = sorted(
                        (
                            (*task.result(), task)
                            for task in done
                        ),
                        key=lambda item: item[1],
                    )
                    for result, _, task in completed:
                        active.pop(task)
                        if result == "failure":
                            consecutive_full_failures += 1
                            if consecutive_full_failures >= 3:
                                try:
                                    await self.match_odds.refresh_proxy()
                                    logger.warning(
                                        "连续 3 场比赛整场采集失败，已强制更新并验证代理"
                                    )
                                except Exception as error:
                                    self.observability.record_health(
                                        "proxy", False, str(error)
                                    )
                                    logger.exception(
                                        "强制更新并验证代理失败"
                                    )
                                finally:
                                    consecutive_full_failures = 0
                        else:
                            # 完整成功或部分页面成功都证明访问链路仍然可用。
                            consecutive_full_failures = 0
                    continue

                await asyncio.sleep(self.odds_refresh_seconds)
        finally:
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
