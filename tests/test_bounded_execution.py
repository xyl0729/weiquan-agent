from __future__ import annotations

import asyncio

import pytest

from app.execution.bounded import (
    BoundedExecutionBusyError,
    BoundedExecutionTimeoutError,
    BoundedExecutor,
)


def run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


async def _wait_until_waiting(
    executor: BoundedExecutor,
    expected: int,
) -> None:
    for _ in range(100):
        if executor.snapshot().waiting == expected:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("等待队列未达到预期状态")


def test_executor_limits_running_and_waiting_work() -> None:
    async def exercise() -> None:
        executor = BoundedExecutor(
            name="ocr",
            max_concurrency=1,
            max_waiting=1,
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked() -> str:
            started.set()
            await release.wait()
            return "done"

        first = asyncio.create_task(executor.run(blocked))
        await started.wait()
        second = asyncio.create_task(executor.run(blocked))
        await _wait_until_waiting(executor, 1)

        with pytest.raises(BoundedExecutionBusyError) as exc_info:
            await executor.run(blocked)
        assert exc_info.value.reason == "queue_full"
        assert executor.snapshot().running == 1
        release.set()
        assert await first == "done"
        assert await second == "done"
        assert executor.snapshot().running == 0
        assert executor.snapshot().waiting == 0

    run(exercise())


def test_wait_timeout_releases_waiting_capacity() -> None:
    async def exercise() -> None:
        executor = BoundedExecutor(
            name="deepseek",
            max_concurrency=1,
            max_waiting=1,
            wait_timeout_seconds=0.01,
        )
        release = asyncio.Event()
        first = asyncio.create_task(
            executor.run(lambda: release.wait())
        )
        await asyncio.sleep(0)

        with pytest.raises(BoundedExecutionBusyError) as exc_info:
            await executor.run(lambda: release.wait())
        assert exc_info.value.reason == "queue_timeout"
        assert executor.snapshot().waiting == 0
        release.set()
        await first
        assert executor.snapshot().running == 0

    run(exercise())


def test_total_budget_cancels_operation_and_releases_slot() -> None:
    async def exercise() -> None:
        executor = BoundedExecutor(
            name="ocr",
            max_concurrency=1,
            max_waiting=0,
        )
        cancelled = asyncio.Event()

        async def slow() -> None:
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        with pytest.raises(BoundedExecutionTimeoutError):
            await executor.run(
                slow,
                total_timeout_seconds=0.01,
            )
        assert cancelled.is_set()
        assert executor.snapshot().running == 0
        assert await executor.run(lambda: asyncio.sleep(0, result=1)) == 1

    run(exercise())


def test_operation_timeout_error_is_not_reclassified() -> None:
    async def operation() -> None:
        raise TimeoutError("operation-owned")

    executor = BoundedExecutor(
        name="test",
        max_concurrency=1,
        max_waiting=0,
    )
    with pytest.raises(TimeoutError, match="operation-owned"):
        run(
            executor.run(
                operation,
                total_timeout_seconds=1,
            )
        )


def test_cancelling_waiter_releases_waiting_capacity() -> None:
    async def exercise() -> None:
        executor = BoundedExecutor(
            name="test",
            max_concurrency=1,
            max_waiting=1,
        )
        release = asyncio.Event()
        first = asyncio.create_task(
            executor.run(lambda: release.wait())
        )
        await asyncio.sleep(0)
        waiting = asyncio.create_task(
            executor.run(lambda: asyncio.sleep(0))
        )
        await _wait_until_waiting(executor, 1)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert executor.snapshot().waiting == 0
        release.set()
        await first
        assert executor.snapshot().running == 0

    run(exercise())
