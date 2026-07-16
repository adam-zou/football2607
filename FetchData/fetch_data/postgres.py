"""PostgreSQL 存储层。

对外暴露异步方法，内部仍使用同步的 psycopg2；耗时数据库操作通过
``asyncio.to_thread`` 放到工作线程，避免卡住抓取网页所用的事件循环。
"""

import asyncio
from datetime import datetime
from typing import List, Optional, Sequence
from zoneinfo import ZoneInfo

import psycopg2
from psycopg2.extensions import connection as Connection
from psycopg2.extras import execute_values

from .match_completion import mark_matches_completed
from .models import MatchBasicInfo
from .schema import load_migrations


SYNC_LOCK_NAME = "football2607:sync-match-status"


def parse_scheduled_at(value: str) -> Optional[datetime]:
    """把页面的北京时间文本转换成可写入 TIMESTAMPTZ 的时间。"""

    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
    except (AttributeError, ValueError):
        return None
    return parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))


INITIALIZE_MATCH_STATUS_TABLE = load_migrations(
    (
        "001_match_status.sql",
        "002_match_basic_info.sql",
        "005_match_dynamic_schedule.sql",
    )
)


FETCH_PENDING_DETAIL_IDS = """
SELECT match_id
FROM match_status
WHERE detail_status = '未完成'
ORDER BY match_id
"""


FETCH_PENDING_DYNAMIC_IDS = """
SELECT status.match_id
FROM match_status AS status
JOIN match_basic_info AS basic ON basic.match_id = status.match_id
LEFT JOIN match_dynamic_schedule AS schedule
  ON schedule.match_id = status.match_id
WHERE status.detail_status = '已完成'
  AND status.crawl_status = '未完成'
  AND basic.status_text <> '完'
  AND basic.scheduled_at <= NOW() + INTERVAL '24 hours'
  AND COALESCE(schedule.next_attempt_at, '-infinity'::TIMESTAMPTZ) <= NOW()
ORDER BY CASE
             WHEN basic.scheduled_at <= NOW() + INTERVAL '5 minutes' THEN 0
             ELSE 1
         END,
         schedule.next_attempt_at ASC NULLS FIRST,
         status.match_id
LIMIT %s
"""


BEGIN_DYNAMIC_ATTEMPT = """
INSERT INTO match_dynamic_schedule (
    match_id, next_attempt_at, last_attempt_at
)
VALUES %s
ON CONFLICT (match_id) DO UPDATE SET
    next_attempt_at = NOW() + INTERVAL '5 minutes',
    last_attempt_at = NOW(),
    updated_at = NOW()
"""


RECORD_DYNAMIC_SUCCESS = """
INSERT INTO match_dynamic_schedule (
    match_id, consecutive_failures, next_attempt_at, last_attempt_at,
    last_succeeded_at, last_error, is_abnormal, abnormal_since
)
SELECT basic.match_id,
       0,
       CASE
           WHEN basic.status_text = '完' THEN NOW() + INTERVAL '1 minute'
           WHEN basic.scheduled_at <= NOW() + INTERVAL '5 minutes'
           THEN NOW() + INTERVAL '1 minute'
           ELSE LEAST(
               NOW() + INTERVAL '8 hours',
               basic.scheduled_at - INTERVAL '5 minutes'
           )
       END,
       NOW(), NOW(), NULL, FALSE, NULL
FROM match_basic_info AS basic
WHERE basic.match_id = ANY(%s::BIGINT[])
ON CONFLICT (match_id) DO UPDATE SET
    consecutive_failures = 0,
    next_attempt_at = EXCLUDED.next_attempt_at,
    last_attempt_at = EXCLUDED.last_attempt_at,
    last_succeeded_at = EXCLUDED.last_succeeded_at,
    last_error = NULL,
    is_abnormal = FALSE,
    abnormal_since = NULL,
    updated_at = NOW()
"""


