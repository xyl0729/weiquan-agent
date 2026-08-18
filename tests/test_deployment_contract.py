from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "deploy" / "compose.production.yml"
SCRIPT_DIR = ROOT / "deploy" / "scripts"
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_docker_image_is_pinned_non_root_and_runtime_ready() -> None:
    source = _source(ROOT / "Dockerfile")

    assert re.search(r"^FROM python:3\.11\.\d+-slim-bookworm$", source, re.M)
    assert "ARG POSTGRESQL_MAJOR=16" in source
    assert "apt.postgresql.org/pub/repos/apt" in source
    assert "postgresql-client-${POSTGRESQL_MAJOR}" in source
    assert not re.search(r"^\s*postgresql-client\s*\\?$", source, re.M)
    assert "pg_dump --version" in source
    assert source.index("postgresql-client-${POSTGRESQL_MAJOR}") < (
        source.index("pg_dump --version")
    )
    assert re.search(r"\bage\b", source)
    assert (
        "ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/"
        in source
    )
    assert "ARG PIP_DEFAULT_TIMEOUT=120" in source
    assert "ARG PIP_RETRIES=10" in source
    assert '"pip==26.2.1"' in source
    assert '"setuptools==83.0.0"' in source
    assert "useradd" in source
    assert "USER 10001:10001" in source
    assert 'CMD ["python", "-m", "uvicorn"' in source
    assert '"--workers", "1"' in source
    assert "--reload" not in source


def test_shell_scripts_are_checked_out_with_linux_line_endings() -> None:
    attributes = _source(ROOT / ".gitattributes")

    assert "*.sh text eol=lf" in attributes
    for extension in ("pdf", "png", "jpg", "jpeg"):
        assert (
            f"tests/fixtures/attachments/*.{extension} -text -diff"
            in attributes
        )


def test_clean_checkout_builds_statute_database_from_tracked_seed() -> None:
    dockerfile = _source(ROOT / "Dockerfile")
    workflow = _source(CI_PATH)
    gitignore = _source(ROOT / ".gitignore")
    dockerignore = _source(ROOT / ".dockerignore")

    assert (ROOT / "data" / "seed_statutes.yaml").is_file()
    assert (ROOT / "data" / "retrieval_benchmark.yaml").is_file()
    assert "data/statutes.db" in gitignore
    assert "data/statutes.db" in dockerignore
    assert ".pip-cache" in dockerignore
    assert "COPY data/statutes.db" not in dockerfile
    assert (
        "COPY data/seed_statutes.yaml /app/data/seed_statutes.yaml"
        in dockerfile
    )
    assert (
        "COPY data/retrieval_benchmark.yaml "
        "/app/data/retrieval_benchmark.yaml"
        in dockerfile
    )
    for command in (
        "python scripts/ingest_statutes.py",
        "python scripts/verify_refs.py",
        "python scripts/check_recall.py",
    ):
        assert command in dockerfile
    assert dockerfile.index("COPY data/seed_statutes.yaml") < (
        dockerfile.index("python scripts/ingest_statutes.py")
    )
    assert dockerfile.index("python scripts/ingest_statutes.py") < (
        dockerfile.index("chmod 0444 /app/data/statutes.db")
    )

    ingest = "python scripts/ingest_statutes.py"
    test_suite = "python -m pytest -q"
    assert ingest in workflow
    assert workflow.index(ingest) < workflow.index(test_suite)


