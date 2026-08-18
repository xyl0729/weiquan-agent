from __future__ import annotations

import ast
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = ROOT / "deploy" / "backup"
BASH_EXECUTABLE = shutil.which("bash")
if BASH_EXECUTABLE is None and os.name == "nt":
    git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if git_bash.is_file():
        BASH_EXECUTABLE = str(git_bash)


def _source(name: str) -> str:
    return (BACKUP_DIR / name).read_text(encoding="utf-8")


def _bash_path(path: Path) -> str:
    resolved = path.resolve()
    if os.name != "nt":
        return resolved.as_posix()
    drive = resolved.drive.rstrip(":").lower()
    return f"/{drive}{resolved.as_posix()[2:]}"


def _write_fake_command(directory: Path, name: str, source: str) -> None:
    path = directory / name
    path.write_text(
        f"#!/usr/bin/env bash\nset -Eeuo pipefail\n{source}",
        encoding="utf-8",
        newline="\n",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_backup_is_tmpfs_first_encrypted_verified_and_then_uploaded() -> None:
    source = _source("backup-postgres.sh")

    assert "set -Eeuo pipefail" in source
    assert "set -x" not in source
    assert "/dev/shm" in source
    assert "pg_dump" in source
    assert "age --encrypt" in source
    assert "sha256sum --check" in source
    assert "oss_objects.py put" in source
    compatibility_check = '"$SCRIPT_DIR/check-postgres-compatibility.sh"'
    assert compatibility_check in source
    assert source.index(compatibility_check) < source.index(
        'PLAIN_DUMP="$(mktemp'
    )
    assert source.index("pg_dump") < source.index("age --encrypt")
    assert source.index("age --encrypt") < source.index(
        "sha256sum --check"
    )
    assert source.index("sha256sum --check") < source.index(
        "oss_objects.py put"
    )
    assert re.search(r"trap\s+cleanup\s+EXIT", source)
    assert "PGPASSWORD" not in re.sub(
        r'require_env "PGPASSWORD"',
        "",
        source,
    )


@pytest.mark.skipif(BASH_EXECUTABLE is None, reason="bash unavailable")
@pytest.mark.parametrize(
    ("client_version", "server_version_num", "expected_returncode"),
    (
        ("16.9", "160004", 0),
        ("15.18", "160004", 1),
        ("16.4", "150014", 1),
    ),
)
def test_postgres_compatibility_check_requires_major_16(
    tmp_path: Path,
    client_version: str,
    server_version_num: str,
    expected_returncode: int,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_command(
        bin_dir,
        "pg_dump",
        f'printf "%s\\n" "pg_dump (PostgreSQL) {client_version}"\n',
    )
    _write_fake_command(
        bin_dir,
        "psql",
        f'printf "%s\\n" "{server_version_num}"\n',
    )

    result = subprocess.run(
        [
            BASH_EXECUTABLE,
            "-c",
            'export PATH="$1:/usr/bin:/bin:$PATH"; exec /bin/bash "$2"',
            "bash",
            _bash_path(bin_dir),
            _bash_path(BACKUP_DIR / "check-postgres-compatibility.sh"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == expected_returncode
    if expected_returncode:
        assert (
            "backup_failed category=postgres_version_mismatch"
            in result.stderr
        )
    else:
        assert result.stderr == ""


def test_backup_retention_is_seven_daily_four_weekly_and_28_days() -> None:
    source = _source("prune-backups.sh")
    helper = _source("oss_objects.py")

    assert "--keep 7" in source
    assert "--keep 4" in source
    assert source.count("--max-age-days 28") == 2
    assert "select_prunable_groups" in helper
    assert "last_modified" in helper


def test_restore_is_isolated_verified_replayed_and_bounded() -> None:
    source = _source("restore-postgres.sh")

    assert 'ALLOW_ISOLATED_RESTORE:-' in source
    assert "sha256sum --check" in source
    assert "age --decrypt" in source
    assert "pg_restore" in source
    assert "alembic_version" in source
    assert "replay-deletions.sh" in source
    assert "consultation_sessions" in source
    assert "RESTORE_RTO_SECONDS" in source
    assert "restore-ready" in source
    assert source.index("sha256sum --check") < source.index(
        "pg_restore"
    )
    assert source.index("pg_restore") < source.index(
        "replay-deletions.sh"
    )
    assert source.index("replay-deletions.sh") < source.index(
        "restore-ready"
    )


def test_deletion_replay_validates_manifest_and_uses_parameterized_sql() -> None:
    source = _source("replay-deletions.sh")

    assert "deletion-manifests" in source
    assert "parse-deletion-manifest" in source
    assert "session_id" in source
    assert "deleted_at" in source
    assert "ON CONFLICT" in source
    assert ":'session_id'" in source
    assert "DELETE FROM consultation_sessions" in source


def test_backup_helper_has_no_import_time_network_or_secrets() -> None:
    path = BACKUP_DIR / "oss_objects.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    top_level_calls = [
        node
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
    ]
    assert top_level_calls == []
    assert "print(access_key" not in _source("oss_objects.py").casefold()


@pytest.mark.skipif(BASH_EXECUTABLE is None, reason="bash unavailable")
def test_backup_shell_scripts_parse() -> None:
    for name in (
        "backup-postgres.sh",
        "check-postgres-compatibility.sh",
        "restore-postgres.sh",
        "replay-deletions.sh",
        "prune-backups.sh",
    ):
        result = subprocess.run(
            [BASH_EXECUTABLE, "-n", str(BACKUP_DIR / name)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_backup_and_restore_runbook_records_rpo_rto_and_secret_boundary() -> None:
    source = (
        ROOT / "docs" / "runbooks" / "backup-and-restore.md"
    ).read_text(encoding="utf-8")

    for marker in (
        "24 小时",
        "4 小时",
        "age",
        "私钥",
        "隔离",
        "删除清单",
        "每月",
        "不得恢复公网写入",
        "/usr/bin/bash deploy/backup/restore-postgres.sh",
    ):
        assert marker in source