RECORD_DYNAMIC_FAILURE = """
INSERT INTO match_dynamic_schedule (
    match_id, consecutive_failures, next_attempt_at, last_attempt_at,
    last_error, is_abnormal, abnormal_since
)
VALUES %s
ON CONFLICT (match_id) DO UPDATE SET
    consecutive_failures = match_dynamic_schedule.consecutive_failures + 1,
    next_attempt_at = NOW() + CASE
        WHEN match_dynamic_schedule.consecutive_failures + 1 = 1
        THEN INTERVAL '1 minute'
        WHEN match_dynamic_schedule.consecutive_failures + 1 = 2
        THEN INTERVAL '2 minutes'
        WHEN match_dynamic_schedule.consecutive_failures + 1 = 3
        THEN INTERVAL '5 minutes'
        ELSE INTERVAL '3 hours'
    END,
    last_attempt_at = NOW(),
    last_error = EXCLUDED.last_error,
    is_abnormal = match_dynamic_schedule.consecutive_failures + 1 >= 4,
    abnormal_since = CASE
        WHEN match_dynamic_schedule.consecutive_failures + 1 >= 4
        THEN COALESCE(match_dynamic_schedule.abnormal_since, NOW())
        ELSE NULL
    END,
    updated_at = NOW()
"""


