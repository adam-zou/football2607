"""持续同步的调度层。

本模块只决定“何时抓、何时写”，不关心网页和 SQL 的实现细节。三个 Protocol
描述它所需要的最小接口，因此测试时可以传入简单的假对象。
"""

import asyncio
import logging
from typing import AsyncIterator, List, Protocol, Sequence

from .models import Match, MatchBasicInfo


logger = logging.getLogger(__name__)


class MatchStore(Protocol):
    """同步器所需的持久化接口；当前正式实现是 PostgresMatchStore。"""

    async def initialize(self) -> None:
        ...

    async def upsert_match_list(self, matches: Sequence[Match]) -> None:
        ...

    async def fetch_pending_match_ids(self) -> List[int]:
        ...

    async def upsert_match_details(
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


class MatchSynchronizer:
    """让列表刷新和详情抓取作为两个互不等待的任务运行。"""

    def __init__(
        self,
        store: MatchStore,
        match_list: MatchListSnapshot,
        match_details: MatchDetailSnapshot,
        *,
        list_refresh_seconds: float = 60.0,
        detail_refresh_seconds: float = 60.0,
        detail_batch_size: int = 10,
    ) -> None:
        if list_refresh_seconds <= 0:
            raise ValueError("list_refresh_seconds must be greater than zero")
        if detail_refresh_seconds <= 0:
            raise ValueError("detail_refresh_seconds must be greater than zero")
        if detail_batch_size <= 0:
            raise ValueError("detail_batch_size must be greater than zero")
        self.store = store
        self.match_list = match_list
        self.match_details = match_details
        self.list_refresh_seconds = list_refresh_seconds
        self.detail_refresh_seconds = detail_refresh_seconds
        self.detail_batch_size = detail_batch_size

    async def run(self) -> None:
        """初始化数据库，启动两个常驻任务，并保证退出时释放资源。"""

        await self.store.initialize()
        # create_task 后两个无限循环会由事件循环交替推进；详情页慢，不会阻塞
        # 下一次列表刷新。
        tasks = [
            asyncio.create_task(self._run_match_list_task()),
            asyncio.create_task(self._run_match_detail_task()),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            # 任一任务异常退出或程序收到取消信号时，另一个任务也必须停止。
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.store.close()

    async def _run_match_list_task(self) -> None:
        """周期抓列表：发现新 ID，并刷新已有详情的时间、比分和状态。"""

        while True:
            try:
                matches = await self.match_list.fetch_matches()
                await self.store.upsert_match_list(matches)
                logger.info("refreshed %d matches from the list page", len(matches))
            except asyncio.CancelledError:
                # CancelledError 是正常关机信号，不能被下面的兜底异常吞掉。
                raise
            except Exception:
                logger.exception("match-list refresh failed")
            # 间隔从本轮结束后计算，并非固定整点调度。
            await asyncio.sleep(self.list_refresh_seconds)

    async def _run_match_detail_task(self) -> None:
        """周期处理数据库中仍标记为“未完成”的详情任务。"""

        while True:
            try:
                match_ids = await self.store.fetch_pending_match_ids()
                if match_ids:
                    stored = 0
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
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("match-detail refresh failed")
            await asyncio.sleep(self.detail_refresh_seconds)
