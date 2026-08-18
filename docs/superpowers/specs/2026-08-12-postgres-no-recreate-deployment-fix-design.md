# PostgreSQL No-Recreate Deployment Fix

## Problem

Production releases live in versioned directories under
`/srv/weiquan/releases`. The PostgreSQL service bind-mounts
`deploy/postgres/postgresql.conf` from the active release. Even when that file
is unchanged, moving to a new release changes the absolute bind-mount source.
Docker Compose therefore plans to recreate the healthy PostgreSQL container
when `deploy.sh` runs `compose up -d postgres`.

The named data volume would survive that recreation, but an application-only
release must not replace or interrupt the production database container.

## Decision

Start the PostgreSQL service with:

```bash
compose up -d --no-recreate postgres
```

`--no-recreate` preserves an existing container even when Compose detects a
configuration difference. It still creates the service during an explicitly
confirmed initial deployment and starts it if the existing container is
stopped. The existing bounded SQL readiness probe remains authoritative: if
the preserved container does not become queryable, deployment stops before
backup, migration, or application replacement.

Changing PostgreSQL configuration remains a separate maintenance operation
with its own approval, backup, and downtime plan.

## Verification

- Contract tests require the guarded command and its ordering before the
  PostgreSQL readiness probe.
- Bash parses all deployment scripts successfully.
- A production `docker compose --dry-run` must not report PostgreSQL
  recreation.
- Deployment records the PostgreSQL container ID and named volume before and
  after release; both must remain unchanged.