def test_production_compose_is_loopback_only_and_resource_bounded() -> None:
    source = _source(COMPOSE_PATH)
    document = yaml.safe_load(source)
    services = document["services"]

    assert set(services) == {"app", "postgres"}

    app = services["app"]
    assert app["ports"] == ["127.0.0.1:8001:8001"]
    assert app["mem_limit"] == "1024m"
    assert app["read_only"] is True
    assert app["user"] == "10001:10001"
    assert app["restart"] == "unless-stopped"
    assert "--workers" in app["command"]
    assert app["command"][app["command"].index("--workers") + 1] == "1"
    assert "/live" in str(app["healthcheck"]["test"])
    attachment_target = "/app/.runtime/attachments"
    assert app["environment"]["ATTACHMENT_TEMP_DIR"] == attachment_target
    attachment_mounts = [
        mount
        for mount in app["volumes"]
        if (
            isinstance(mount, dict)
            and mount.get("source") == "/srv/weiquan/attachments"
        )
    ]
    assert len(attachment_mounts) == 1
    attachment_mount = attachment_mounts[0]
    assert attachment_mount["target"] == attachment_target
    assert attachment_mount.get("read_only", False) is False
    target_path = PurePosixPath(attachment_target)
    assert target_path.is_relative_to(PurePosixPath("/app"))
    assert not target_path.is_relative_to(PurePosixPath("/app/app/web"))

    postgres = services["postgres"]
    assert "ports" not in postgres
    assert postgres["mem_limit"] == "512m"
    assert postgres["restart"] == "unless-stopped"
    assert "pg_isready" in str(postgres["healthcheck"]["test"])
    assert any(
        mount["target"] == "/etc/postgresql/postgresql.conf"
        and mount["read_only"] is True
        for mount in postgres["volumes"]
        if isinstance(mount, dict)
    )

    assert "/etc/weiquan/weiquan.env" in source
    assert "POSTGRES_PASSWORD:" not in source
    assert "DEEPSEEK_API_KEY:" not in source
    assert "8000:" not in source


def test_postgres_configuration_matches_small_ecs_budget() -> None:
    source = _source(
        ROOT / "deploy" / "postgres" / "postgresql.conf"
    )

    for setting in (
        "max_connections = 20",
        "shared_buffers = 128MB",
        "work_mem = 4MB",
        "maintenance_work_mem = 64MB",
        "timezone = 'UTC'",
    ):
        assert setting in source
    assert "listen_addresses = '*'" in source
    assert "log_statement = 'all'" not in source


def test_weiquan_nginx_is_https_only_and_hides_internal_routes() -> None:
    source = _source(
        ROOT / "deploy" / "nginx" / "weiquan.072988.xyz.conf"
    )

    assert source.count("server_name weiquan.072988.xyz;") == 2
    assert "listen 443 ssl http2;" in source
    assert "return 301 https://$host$request_uri;" in source
    assert re.search(
        r"location \^~ /\.well-known/acme-challenge/ \{"
        r"[^}]*root /var/www/letsencrypt;"
        r"[^}]*try_files \$uri =404;",
        source,
        re.S,
    )
    assert "proxy_pass http://127.0.0.1:8001;" in source
    assert re.search(
        r"location \^~ /internal/ \{[^}]*return 404;",
        source,
        re.S,
    )
    assert "client_max_body_size 11m;" in source
    assert "weiquan.access.log" in source
    assert "audio.access.log" not in source


def test_audio_nginx_is_read_only_mp3_and_independently_logged() -> None:
    source = _source(
        ROOT / "deploy" / "nginx" / "audio.072988.xyz.conf"
    )

    assert source.count("server_name audio.072988.xyz;") == 2
    assert "listen 443 ssl http2;" in source
    assert "root /srv/audio;" in source
    assert "autoindex off;" in source
    assert "location ~* \\.mp3$" in source
    assert "limit_except GET" in source
    assert "try_files $uri =404;" in source
    assert "audio.access.log" in source
    assert "weiquan.access.log" not in source
    assert "proxy_pass" not in source


