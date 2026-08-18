from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

_UUID = UUID(as_uuid=False)
_EMPTY_OBJECT = text("'{}'::jsonb")
_EMPTY_ARRAY = text("'[]'::jsonb")


users = Table(
    "users",
    metadata,
    Column("id", _UUID, primary_key=True),
    Column("email_normalized", String(320)),
    Column("password_hash", Text),
    Column(
        "status",
        String(30),
        nullable=False,
        server_default=text("'local'"),
    ),
    Column(
        "role",
        String(20),
        nullable=False,
        server_default=text("'user'"),
    ),
    Column("verified_at", DateTime(timezone=True)),
    Column("disabled_at", DateTime(timezone=True)),
    Column("deleted_at", DateTime(timezone=True)),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
    CheckConstraint(
        "status IN ("
        "'local', 'pending_verification', 'active', 'disabled', 'deleted'"
        ")",
        name="status_allowed",
    ),
    CheckConstraint(
        "role IN ('user', 'admin')",
        name="role_allowed",
    ),
    CheckConstraint(
        "(status = 'local' AND email_normalized IS NULL "
        "AND password_hash IS NULL) OR "
        "(status IN ('pending_verification', 'active', 'disabled') "
        "AND email_normalized IS NOT NULL AND password_hash IS NOT NULL) "
        "OR (status = 'deleted' AND email_normalized IS NULL "
        "AND password_hash IS NULL)",
        name="account_credentials_state",
    ),
)
Index(
    "uq_users_email_normalized",
    users.c.email_normalized,
    unique=True,
)
Index(
    "ix_users_status_created",
    users.c.status,
    users.c.created_at,
)


