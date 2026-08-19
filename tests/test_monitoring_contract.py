from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "monitoring" / "check-services.sh"


def test_monitor_covers_required_resource_and_service_signals() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    for marker in (
        "/proc/loadavg",
        "MemAvailable",
        "SwapFree",
        "DISK_MAX_USED_PERCENT",
        "RestartCount",
        "/live",
        "/ready",
        "/internal/metrics",
        "nginx",
        "5xx",
        "queue",
        "provider",
        "mail",
        "captcha",
        "attachment",
        "last-success.json",
    ):
        assert marker in source
    assert "set -Eeuo pipefail" in source
    assert "set -x" not in source
    assert "-v load=" not in source
    assert "-v current_load=" in source
    assert "exit 1" in source


def test_monitoring_units_are_bounded_and_non_overlapping() -> None:
    service = (
        ROOT / "deploy" / "systemd" / "weiquan-monitor.service"
    ).read_text(encoding="utf-8")
    timer = (
        ROOT / "deploy" / "systemd" / "weiquan-monitor.timer"
    ).read_text(encoding="utf-8")

    assert "Type=oneshot" in service
    assert "TimeoutStartSec=" in service
    assert "flock" in service
    assert (
        "/usr/bin/bash "
        "/srv/weiquan/current/deploy/monitoring/check-services.sh"
        in service
    )
    assert "OnUnitActiveSec=5min" in timer
    assert "Persistent=true" in timer


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")
def test_monitoring_shell_script_parses() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_monitoring_runbook_has_thresholds_and_response_paths() -> None:
    source = (
        ROOT / "docs" / "runbooks" / "monitoring-and-alerts.md"
    ).read_text(encoding="utf-8")

    for marker in (
        "80%",
        "swap",
        "容器重启",
        "备份失败",
        "5xx",
        "Provider",
        "DirectMail",
        "CAPTCHA",
        "临时附件",
        "关闭新咨询",
    ):
        assert marker in source
