"""Bounded async scheduling helpers for standalone crawler pages."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import (
    AsyncIterator,
    Awaitable,
    Callable,
    Generic,
    Optional,
    Sequence,
    TypeVar,
)


Job = TypeVar("Job")
Result = TypeVar("Result")


@dataclass(frozen=True)
class JobOutcome(Generic[Job, Result]):
    job: Job
    result: Optional[Result] = None
    error: Optional[Exception] = None


async def iter_bounded(
    jobs: Sequence[Job],
    concurrency: int,
    handler: Callable[[Job], Awaitable[Result]],
) -> AsyncIterator[JobOutcome[Job, Result]]:
    """Run at most ``concurrency`` jobs and yield outcomes as they finish."""

    if concurrency <= 0:
        raise ValueError("concurrency must be greater than zero")
    if not jobs:
        return

    queue: asyncio.Queue = asyncio.Queue(maxsize=concurrency * 2)
    outcomes: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    async def produce() -> None:
        for job in jobs:
            await queue.put(job)
        for _ in range(concurrency):
            await queue.put(sentinel)

    async def work() -> None:
        while True:
            job = await queue.get()
            try:
                if job is sentinel:
                    return
                try:
                    result = await handler(job)
                except Exception as error:
                    await outcomes.put(JobOutcome(job=job, error=error))
                else:
                    await outcomes.put(JobOutcome(job=job, result=result))
            finally:
                queue.task_done()

    producer = asyncio.create_task(produce())
    workers = [asyncio.create_task(work()) for _ in range(concurrency)]
    tasks = [producer, *workers]
    try:
        for _ in jobs:
            yield await outcomes.get()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@asynccontextmanager
async def async_proxy_lease(proxy_client, *, min_remaining_seconds: float):
    """Use the blocking localhost proxy lease client without blocking Playwright."""

    lease = proxy_client.lease(
        min_remaining_seconds=min_remaining_seconds,
    )
    enter_task = asyncio.create_task(asyncio.to_thread(lease.__enter__))
    try:
        proxy = await asyncio.shield(enter_task)
    except asyncio.CancelledError:
        proxy = await enter_task
        await asyncio.to_thread(
            lease.__exit__,
            asyncio.CancelledError,
            None,
            None,
        )
        raise

    try:
        yield proxy
    except BaseException as error:
        await asyncio.to_thread(
            lease.__exit__,
            type(error),
            error,
            error.__traceback__,
        )
        raise
    else:
        await asyncio.to_thread(lease.__exit__, None, None, None)