class PostgresMatchStore:
    """负责建表、查询待抓 ID，以及批量写入比赛信息。"""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._connection: Optional[Connection] = None
        # 两个同步任务共享一条 psycopg2 连接。Lock 保证同一时刻只有一个线程
        # 操作它，因为 psycopg2 连接上的事务不能在这里并发交错。
        self._lock: Optional[asyncio.Lock] = None

    async def initialize(self) -> None:
        """连接数据库、取得进程锁并初始化表结构。"""

        async with self._get_lock():
            await asyncio.to_thread(self._initialize_sync)

    async def upsert_match_list(self, match_ids: Sequence[int]) -> None:
        """把列表页发现的新比赛 ID 加入采集状态表。"""

        if not match_ids:
            return
        async with self._get_lock():
            await asyncio.to_thread(self._upsert_match_list_sync, match_ids)

    async def fetch_pending_detail_ids(self) -> List[int]:
        """查询静态详情尚未成功保存的比赛 ID。"""

        async with self._get_lock():
            return await asyncio.to_thread(self._fetch_pending_detail_ids_sync)

    async def fetch_pending_dynamic_ids(self, limit: int) -> List[int]:
        """查询已到动态信息执行时间的比赛 ID。"""

        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        async with self._get_lock():
            return await asyncio.to_thread(
                self._fetch_pending_dynamic_ids_sync,
                limit,
            )

    async def begin_dynamic_attempts(self, match_ids: Sequence[int]) -> None:
        """为本轮动态详情请求写入五分钟租约。"""

        if not match_ids:
            return
        async with self._get_lock():
            await asyncio.to_thread(self._begin_dynamic_attempts_sync, match_ids)

    async def upsert_match_details(
        self,
        details: Sequence[MatchBasicInfo],
    ) -> None:
        """批量新增或更新详情页负责的基本信息字段。"""

        if not details:
            return
        async with self._get_lock():
            await asyncio.to_thread(self._upsert_match_details_sync, details)

    async def upsert_match_dynamics(
        self,
        details: Sequence[MatchBasicInfo],
    ) -> None:
        """更新详情页提供的开赛时间、比分和比赛状态。"""

        if not details:
            return
        async with self._get_lock():
            await asyncio.to_thread(self._upsert_match_dynamics_sync, details)

    async def record_dynamic_failures(
        self,
        match_ids: Sequence[int],
        error: str,
    ) -> None:
        """为未返回有效详情的比赛记录独立退避。"""

        if not match_ids:
            return
        async with self._get_lock():
            await asyncio.to_thread(
                self._record_dynamic_failures_sync,
                match_ids,
                error,
            )

    async def close(self) -> None:
        """关闭共享连接；重复调用也安全。"""

        async with self._get_lock():
            if self._connection is not None:
                await asyncio.to_thread(self._connection.close)
                self._connection = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

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

    def _upsert_match_list_sync(self, match_ids: Sequence[int]) -> None:
        if self._connection is None:
            raise RuntimeError("PostgresMatchStore is not initialized")

        # execute_values 要求“行的序列”，即便只有一列也写成单元素元组。
        values = [(int(match_id),) for match_id in match_ids]
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
                    values,
                )
                # 列表页只发现比赛 ID。基础详情和动态字段均由数据库驱动的
                # 详情任务负责，避免页面当前展示范围决定后续采集范围。

    def _fetch_pending_detail_ids_sync(self) -> List[int]:
        if self._connection is None:
            raise RuntimeError("PostgresMatchStore is not initialized")

        with self._connection:
            with self._connection.cursor() as cursor:
                cursor.execute(FETCH_PENDING_DETAIL_IDS)
                return [int(row[0]) for row in cursor.fetchall()]

    def _fetch_pending_dynamic_ids_sync(self, limit: int) -> List[int]:
        if self._connection is None:
            raise RuntimeError("PostgresMatchStore is not initialized")

        with self._connection.cursor() as cursor:
            cursor.execute(FETCH_PENDING_DYNAMIC_IDS, (limit,))
            return [int(row[0]) for row in cursor.fetchall()]

    def _begin_dynamic_attempts_sync(self, match_ids: Sequence[int]) -> None:
        if self._connection is None:
            raise RuntimeError("PostgresMatchStore is not initialized")
        with self._connection:
            with self._connection.cursor() as cursor:
                execute_values(
                    cursor,
                    BEGIN_DYNAMIC_ATTEMPT,
                    [(int(match_id),) for match_id in match_ids],
                    template="(%s, NOW() + INTERVAL '5 minutes', NOW())",
                )

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
                parse_scheduled_at(detail.scheduled_time),
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
                        scheduled_at,
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
                        scheduled_time = CASE
                            WHEN match_basic_info.scheduled_time ~
                                '^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$'
                            THEN match_basic_info.scheduled_time
                            ELSE EXCLUDED.scheduled_time
                        END,
                        scheduled_at = CASE
                            WHEN match_basic_info.scheduled_time ~
                                '^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$'
                            THEN match_basic_info.scheduled_at
                            ELSE EXCLUDED.scheduled_at
                        END,
                        updated_at = NOW()
                    """,
                    values,
                )
                cursor.execute(
                    """
                    UPDATE match_status
                    SET detail_status = '已完成',
                        updated_at = NOW()
                    WHERE match_id = ANY(%s::BIGINT[])
                    """,
                    ([detail.match_id for detail in details],),
                )
                # 基础详情首次成功即完成；后续开赛时间、比分和状态由独立的
                # 数据库驱动动态任务更新。
                mark_matches_completed(
                    cursor,
                    [detail.match_id for detail in details],
                )

    def _upsert_match_dynamics_sync(
        self,
        details: Sequence[MatchBasicInfo],
    ) -> None:
        if self._connection is None:
            raise RuntimeError("PostgresMatchStore is not initialized")

        match_ids = [detail.match_id for detail in details]
        with self._connection:
            with self._connection.cursor() as cursor:
                execute_values(
                    cursor,
                    """
                    UPDATE match_basic_info AS basic
                    SET scheduled_time = dynamic.scheduled_time,
                        scheduled_at = dynamic.scheduled_at::TIMESTAMPTZ,
                        home_score = dynamic.home_score::SMALLINT,
                        away_score = dynamic.away_score::SMALLINT,
                        status_text = dynamic.status_text,
                        dynamic_updated_at = NOW(),
                        updated_at = NOW()
                    FROM (VALUES %s) AS dynamic(
                        match_id,
                        scheduled_time,
                        scheduled_at,
                        home_score,
                        away_score,
                        status_text
                    )
                    WHERE basic.match_id = dynamic.match_id::BIGINT
                    """,
                    [
                        (
                            detail.match_id,
                            detail.scheduled_time,
                            parse_scheduled_at(detail.scheduled_time),
                            detail.home_score,
                            detail.away_score,
                            detail.status_text,
                        )
                        for detail in details
                    ],
                )
                cursor.execute(
                    RECORD_DYNAMIC_SUCCESS,
                    (match_ids,),
                )
                mark_matches_completed(cursor, match_ids)

    def _record_dynamic_failures_sync(
        self,
        match_ids: Sequence[int],
        error: str,
    ) -> None:
        if self._connection is None:
            raise RuntimeError("PostgresMatchStore is not initialized")
        with self._connection:
            with self._connection.cursor() as cursor:
                execute_values(
                    cursor,
                    RECORD_DYNAMIC_FAILURE,
                    [(int(match_id), str(error)[:1000]) for match_id in match_ids],
                    template=(
                        "(%s, 1, NOW() + INTERVAL '1 minute', NOW(), "
                        "%s, FALSE, NULL)"
                    ),
                )