def test_deploy_and_rollback_are_ordered_pinned_and_content_free() -> None:
    preflight = _source(SCRIPT_DIR / "preflight.sh")
    deploy = _source(SCRIPT_DIR / "deploy.sh")
    rollback = _source(SCRIPT_DIR / "rollback.sh")
    smoke = _source(SCRIPT_DIR / "smoke.sh")

    for source in (preflight, deploy, rollback, smoke):
        assert "set -Eeuo pipefail" in source
        assert "set -x" not in source

    for marker in (
        "docker compose",
        "config --quiet",
        "data/seed_statutes.yaml",
        "/etc/weiquan/weiquan.env",
        "nginx -t",
        "127.0.0.1:8001",
    ):
        assert marker in preflight

    assert "latest" in deploy
    assert "backup-postgres.sh" in deploy
    assert "alembic upgrade head" in deploy
    assert "smoke.sh" in deploy
    assert "POSTGRES_PROBE_ATTEMPTS=60" in deploy
    assert "POSTGRES_PROBE_INTERVAL_SECONDS=2" in deploy
    assert "for ((attempt = 1;" in deploy
    assert 'sleep "$POSTGRES_PROBE_INTERVAL_SECONDS"' in deploy
    assert "compose up -d postgres" not in deploy
    assert deploy.index(
        "compose up -d --no-recreate postgres"
    ) < deploy.index(
        "for ((attempt = 1;"
    )
    assert deploy.index("for ((attempt = 1;") < deploy.index(
        "backup-postgres.sh"
    )
    assert deploy.index("backup-postgres.sh") < deploy.index(
        "alembic upgrade head"
    )
    assert deploy.index("alembic upgrade head") < deploy.index(
        "smoke.sh"
    )
    assert "previous-image" in deploy
    assert 'IMAGE_SOURCE="${IMAGE_SOURCE:-pull}"' in deploy
    assert 'case "$IMAGE_SOURCE" in' in deploy
    assert "compose pull app postgres" in deploy
    assert "compose pull postgres" in deploy
    assert "compose build --pull app" in deploy
    assert "python scripts/verify_refs.py" in deploy

    assert '/usr/bin/bash "$SCRIPT_DIR/preflight.sh"' in deploy
    assert '/usr/bin/bash "$SCRIPT_DIR/smoke.sh"' in deploy
    assert '/usr/bin/bash "$SCRIPT_DIR/rollback.sh"' in deploy

    assert "previous-image" in rollback
    assert "alembic downgrade" not in rollback
    assert "smoke.sh" in rollback
    assert "restore-postgres.sh" not in rollback
    assert '/usr/bin/bash "$SCRIPT_DIR/preflight.sh"' in rollback
    assert '/usr/bin/bash "$SCRIPT_DIR/smoke.sh"' in rollback

    assert "/live" in smoke
    assert "/ready" in smoke
    assert "/internal/metrics" in smoke
    assert "/api/consult" not in smoke
    assert re.search(r"\bcurl\b", smoke)
    assert "-X POST" not in smoke
    assert "8000" not in deploy + rollback + smoke


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")
def test_deployment_shell_scripts_parse() -> None:
    for name in ("preflight.sh", "deploy.sh", "rollback.sh", "smoke.sh"):
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT_DIR / name)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_deployment_runbook_and_readme_cover_operator_boundaries() -> None:
    runbook = _source(
        ROOT / "docs" / "runbooks" / "deployment-and-rollback.md"
    )
    readme = _source(ROOT / "README.md")

    for marker in (
        "127.0.0.1:8001",
        "/etc/weiquan/weiquan.env",
        "IMAGE_REF",
        "preflight.sh",
        "deploy.sh",
        "rollback.sh",
        "smoke.sh",
        "ICP",
        "HTTPS",
        "DeepSeek",
        "8000",
        "IMAGE_SOURCE",
        "/srv/weiquan/current",
        "/etc/systemd/system",
        "/etc/nginx/sites-enabled",
        "/etc/logrotate.d/weiquan",
        "systemctl daemon-reload",
        "systemctl start weiquan-backup.service",
    ):
        assert marker in runbook
    assert "deploy/compose.production.yml" in readme
    assert "deployment-and-rollback.md" in readme


def test_logrotate_does_not_require_an_undeclared_host_user() -> None:
    source = _source(ROOT / "deploy" / "logrotate" / "weiquan")

    assert "copytruncate" in source
    assert "create 0600 weiquan weiquan" not in source
