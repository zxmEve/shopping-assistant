"""Add multi-level cost budgets and immutable budget ledger.

Revision ID: 20260911_0009
Revises: 20260910_0008
Create Date: 2026-09-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260911_0009"
down_revision: str | None = "20260910_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_document = postgresql.JSONB(astext_type=sa.Text())
    op.add_column("tool_calls", sa.Column("lease_owner", sa.String(length=64)))
    op.add_column("tool_calls", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column(
        "tool_calls",
        sa.Column("fencing_token", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "budget_policies",
        sa.Column("policy_id", sa.String(length=64), primary_key=True),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.String(length=256), nullable=False),
        sa.Column("provider_kind", sa.String(length=32)),
        sa.Column("provider_id", sa.String(length=256)),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("period_type", sa.String(length=32), nullable=False),
        sa.Column("period_timezone", sa.String(length=64), nullable=False),
        sa.Column("hard_limit", sa.BigInteger(), nullable=False),
        sa.Column("soft_limit", sa.BigInteger()),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "scope_type",
            "scope_key",
            "provider_kind",
            "provider_id",
            "metric",
            "period_type",
            "version",
            name="uq_budget_policies_scope_version",
        ),
    )
    op.create_index(
        "ix_budget_policies_lookup",
        "budget_policies",
        ["scope_type", "scope_key", "enabled"],
    )

    op.create_table(
        "budget_reservations",
        sa.Column("reservation_id", sa.String(length=64), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("tool_call_id", sa.String(length=64)),
        sa.Column("agent_execution_id", sa.String(length=64)),
        sa.Column("provider_invocation_id", sa.String(length=64)),
        sa.Column("invocation_kind", sa.String(length=32), nullable=False),
        sa.Column("provider_kind", sa.String(length=32), nullable=False),
        sa.Column("provider_id", sa.String(length=256), nullable=False),
        sa.Column("model", sa.String(length=256)),
        sa.Column("pricing_version", sa.String(length=128), nullable=False),
        sa.Column("reserved_amount", sa.BigInteger(), nullable=False),
        sa.Column("actual_amount", sa.BigInteger()),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_request_id", sa.String(length=256)),
        sa.Column("settlement_basis", sa.String(length=64)),
        sa.Column("applied_limits_json", json_document, nullable=False),
        sa.Column(
            "metadata_json",
            json_document,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_budget_reservations_idempotency"),
    )
    op.create_index(
        "ix_budget_reservations_status_expiry",
        "budget_reservations",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_budget_reservations_tenant_user",
        "budget_reservations",
        ["tenant_id", "user_id"],
    )

    op.create_table(
        "budget_ledger_entries",
        sa.Column("entry_id", sa.String(length=64), primary_key=True),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("reservation_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.String(length=256), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delta_reserved", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("delta_committed", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("policy_id", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "metadata_json",
            json_document,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.UniqueConstraint(
            "event_id",
            "scope_type",
            "scope_key",
            "period_start",
            "metric",
            name="uq_budget_ledger_event_scope",
        ),
    )
    op.create_index(
        "ix_budget_ledger_scope_period",
        "budget_ledger_entries",
        ["scope_type", "scope_key", "period_start"],
    )
    op.create_index(
        "ix_budget_ledger_reservation",
        "budget_ledger_entries",
        ["reservation_id", "occurred_at"],
    )

    op.create_table(
        "budget_outbox",
        sa.Column("outbox_id", sa.String(length=64), primary_key=True),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("reservation_id", sa.String(length=64), nullable=False),
        sa.Column("payload_json", json_document, nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("event_id", name="uq_budget_outbox_event"),
    )
    op.create_index(
        "ix_budget_outbox_pending", "budget_outbox", ["processed_at", "available_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_budget_outbox_pending", table_name="budget_outbox")
    op.drop_table("budget_outbox")
    op.drop_index("ix_budget_ledger_reservation", table_name="budget_ledger_entries")
    op.drop_index("ix_budget_ledger_scope_period", table_name="budget_ledger_entries")
    op.drop_table("budget_ledger_entries")
    op.drop_index("ix_budget_reservations_tenant_user", table_name="budget_reservations")
    op.drop_index("ix_budget_reservations_status_expiry", table_name="budget_reservations")
    op.drop_table("budget_reservations")
    op.drop_index("ix_budget_policies_lookup", table_name="budget_policies")
    op.drop_table("budget_policies")
    op.drop_column("tool_calls", "fencing_token")
    op.drop_column("tool_calls", "lease_expires_at")
    op.drop_column("tool_calls", "lease_owner")
