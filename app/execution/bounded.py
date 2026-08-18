from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import TypeVar


T = TypeVar("T")


class BoundedExecutionBusyError(RuntimeError):
    """The bounded queue cannot accept or start more work."""

    def __init__(self, reason: str = "queue_full") -> None:
        super().__init__(reason)
        self.reason = reason


class BoundedExecutionTimeoutError(RuntimeError):
    """The total queue and execution budget was exhausted."""


@dataclass(frozen=True, slots=True)
class BoundedExecutorSnapshot:
    name: str
    running: int
    waiting: int
    max_concurrency: int
    max_waiting: int


class BoundedExecutor:
    """Bound asynchronous work without creating an unbounded waiter list."""

    def __init__(
        self,
        *,
        name: str,
        max_concurrency: int,
        max_waiting: int,
        wait_timeout_seconds: float | None = None,
    ) -> None:
        if not name.strip():
            raise ValueError("执行器名称不能为空")
        if max_concurrency < 1:
            raise ValueError("并发上限必须大于零")
        if max_waiting < 0:
            raise ValueError("等待上限不能小于零")
        if (
            wait_timeout_seconds is not None
            and wait_timeout_seconds <= 0
        ):
            raise ValueError("等待超时必须大于零")
        self.name = name.strip()
        self.max_concurrency = int(max_concurrency)
        self.max_waiting = int(max_waiting)
        self.wait_timeout_seconds = wait_timeout_seconds
        self._condition = asyncio.Condition()
        self._running = 0
        self._waiting = 0

    def snapshot(self) -> BoundedExecutorSnapshot:
        return BoundedExecutorSnapshot(
            name=self.name,
            running=self._running,
            waiting=self._waiting,
            max_concurrency=self.max_concurrency,
            max_waiting=self.max_waiting,
        )

    async def run(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        wait_timeout_seconds: float | None = None,
        total_timeout_seconds: float | None = None,
    ) -> T:
        if total_timeout_seconds is not None and total_timeout_seconds <= 0:
            raise ValueError("总执行预算必须大于零")
        started = monotonic()
        acquired = False
        queued = False

        async with self._condition:
            if self._running < self.max_concurrency:
                self._running += 1
                acquired = True
            elif self._waiting >= self.max_waiting:
                raise BoundedExecutionBusyError("queue_full")
            else:
                self._waiting += 1
                queued = True

        try:
            if not acquired:
                timeout = (
                    self.wait_timeout_seconds
                    if wait_timeout_seconds is None
                    else wait_timeout_seconds
                )
                if total_timeout_seconds is not None:
                    remaining = total_timeout_seconds - (
                        monotonic() - started
                    )
                    if remaining <= 0:
                        raise BoundedExecutionTimeoutError(
                            "total_timeout"
                        )
                    timeout = (
                        remaining
                        if timeout is None
                        else min(timeout, remaining)
                    )
                async def wait_for_slot() -> None:
                    async with self._condition:
                        await self._condition.wait_for(
                            lambda: self._running
                            < self.max_concurrency
                        )
                        self._waiting -= 1
                        self._running += 1

                waiter = asyncio.create_task(wait_for_slot())
                try:
                    done, _ = await asyncio.wait(
                        {waiter},
                        timeout=timeout,
                    )
                except BaseException:
                    waiter.cancel()
                    await asyncio.gather(
                        waiter,
                        return_exceptions=True,
                    )
                    raise
                if not done:
                    waiter.cancel()
                    await asyncio.gather(
                        waiter,
                        return_exceptions=True,
                    )
                    if (
                        total_timeout_seconds is not None
                        and monotonic() - started
                        >= total_timeout_seconds
                    ):
                        raise BoundedExecutionTimeoutError(
                            "total_timeout"
                        ) from None
                    raise BoundedExecutionBusyError(
                        "queue_timeout"
                    ) from None
                await waiter
                acquired = True
                queued = False

            remaining_budget: float | None = None
            if total_timeout_seconds is not None:
                remaining_budget = total_timeout_seconds - (
                    monotonic() - started
                )
                if remaining_budget <= 0:
                    raise BoundedExecutionTimeoutError("total_timeout")
            pending = operation()
            if remaining_budget is None:
                return await pending
            task = asyncio.create_task(pending)
            done, _ = await asyncio.wait(
                {task},
                timeout=remaining_budget,
            )
            if not done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise BoundedExecutionTimeoutError(
                    "total_timeout"
                ) from None
            return await task
        finally:
            if queued:
                async with self._condition:
                    self._waiting -= 1
            if acquired:
                async with self._condition:
                    self._running -= 1
                    self._condition.notify(1)
