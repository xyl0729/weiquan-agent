FROM python:3.11.13-slim-bookworm

ARG POSTGRESQL_MAJOR=16
ARG PGDG_KEY_SHA256=0144068502a1eddd2a0280ede10ef607d1ec592ce819940991203941564e8e76

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        ca-certificates \
        curl \
    && install -d -m 0755 /usr/share/keyrings \
    && curl --fail --silent --show-error --location \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        --output /usr/share/keyrings/postgresql.asc \
    && printf '%s  %s\n' \
        "$PGDG_KEY_SHA256" \
        /usr/share/keyrings/postgresql.asc \
        | sha256sum --check - \
    && printf '%s\n' \
        'deb [signed-by=/usr/share/keyrings/postgresql.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main' \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install --no-install-recommends --yes \
        age \
        libglib2.0-0 \
        libgl1 \
        libgomp1 \
        postgresql-client-${POSTGRESQL_MAJOR} \
    && test "$( \
        pg_dump --version \
            | sed -nE 's/^pg_dump \(PostgreSQL\) ([0-9]+).*/\1/p' \
        )" = "$POSTGRESQL_MAJOR" \
    && apt-get purge --auto-remove --yes curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 weiquan \
    && useradd \
        --uid 10001 \
        --gid 10001 \
        --no-create-home \
        --shell /usr/sbin/nologin \
        weiquan

WORKDIR /app

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_DEFAULT_TIMEOUT=120
ARG PIP_RETRIES=10

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade \
        "pip==26.2.1" \
        "setuptools==83.0.0" \
    && python -m pip install --requirement /app/requirements.txt

COPY app /app/app
COPY migrations /app/migrations
COPY scripts /app/scripts
COPY deploy/backup /app/deploy/backup
COPY alembic.ini /app/alembic.ini
COPY data/seed_statutes.yaml /app/data/seed_statutes.yaml
COPY data/retrieval_benchmark.yaml /app/data/retrieval_benchmark.yaml

RUN python scripts/ingest_statutes.py \
    && python scripts/verify_refs.py \
    && python scripts/check_recall.py

RUN install -d \
        -o 10001 \
        -g 10001 \
        /srv/weiquan/attachments \
        /srv/weiquan/backup-staging \
        /srv/weiquan/logs \
    && chmod 0555 /app \
    && chmod 0444 /app/data/statutes.db \
    && find /app/deploy/backup -type f -name '*.sh' -exec chmod 0555 {} +

USER 10001:10001

EXPOSE 8001

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