registration_capacity = Table(
    "registration_capacity",
    metadata,
    Column("id", SmallInteger, primary_key=True),
    Column(
        "capacity_limit",
        Integer,
        nullable=False,
        server_default=text("100"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
    CheckConstraint("id = 1", name="singleton"),
    CheckConstraint("capacity_limit >= 1", name="limit_positive"),
)


auth_sessions = Table(
    "auth_sessions",
    metadata,
    Column("id", _UUID, primary_key=True),
    Column(
        "user_id",
        _UUID,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("token_digest", String(64), nullable=False, unique=True),
    Column("csrf_digest", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
)
Index(
    "ix_auth_sessions_user_expires",
    auth_sessions.c.user_id,
    auth_sessions.c.expires_at,
)
Index(
    "ix_auth_sessions_expires",
    auth_sessions.c.expires_at,
)


auth_tokens = Table(
    "auth_tokens",
    metadata,
    Column("id", _UUID, primary_key=True),
    Column(
        "user_id",
        _UUID,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("purpose", String(30), nullable=False),
    Column("token_digest", String(64), nullable=False, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("consumed_at", DateTime(timezone=True)),
    Column("invalidated_at", DateTime(timezone=True)),
    CheckConstraint(
        "purpose IN ('email_verification', 'password_reset')",
        name="purpose_allowed",
    ),
)
Index(
    "ix_auth_tokens_user_purpose_expires",
    auth_tokens.c.user_id,
    auth_tokens.c.purpose,
    auth_tokens.c.expires_at,
)
Index("ix_auth_tokens_expires", auth_tokens.c.expires_at)


privacy_acceptances = Table(
    "privacy_acceptances",
    metadata,
    Column("id", _UUID, primary_key=True),
    Column(
        "user_id",
        _UUID,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("context", String(30), nullable=False),
    Column("policy_version", String(100), nullable=False),
    Column("accepted_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "context IN ('registration', 'consultation')",
        name="context_allowed",
    ),
    UniqueConstraint(
        "user_id",
        "context",
        "policy_version",
        name="uq_privacy_acceptances_user_context_version",
    ),
)
Index(
    "ix_privacy_acceptances_user_context",
    privacy_acceptances.c.user_id,
    privacy_acceptances.c.context,
)


security_rate_limits = Table(
    "security_rate_limits",
    metadata,
    Column("id", _UUID, primary_key=True),
    Column("action", String(50), nullable=False),
    Column("key_digest", String(64), nullable=False),
    Column(
        "window_started_at",
        DateTime(timezone=True),
        nullable=False,
    ),
    Column(
        "request_count",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "request_count >= 0",
        name="request_count_nonnegative",
    ),
    UniqueConstraint(
        "action",
        "key_digest",
        "window_started_at",
        name="uq_security_rate_limits_action_key_window",
    ),
)
Index(
    "ix_security_rate_limits_expires",
    security_rate_limits.c.expires_at,
)


trial_identities = Table(
    "trial_identities",
    metadata,
    Column("id", _UUID, primary_key=True),
    Column("token_digest", String(64), nullable=False, unique=True),
    Column("ip_digest", String(64), nullable=False),
    Column("policy_version", String(100), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "length(token_digest) = 64",
        name="token_digest_length",
    ),
    CheckConstraint(
        "length(ip_digest) = 64",
        name="ip_digest_length",
    ),
    CheckConstraint(
        "expires_at > created_at",
        name="expiry_after_creation",
    ),
)
Index(
    "ix_trial_identities_expires",
    trial_identities.c.expires_at,
)
Index(
    "ix_trial_identities_ip_created",
    trial_identities.c.ip_digest,
    trial_identities.c.created_at,
)


trial_ip_grants = Table(
    "trial_ip_grants",
    metadata,
    Column("id", _UUID, primary_key=True),
    Column(
        "identity_id",
        _UUID,
        ForeignKey("trial_identities.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    Column("ip_digest", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "length(ip_digest) = 64",
        name="ip_digest_length",
    ),
    CheckConstraint(
        "expires_at > created_at",
        name="expiry_after_creation",
    ),
)
Index(
    "ix_trial_ip_grants_ip_expires",
    trial_ip_grants.c.ip_digest,
    trial_ip_grants.c.expires_at,
)
Index(
    "ix_trial_ip_grants_expires",
    trial_ip_grants.c.expires_at,
)


quota_buckets = Table(
    "quota_buckets",
    metadata,
    Column("id", _UUID, primary_key=True),
    Column("bucket_type", String(50), nullable=False),
    Column("subject_key", String(100), nullable=False),
    Column("period_key", String(20), nullable=False),
    Column(
        "used_count",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    Column("limit_count", Integer, nullable=False),
    Column("resets_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "used_count >= 0",
        name="used_count_nonnegative",
    ),
    CheckConstraint(
        "limit_count >= 1",
        name="limit_count_positive",
    ),
    CheckConstraint(
        "used_count <= limit_count",
        name="used_not_above_limit",
    ),
    UniqueConstraint(
        "bucket_type",
        "subject_key",
        "period_key",
        name="uq_quota_buckets_scope_period",
    ),
)
Index(
    "ix_quota_buckets_reset",
    quota_buckets.c.resets_at,
)


quota_reservations = Table(
    "quota_reservations",
    metadata,
    Column("id", _UUID, primary_key=True),
    Column("kind", String(20), nullable=False),
    Column("subject_id", String(100), nullable=False),
    Column("logical_call_id", String(100), nullable=False),
    Column("status", String(20), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "kind IN ('trial', 'registered')",
        name="kind_allowed",
    ),
    CheckConstraint(
        "status IN ('reserved', 'succeeded', 'refunded')",
        name="status_allowed",
    ),
    UniqueConstraint(
        "kind",
        "subject_id",
        "logical_call_id",
        name="uq_quota_reservations_logical_call",
    ),
)
Index(
    "ix_quota_reservations_status_created",
    quota_reservations.c.status,
    quota_reservations.c.created_at,
)
Index(
    "ix_quota_reservations_subject_created",
    quota_reservations.c.kind,
    quota_reservations.c.subject_id,
    quota_reservations.c.created_at,
)


quota_reservation_buckets = Table(
    "quota_reservation_buckets",
    metadata,
    Column(
        "reservation_id",
        _UUID,
        ForeignKey("quota_reservations.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "bucket_id",
        _UUID,
        ForeignKey("quota_buckets.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
)
Index(
    "ix_quota_reservation_buckets_bucket",
    quota_reservation_buckets.c.bucket_id,
)


provider_call_results = Table(
    "provider_call_results",
    metadata,
    Column("id", _UUID, primary_key=True),
    Column("logical_call_id", _UUID, nullable=False),
    Column("provider", String(50), nullable=False),
    Column("model", String(200), nullable=False),
    Column("outcome", String(50), nullable=False),
    Column("duration_ms", Integer, nullable=False),
    Column(
        "input_tokens",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    Column(
        "output_tokens",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    Column(
        "total_tokens",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "outcome IN ("
        "'success', 'timeout', 'network_error', 'rate_limited', "
        "'server_error', 'invalid_output', 'rejected', "
        "'configuration_error', 'provider_error'"
        ")",
        name="outcome_allowed",
    ),
    CheckConstraint(
        "duration_ms >= 0",
        name="duration_nonnegative",
    ),
    CheckConstraint(
        "input_tokens >= 0 AND output_tokens >= 0 "
        "AND total_tokens >= 0",
        name="token_counts_nonnegative",
    ),
)
Index(
    "ix_provider_call_results_provider_occurred",
    provider_call_results.c.provider,
    provider_call_results.c.occurred_at,
)
Index(
    "ix_provider_call_results_logical_call",
    provider_call_results.c.logical_call_id,
)


provider_health_states = Table(
    "provider_health_states",
    metadata,
    Column("provider", String(50), primary_key=True),
    Column("status", String(20), nullable=False),
    Column("sample_count", Integer, nullable=False),
    Column("failure_count", Integer, nullable=False),
    Column("consecutive_successes", Integer, nullable=False),
    Column("consecutive_failures", Integer, nullable=False),
    Column("last_result_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "status IN ('unknown', 'healthy', 'degraded')",
        name="status_allowed",
    ),
    CheckConstraint(
        "sample_count BETWEEN 0 AND 10 "
        "AND failure_count BETWEEN 0 AND sample_count "
        "AND consecutive_successes BETWEEN 0 AND 10 "
        "AND consecutive_failures BETWEEN 0 AND 10",
        name="counts_valid",
    ),
)


admin_audit_events = Table(
    "admin_audit_events",
    metadata,
    Column("id", _UUID, primary_key=True),
    Column("admin_id", _UUID, nullable=False),
    Column("target_user_id", _UUID, nullable=False),
    Column("action", String(50), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("result", String(20), nullable=False),
    CheckConstraint(
        "action IN ('revoke_sessions', 'disable_user')",
        name="action_allowed",
    ),
    CheckConstraint(
        "result IN ('succeeded', 'not_found')",
        name="result_allowed",
    ),
)
Index(
    "ix_admin_audit_events_occurred",
    admin_audit_events.c.occurred_at,
)
Index(
    "ix_admin_audit_events_admin_occurred",
    admin_audit_events.c.admin_id,
    admin_audit_events.c.occurred_at,
)


consultation_sessions = Table(
    "consultation_sessions",
    metadata,
    Column("id", _UUID, primary_key=True),
    Column(
        "owner_id",
        _UUID,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("scenario_id", String(100)),
    Column(
        "facts",
        JSONB,
        nullable=False,
        server_default=_EMPTY_OBJECT,
    ),
    Column(
        "followup_round",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    Column("status", String(30), nullable=False),
    Column("jurisdiction", String(100)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("deleted_at", DateTime(timezone=True)),
    CheckConstraint(
        "followup_round BETWEEN 0 AND 2",
        name="followup_round_range",
    ),
    CheckConstraint(
        "status IN ("
        "'collecting', 'need_more_facts', 'ready', 'escalate', 'error'"
        ")",
        name="status_allowed",
    ),
    UniqueConstraint(
        "owner_id",
        "id",
        name="uq_consultation_sessions_owner_id_id",
    ),
)
Index(
    "ix_consultation_sessions_owner_updated",
    consultation_sessions.c.owner_id,
    consultation_sessions.c.updated_at,
)
Index(
    "ix_consultation_sessions_expires",
    consultation_sessions.c.expires_at,
)
Index(
    "ix_consultation_sessions_deleted_expires",
    consultation_sessions.c.deleted_at,
    consultation_sessions.c.expires_at,
)


consultation_deletion_outbox = Table(
    "consultation_deletion_outbox",
    metadata,
    Column("session_id", _UUID, primary_key=True),
    Column("deleted_at", DateTime(timezone=True), nullable=False),
    Column("manifest_uploaded_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    Column("last_attempted_at", DateTime(timezone=True)),
    Column("last_error_category", String(50)),
    CheckConstraint(
        "manifest_uploaded_at IS NULL "
        "OR manifest_uploaded_at >= deleted_at",
        name="manifest_upload_after_deletion",
    ),
    CheckConstraint(
        "completed_at IS NULL OR ("
        "manifest_uploaded_at IS NOT NULL "
        "AND completed_at >= deleted_at"
        ")",
        name="completion_after_manifest",
    ),
    CheckConstraint(
        "last_error_category IS NULL OR last_error_category IN ("
        "'encryption_failed', 'upload_failed', 'storage_failed'"
        ")",
        name="error_category_allowed",
    ),
)
Index(
    "ix_consultation_deletion_outbox_completed",
    consultation_deletion_outbox.c.completed_at,
    consultation_deletion_outbox.c.deleted_at,
)


consultation_turns = Table(
    "consultation_turns",
    metadata,
    Column("id", _UUID, primary_key=True),
    Column("owner_id", _UUID, nullable=False),
    Column("session_id", _UUID, nullable=False),
    Column("user_message", Text, nullable=False),
    Column(
        "facts",
        JSONB,
        nullable=False,
        server_default=_EMPTY_OBJECT,
    ),
    Column(
        "rule_matches",
        JSONB,
        nullable=False,
        server_default=_EMPTY_ARRAY,
    ),
    Column(
        "response",
        JSONB,
        nullable=False,
        server_default=_EMPTY_OBJECT,
    ),
    Column("provider_name", String(50)),
    Column("provider_model", String(200)),
    Column("provider_request_id", String(200)),
    Column(
        "input_tokens",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    Column(
        "output_tokens",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    Column(
        "total_tokens",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    Column("estimated_cost_usd", Numeric(18, 8)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ("owner_id", "session_id"),
        (
            "consultation_sessions.owner_id",
            "consultation_sessions.id",
        ),
        ondelete="CASCADE",
    ),
    CheckConstraint(
        "input_tokens >= 0 AND output_tokens >= 0 "
        "AND total_tokens >= 0",
        name="token_counts_nonnegative",
    ),
    CheckConstraint(
        "estimated_cost_usd IS NULL OR estimated_cost_usd >= 0",
        name="estimated_cost_nonnegative",
    ),
    UniqueConstraint(
        "owner_id",
        "id",
        name="uq_consultation_turns_owner_id_id",
    ),
    UniqueConstraint(
        "owner_id",
        "session_id",
        "id",
        name="uq_consultation_turns_owner_session_id",
    ),
)
Index(
    "ix_consultation_turns_owner_session_created",
    consultation_turns.c.owner_id,
    consultation_turns.c.session_id,
    consultation_turns.c.created_at,
)


consultation_attachments = Table(
    "consultation_attachments",
    metadata,
    Column("id", _UUID, primary_key=True),
    Column("owner_id", _UUID, nullable=False),
    Column("session_id", _UUID),
    Column("turn_id", _UUID),
    Column("turn_position", Integer),
    Column("status", String(30), nullable=False),
    Column("original_name", String(255), nullable=False),
    Column("media_type", String(100), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("page_count", Integer),
    Column("extraction_method", String(30)),
    Column("confirmed_text", Text),
    Column(
        "warnings",
        JSONB,
        nullable=False,
        server_default=_EMPTY_ARRAY,
    ),
    Column("error_code", String(100)),
    Column("reservation_id", _UUID),
    Column("reserved_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True)),
    ForeignKeyConstraint(
        ("owner_id", "session_id"),
        (
            "consultation_sessions.owner_id",
            "consultation_sessions.id",
        ),
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ("owner_id", "session_id", "turn_id"),
        (
            "consultation_turns.owner_id",
            "consultation_turns.session_id",
            "consultation_turns.id",
        ),
        ondelete="CASCADE",
    ),
    CheckConstraint(
        "status IN ("
        "'processing', 'review_required', 'confirmed', 'failed', 'bound'"
        ")",
        name="status_allowed",
    ),
    CheckConstraint(
        "size_bytes >= 0",
        name="size_nonnegative",
    ),
    CheckConstraint(
        "page_count IS NULL OR page_count >= 1",
        name="page_count_positive",
    ),
    CheckConstraint(
        "turn_position IS NULL OR turn_position BETWEEN 0 AND 2",
        name="turn_position_range",
    ),
    CheckConstraint(
        "(status = 'bound' AND session_id IS NOT NULL "
        "AND turn_id IS NOT NULL AND turn_position IS NOT NULL "
        "AND reservation_id IS NULL AND reserved_at IS NULL "
        "AND expires_at IS NULL) OR "
        "(status <> 'bound' AND session_id IS NULL AND turn_id IS NULL "
        "AND expires_at IS NOT NULL)",
        name="binding_state",
    ),
    CheckConstraint(
        "(status IN ('confirmed', 'bound') "
        "AND confirmed_text IS NOT NULL "
        "AND length(btrim(confirmed_text)) > 0) OR "
        "(status IN ('processing', 'review_required', 'failed') "
        "AND confirmed_text IS NULL)",
        name="confirmed_text_state",
    ),
    UniqueConstraint(
        "owner_id",
        "id",
        name="uq_consultation_attachments_owner_id_id",
    ),
)
Index(
    "ix_consultation_attachments_owner_expires",
    consultation_attachments.c.owner_id,
    consultation_attachments.c.expires_at,
)
Index(
    "ix_consultation_attachments_owner_turn",
    consultation_attachments.c.owner_id,
    consultation_attachments.c.turn_id,
    consultation_attachments.c.turn_position,
)
Index(
    "uq_consultation_attachments_owner_reservation_position",
    consultation_attachments.c.owner_id,
    consultation_attachments.c.reservation_id,
    consultation_attachments.c.turn_position,
    unique=True,
    postgresql_where=(
        consultation_attachments.c.reservation_id.is_not(None)
    ),
)
Index(
    "uq_consultation_attachments_owner_turn_position",
    consultation_attachments.c.owner_id,
    consultation_attachments.c.turn_id,
    consultation_attachments.c.turn_position,
    unique=True,
    postgresql_where=consultation_attachments.c.turn_id.is_not(None),
)


content_audit_records = Table(
    "content_audit_records",
    metadata,
    Column("id", _UUID, primary_key=True),
    Column("owner_id", _UUID, nullable=False),
    Column("audit_id", _UUID, nullable=False),
    Column("session_id", _UUID, nullable=False),
    Column("turn_id", _UUID),
    Column("stage", String(100), nullable=False),
    Column("status", String(30), nullable=False),
    Column(
        "duration_ms",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    Column("playbook_id", String(100)),
    Column("playbook_version", String(50)),
    Column(
        "citations",
        JSONB,
        nullable=False,
        server_default=_EMPTY_ARRAY,
    ),
    Column("error_category", String(100)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ("owner_id", "session_id"),
        (
            "consultation_sessions.owner_id",
            "consultation_sessions.id",
        ),
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ("owner_id", "session_id", "turn_id"),
        (
            "consultation_turns.owner_id",
            "consultation_turns.session_id",
            "consultation_turns.id",
        ),
        ondelete="CASCADE",
    ),
    CheckConstraint(
        "status IN ('started', 'ok', 'error', 'degraded')",
        name="status_allowed",
    ),
    CheckConstraint(
        "duration_ms >= 0",
        name="duration_nonnegative",
    ),
)
Index(
    "ix_content_audit_records_owner_session",
    content_audit_records.c.owner_id,
    content_audit_records.c.session_id,
    content_audit_records.c.created_at,
)
Index(
    "ix_content_audit_records_owner_audit",
    content_audit_records.c.owner_id,
    content_audit_records.c.audit_id,
    content_audit_records.c.created_at,
)
