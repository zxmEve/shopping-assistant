from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from procure_pilot.domain.models import utc_now


class ExecutionStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class AgentExecution(BaseModel):
    execution_id: str = Field(default_factory=lambda: f"ax_{uuid4().hex[:16]}")
    tenant_id: str
    user_id: str
    run_id: str
    node_name: str
    agent_kind: str
    task_id: str
    state_version: int = Field(ge=0)
    input_fingerprint: str
    idempotency_key: str
    status: ExecutionStatus = ExecutionStatus.RUNNING
    attempt_count: int = Field(default=1, ge=1)
    result_json: dict[str, Any] | None = None
    error: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class ToolCall(BaseModel):
    tool_call_id: str = Field(default_factory=lambda: f"tc_{uuid4().hex[:16]}")
    tenant_id: str
    user_id: str
    run_id: str
    node_name: str
    agent_execution_id: str | None = None
    tool_name: str
    provider: str | None = None
    risk: str
    budget_pool: str
    input_fingerprint: str
    idempotency_key: str
    arguments_json: dict[str, Any] = Field(default_factory=dict)
    status: ExecutionStatus = ExecutionStatus.RUNNING
    attempt_count: int = Field(default=1, ge=1)
    lease_owner: str | None = Field(default_factory=lambda: f"worker_{uuid4().hex[:16]}")
    lease_expires_at: datetime | None = Field(
        default_factory=lambda: utc_now() + timedelta(minutes=5)
    )
    fencing_token: int = Field(default=1, ge=0)
    result_json: dict[str, Any] | None = None
    error: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)
