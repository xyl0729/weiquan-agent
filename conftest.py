from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parent


def pytest_configure(config) -> None:
    """Keep pytest's temporary files inside this workspace per run."""
    explicit_basetemp = any(
        argument == "--basetemp"
        or argument.startswith("--basetemp=")
        for argument in config.invocation_params.args
    )
    if explicit_basetemp:
        return

    temp_root = PROJECT_ROOT / ".tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    run_id = (
        f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
        f"-{os.getpid()}-{uuid4().hex[:8]}"
    )
    config.option.basetemp = str(temp_root / f"pytest-{run_id}")
