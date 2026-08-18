from __future__ import annotations

import asyncio

from fastapi import FastAPI

from app.config import Settings
from app.deps import initialize_execution_dependencies


def test_execution_dependencies_are_singletons_per_application() -> None:
    application = FastAPI()
    application.state.settings = Settings(
        _env_file=None,
        deployment_mode="test",
    )

    first = initialize_execution_dependencies(application)
    second = initialize_execution_dependencies(application)

    assert all(left is right for left, right in zip(first, second))
    ocr, deepseek, _ = first
    assert ocr.max_concurrency == 1
    assert deepseek.max_concurrency == 2


def test_two_consultations_and_one_ocr_run_with_bounded_resources() -> None:
    application = FastAPI()
    application.state.settings = Settings(
        _env_file=None,
        deployment_mode="test",
    )
    ocr, deepseek, _ = initialize_execution_dependencies(application)

    async def exercise() -> None:
        release = asyncio.Event()
        all_started = asyncio.Event()
        running = {"deepseek": 0, "ocr": 0, "total": 0}
        observed_max = {"deepseek": 0, "ocr": 0}

        async def operation(kind: str) -> str:
            running[kind] += 1
            running["total"] += 1
            observed_max[kind] = max(
                observed_max[kind],
                running[kind],
            )
            if running["total"] == 3:
                all_started.set()
            try:
                await release.wait()
                return kind
            finally:
                running[kind] -= 1
                running["total"] -= 1

        tasks = [
            asyncio.create_task(
                deepseek.run(lambda: operation("deepseek"))
            ),
            asyncio.create_task(
                deepseek.run(lambda: operation("deepseek"))
            ),
            asyncio.create_task(ocr.run(lambda: operation("ocr"))),
        ]
        await asyncio.wait_for(all_started.wait(), timeout=1)

        assert deepseek.snapshot().running == 2
        assert deepseek.snapshot().waiting == 0
        assert ocr.snapshot().running == 1
        assert ocr.snapshot().waiting == 0

        release.set()
        assert sorted(await asyncio.gather(*tasks)) == [
            "deepseek",
            "deepseek",
            "ocr",
        ]
        assert observed_max == {"deepseek": 2, "ocr": 1}

    asyncio.run(exercise())

    assert deepseek.snapshot().running == 0
    assert deepseek.snapshot().waiting == 0
    assert ocr.snapshot().running == 0
    assert ocr.snapshot().waiting == 0
