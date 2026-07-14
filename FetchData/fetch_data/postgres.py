"""PostgreSQL 存储层。

对外暴露异步方法，内部仍使用同步的 psycopg2；耗时数据库操作通过
``asyncio.to_thread`` 放到工作线程，避免卡住抓取网页所用的事件循环。
"""

import asyncio
from typing import List, Optional, Sequence

import psycopg2
from psycopg2.extensions import connection as Connection
from psycopg2.extras import execute_values

from .models import Match, MatchBasicInfo


SYNC_LOCK_NAME = "football2607:sync-match-status"


INITIALIZE_MATCH_STATUS_TABLE = """
CREATE TABLE IF NOT EXISTS match_status (
    match_id BIGINT PRIMARY KEY,
    crawl_status TEXT NOT NULL DEFAULT '未完成'
        CHECK (crawl_status IN ('未完成', '已完成'))
);

ALTER TABLE match_status
ADD COLUMN IF NOT EXISTS crawl_status TEXT NOT NULL DEFAULT '未完成'
    CHECK (crawl_status IN ('未完成', '已完成'));

ALTER TABLE match_status DROP COLUMN IF EXISTS status_text;
ALTER TABLE match_status DROP COLUMN IF EXISTS status;

CREATE INDEX IF NOT EXISTS idx_match_status_crawl_status
ON match_status(crawl_status);

CREATE TABLE IF NOT EXISTS match_basic_info (
    match_id BIGINT PRIMARY KEY REFERENCES match_status(match_id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    league TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    scheduled_time TEXT NOT NULL,
    home_score SMALLINT,
    away_score SMALLINT,
    status_text TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


class PostgresMatchStore:
    """负责建表、查询待抓 ID，以及批量写入比赛信息。"""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._connection: Optional[Connection] = None
        # 两个同步任务共享一条 psycopg2 连接。Lock 保证同一时刻只有一个线程
        # 操作它，因为 psycopg2 连接上的事务不能在这里并发交错。
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """连接数据库、取得进程锁并初始化表结构。"""

        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    async def upsert_match_list(self, matches: Sequence[Match]) -> None:
        """写入列表页结果：新增 ID，并更新已有详情的动态字段。"""

        if not matches:
            return
        async with self._lock:
            await asyncio.to_thread(self._upsert_match_list_sync, matches)

    async def fetch_pending_match_ids(self) -> List[int]:
        """查询尚未完成详情采集的比赛 ID。"""

        async with self._lock:
            return await asyncio.to_thread(self._fetch_pending_match_ids_sync)

    async def upsert_match_details(
        self,
        details: Sequence[MatchBasicInfo],
    ) -> None:
        """批量新增或更新详情页负责的基本信息字段。"""

        if not details:
            return
        async with self._lock:
            await asyncio.to_thread(self._upsert_match_details_sync, details)

    async def close(self) -> None:
        """关闭共享连接；重复调用也安全。"""

        async with self._lock:
            if self._connection is not None:
                await asyncio.to_thread(self._connection.close)
                self._connection = None

    def _initialize_sync(self) -> None:
        """同步版初始化逻辑，只应由上面的异步包装方法调用。"""

        self._connection = psycopg2.connect(self.dsn)
        try:
            with self._connection:
                with self._connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_try_advisory_lock(hashtext(%s))",
                        (SYNC_LOCK_NAME,),
                    )
                    # PostgreSQL advisory lock 是跨进程互斥锁，防止误启动两个
                    # sync-match-status 进程，造成重复抓取和写入竞争。
                    if not cursor.fetchone()[0]:
                        raise RuntimeError(
                            "another sync-match-status process is already running"
                        )
                    cursor.execute(INITIALIZE_MATCH_STATUS_TABLE)
        except Exception:
            self._connection.close()
            self._connection = None
            raise

    def _upsert_match_list_sync(self, matches: Sequence[Match]) -> None:
        if self._connection is None:
            raise RuntimeError("PostgresMatchStore is not initialized")

        # execute_values 要求“行的序列”，即便只有一列也写成单元素元组。
        match_ids = [(int(match.match_id),) for match in matches]
        # 列表页最可靠的是实时变化字段；队名和联赛仍由详情页负责。
        dynamic_values = [
            (
                int(match.match_id),
                match.scheduled_time,
                match.home_score,
                match.away_score,
                match.status_text or "未开始",
            )
            for match in matches
        ]
        with self._connection:
            with self._connection.cursor() as cursor:
                execute_values(
                    cursor,
                    """
                    INSERT INTO match_status (match_id)
                    VALUES %s
                    ON CONFLICT (match_id)
                    DO NOTHING
                    """,
                    match_ids,
                )
                # 只有详情行已经存在时才更新。刚发现的比赛先进入 match_status
                # 等待队列，之后由详情任务创建 match_basic_info 行。
                execute_values(
                    cursor,
                    """
                    UPDATE match_basic_info AS detail
                    SET scheduled_time = snapshot.scheduled_time,
                        home_score = snapshot.home_score::SMALLINT,
                        away_score = snapshot.away_score::SMALLINT,
                        status_text = snapshot.status_text,
                        updated_at = NOW()
                    FROM (VALUES %s) AS snapshot(
                        match_id,
                        scheduled_time,
                        home_score,
                        away_score,
                        status_text
                    )
                    WHERE detail.match_id = snapshot.match_id::BIGINT
                    """,
                    dynamic_values,
                )

    def _fetch_pending_match_ids_sync(self) -> List[int]:
        if self._connection is None:
            raise RuntimeError("PostgresMatchStore is not initialized")

        with self._connection:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT match_id
                    FROM match_status
                    WHERE crawl_status = '未完成'
                    ORDER BY match_id
                    """
                )
                return [int(row[0]) for row in cursor.fetchall()]

    def _upsert_match_details_sync(
        self,
        details: Sequence[MatchBasicInfo],
    ) -> None:
        if self._connection is None:
            raise RuntimeError("PostgresMatchStore is not initialized")

        # 先在 Python 中整理为与 INSERT 列顺序一致的元组，交给
        # execute_values 一次性发送，避免逐行访问数据库。
        values = [
            (
                detail.match_id,
                detail.source,
                detail.league,
                detail.home_team,
                detail.away_team,
                detail.scheduled_time,
                detail.home_score,
                detail.away_score,
                detail.status_text,
            )
            for detail in details
        ]
        with self._connection:
            with self._connection.cursor() as cursor:
                execute_values(
                    cursor,
                    """
                    INSERT INTO match_basic_info (
                        match_id,
                        source,
                        league,
                        home_team,
                        away_team,
                        scheduled_time,
                        home_score,
                        away_score,
                        status_text
                    )
                    VALUES %s
                    ON CONFLICT (match_id)
                    DO UPDATE SET
                        source = EXCLUDED.source,
                        league = EXCLUDED.league,
                        home_team = EXCLUDED.home_team,
                        away_team = EXCLUDED.away_team,
                        updated_at = NOW()
                    """,
                    values,
                )
                # 冲突更新刻意不覆盖 scheduled_time、比分和 status_text：这些
                # 高频变化字段后续归列表任务所有，避免较旧详情页数据倒灌。
