from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.jobs.cleanup import CleanupJob


NOW = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)


class RecordingCleanupStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, datetime]] = []
        self.remaining = {
            "consultations": 3,
            "attachments": 4,
            "auth_sessions": 2,
            "auth_tokens": 5,
            "rate_limits": 6,
            "pending_users": 1,
            "trial_ip_grants": 7,
            "trial_identities": 8,
            "deletion_outbox": 9,
        }

    def _take(self, name: str, *, limit: int, cutoff: datetime) -> int:
        self.calls.append((name, limit, cutoff))
        count = min(limit, self.remaining[name])
        self.remaining[name] -= count
        return count

    def purge_expired_consultations(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> int:
        return self._take("consultations", limit=limit, cutoff=cutoff)

    def purge_expired_attachments(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> int:
        return self._take("attachments", limit=limit, cutoff=cutoff)

    def purge_expired_auth_sessions(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> int:
        return self._take("auth_sessions", limit=limit, cutoff=cutoff)

    def purge_expired_auth_tokens(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> int:
        return self._take("auth_tokens", limit=limit, cutoff=cutoff)

    def purge_expired_rate_limits(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> int:
        return self._take("rate_limits", limit=limit, cutoff=cutoff)

    def expire_pending_users(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> int:
        return self._take("pending_users", limit=limit, cutoff=cutoff)

    def purge_expired_trial_ip_grants(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> int:
        return self._take("trial_ip_grants", limit=limit, cutoff=cutoff)

    def purge_expired_trial_identities(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> int:
        return self._take("trial_identities", limit=limit, cutoff=cutoff)

    def purge_completed_deletion_outbox(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> int:
        return self._take("deletion_outbox", limit=limit, cutoff=cutoff)


class PendingDeletionProcessor:
    def __init__(self) -> None:
        self.limits: list[int] = []

    def resume_pending(self, *, limit: int) -> int:
        self.limits.append(limit)
        return min(limit, 11)


def test_cleanup_job_bounds_every_operation_and_reports_counts() -> None:
    store = RecordingCleanupStore()
    deletions = PendingDeletionProcessor()
    job = CleanupJob(
        store=store,
        deletion_processor=deletions,
        pending_user_ttl=timedelta(hours=24),
        deletion_outbox_retention=timedelta(days=35),
        now=lambda: NOW,
    )

    report = job.run_once(limit=2)

    assert deletions.limits == [2]
    assert all(limit == 2 for _, limit, _ in store.calls)
    assert report.to_dict() == {
        "pending_deletions": 2,
        "consultations": 2,
        "attachments": 2,
        "auth_sessions": 2,
        "auth_tokens": 2,
        "rate_limits": 2,
        "pending_users": 1,
        "trial_ip_grants": 2,
        "trial_identities": 2,
        "deletion_outbox": 2,
    }
    cutoffs = {name: cutoff for name, _, cutoff in store.calls}
    assert cutoffs["pending_users"] == NOW - timedelta(hours=24)
    assert cutoffs["deletion_outbox"] == NOW - timedelta(days=35)
    for name in (
        "consultations",
        "attachments",
        "auth_sessions",
        "auth_tokens",
        "rate_limits",
        "trial_ip_grants",
        "trial_identities",
    ):
        assert cutoffs[name] == NOW


def test_cleanup_job_is_repeatable_and_rejects_unbounded_batches() -> None:
    store = RecordingCleanupStore()
    job = CleanupJob(store=store, now=lambda: NOW)

    while any(store.remaining.values()):
        job.run_once(limit=3)

    assert all(value == 0 for value in job.run_once(limit=3).to_dict().values())
    with pytest.raises(ValueError, match="limit"):
        job.run_once(limit=0)
    with pytest.raises(ValueError, match="limit"):
        job.run_once(limit=1001)

