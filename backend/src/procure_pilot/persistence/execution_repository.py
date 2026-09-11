from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from procure_pilot.domain.execution import AgentExecution, ExecutionStatus, ToolCall
from procure_pilot.domain.models import utc_now
from procure_pilot.persistence.models import AgentExecutionRecord, ToolCallRecord


class ExecutionRepositoryPort(Protocol):
    def get_agent(self, idempotency_key: str) -> AgentExecution | None: ...

    def claim_agent(self, execution: AgentExecution) -> AgentExecution: ...

    def complete_agent(
        self, execution_id: str, result_json: dict, *, error: str | None = None
    ) -> AgentExecution: ...

    def fail_agent(self, execution_id: str, error: str) -> AgentExecution: ...

    def list_agents(self, run_id: str, *, tenant_id: str) -> list[AgentExecution]: ...

    def get_tool(self, idempotency_key: str) -> ToolCall | None: ...

    def claim_tool(self, tool_call: ToolCall) -> ToolCall: ...

    def complete_tool(
        self,
        tool_call_id: str,
        result_json: dict,
        *,
        error: str | None = None,
        expected_fencing_token: int | None = None,
    ) -> ToolCall: ...

    def fail_tool(
        self, tool_call_id: str, error: str, *, expected_fencing_token: int | None = None
    ) -> ToolCall: ...

    def list_tools(self, run_id: str, *, tenant_id: str) -> list[ToolCall]: ...


class InMemoryExecutionRepository:
    def __init__(self) -> None:
        self._agents: dict[str, AgentExecution] = {}
        self._agent_keys: dict[str, str] = {}
        self._tools: dict[str, ToolCall] = {}
        self._tool_keys: dict[str, str] = {}
        self._lock = threading.RLock()

    def get_agent(self, idempotency_key: str) -> AgentExecution | None:
        with self._lock:
            execution_id = self._agent_keys.get(idempotency_key)
            return (
                self._agents[execution_id].model_copy(deep=True)
                if execution_id is not None
                else None
            )

    def claim_agent(self, execution: AgentExecution) -> AgentExecution:
        with self._lock:
            current = self.get_agent(execution.idempotency_key)
            if current is not None:
                if current.status != ExecutionStatus.SUCCEEDED:
                    current.attempt_count += 1
                    current.status = ExecutionStatus.RUNNING
                    current.error = None
                    current.started_at = utc_now()
                    current.updated_at = current.started_at
                    self._agents[current.execution_id] = current.model_copy(deep=True)
                return current
            self._agents[execution.execution_id] = execution.model_copy(deep=True)
            self._agent_keys[execution.idempotency_key] = execution.execution_id
            return execution.model_copy(deep=True)

    def complete_agent(
        self, execution_id: str, result_json: dict, *, error: str | None = None
    ) -> AgentExecution:
        return self._finish_agent(execution_id, ExecutionStatus.SUCCEEDED, result_json, error)

    def fail_agent(self, execution_id: str, error: str) -> AgentExecution:
        return self._finish_agent(execution_id, ExecutionStatus.FAILED, None, error)

    def _finish_agent(
        self,
        execution_id: str,
        status: ExecutionStatus,
        result_json: dict | None,
        error: str | None,
    ) -> AgentExecution:
        with self._lock:
            item = self._agents[execution_id].model_copy(deep=True)
            item.status = status
            item.result_json = result_json
            item.error = error
            item.completed_at = utc_now()
            item.updated_at = item.completed_at
            self._agents[execution_id] = item.model_copy(deep=True)
            return item

    def list_agents(self, run_id: str, *, tenant_id: str) -> list[AgentExecution]:
        with self._lock:
            return [
                item.model_copy(deep=True)
                for item in self._agents.values()
                if item.run_id == run_id and item.tenant_id == tenant_id
            ]

    def get_tool(self, idempotency_key: str) -> ToolCall | None:
        with self._lock:
            tool_call_id = self._tool_keys.get(idempotency_key)
            return (
                self._tools[tool_call_id].model_copy(deep=True)
                if tool_call_id is not None
                else None
            )

    def claim_tool(self, tool_call: ToolCall) -> ToolCall:
        with self._lock:
            current = self.get_tool(tool_call.idempotency_key)
            if current is not None:
                if current.status != ExecutionStatus.SUCCEEDED:
                    now = utc_now()
                    if (
                        current.status == ExecutionStatus.RUNNING
                        and current.lease_expires_at is not None
                        and current.lease_expires_at > now
                    ):
                        return current
                    current.attempt_count += 1
                    current.status = ExecutionStatus.RUNNING
                    current.error = None
                    current.started_at = now
                    current.updated_at = now
                    current.lease_owner = tool_call.lease_owner
                    current.lease_expires_at = tool_call.lease_expires_at
                    current.fencing_token += 1
                    self._tools[current.tool_call_id] = current.model_copy(deep=True)
                return current
            self._tools[tool_call.tool_call_id] = tool_call.model_copy(deep=True)
            self._tool_keys[tool_call.idempotency_key] = tool_call.tool_call_id
            return tool_call.model_copy(deep=True)

    def complete_tool(
        self,
        tool_call_id: str,
        result_json: dict,
        *,
        error: str | None = None,
        expected_fencing_token: int | None = None,
    ) -> ToolCall:
        return self._finish_tool(
            tool_call_id,
            ExecutionStatus.SUCCEEDED,
            result_json,
            error,
            expected_fencing_token=expected_fencing_token,
        )

    def fail_tool(
        self, tool_call_id: str, error: str, *, expected_fencing_token: int | None = None
    ) -> ToolCall:
        return self._finish_tool(
            tool_call_id,
            ExecutionStatus.FAILED,
            None,
            error,
            expected_fencing_token=expected_fencing_token,
        )

    def _finish_tool(
        self,
        tool_call_id: str,
        status: ExecutionStatus,
        result_json: dict | None,
        error: str | None,
        *,
        expected_fencing_token: int | None = None,
    ) -> ToolCall:
        with self._lock:
            item = self._tools[tool_call_id].model_copy(deep=True)
            if (
                expected_fencing_token is not None
                and item.fencing_token != expected_fencing_token
            ):
                raise PermissionError("Stale tool worker fencing token")
            item.status = status
            item.result_json = result_json
            item.error = error
            item.completed_at = utc_now()
            item.updated_at = item.completed_at
            self._tools[tool_call_id] = item.model_copy(deep=True)
            return item

    def list_tools(self, run_id: str, *, tenant_id: str) -> list[ToolCall]:
        with self._lock:
            return [
                item.model_copy(deep=True)
                for item in self._tools.values()
                if item.run_id == run_id and item.tenant_id == tenant_id
            ]


class SqlAlchemyExecutionRepository:
    def __init__(self, database_url: str | None = None, *, engine: Engine | None = None) -> None:
        if engine is None and database_url is None:
            raise ValueError("database_url or engine is required")
        self.engine = engine or create_engine(database_url, pool_pre_ping=True)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def _session(self) -> Iterator[Session]:
        with self.session_factory() as session, session.begin():
            yield session

    def get_agent(self, idempotency_key: str) -> AgentExecution | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(AgentExecutionRecord).where(
                    AgentExecutionRecord.idempotency_key == idempotency_key
                )
            )
            return self._agent(row) if row is not None else None

    def claim_agent(self, execution: AgentExecution) -> AgentExecution:
        try:
            with self._session() as session:
                session.add(AgentExecutionRecord(**execution.model_dump()))
            return execution
        except IntegrityError:
            current = self.get_agent(execution.idempotency_key)
            if current is None:
                raise
            if current.status != ExecutionStatus.SUCCEEDED:
                with self._session() as session:
                    row = session.get(AgentExecutionRecord, current.execution_id)
                    assert row is not None
                    row.attempt_count += 1
                    row.status = ExecutionStatus.RUNNING.value
                    row.error = None
                    row.started_at = utc_now()
                    row.updated_at = row.started_at
                current = self.get_agent(execution.idempotency_key)
                assert current is not None
            return current

    def complete_agent(
        self, execution_id: str, result_json: dict, *, error: str | None = None
    ) -> AgentExecution:
        return self._finish_agent(execution_id, ExecutionStatus.SUCCEEDED, result_json, error)

    def fail_agent(self, execution_id: str, error: str) -> AgentExecution:
        return self._finish_agent(execution_id, ExecutionStatus.FAILED, None, error)

    def _finish_agent(
        self,
        execution_id: str,
        status: ExecutionStatus,
        result_json: dict | None,
        error: str | None,
    ) -> AgentExecution:
        with self._session() as session:
            row = session.get(AgentExecutionRecord, execution_id)
            if row is None:
                raise KeyError(execution_id)
            row.status = status.value
            row.result_json = result_json
            row.error = error
            row.completed_at = utc_now()
            row.updated_at = row.completed_at
        with self.session_factory() as session:
            restored = session.get(AgentExecutionRecord, execution_id)
            assert restored is not None
            return self._agent(restored)

    def list_agents(self, run_id: str, *, tenant_id: str) -> list[AgentExecution]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(AgentExecutionRecord)
                .where(
                    AgentExecutionRecord.run_id == run_id,
                    AgentExecutionRecord.tenant_id == tenant_id,
                )
                .order_by(AgentExecutionRecord.started_at)
            ).all()
            return [self._agent(row) for row in rows]

    def get_tool(self, idempotency_key: str) -> ToolCall | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(ToolCallRecord).where(ToolCallRecord.idempotency_key == idempotency_key)
            )
            return self._tool(row) if row is not None else None

    def claim_tool(self, tool_call: ToolCall) -> ToolCall:
        try:
            with self._session() as session:
                session.add(ToolCallRecord(**tool_call.model_dump()))
            return tool_call
        except IntegrityError:
            current = self.get_tool(tool_call.idempotency_key)
            if current is None:
                raise
            if current.status != ExecutionStatus.SUCCEEDED:
                with self._session() as session:
                    row = session.scalar(
                        select(ToolCallRecord)
                        .where(ToolCallRecord.tool_call_id == current.tool_call_id)
                        .with_for_update()
                    )
                    assert row is not None
                    now = utc_now()
                    lease_expires_at = row.lease_expires_at
                    if (
                        row.status == ExecutionStatus.RUNNING.value
                        and lease_expires_at is not None
                        and lease_expires_at > now
                    ):
                        return self._tool(row)
                    row.attempt_count += 1
                    row.status = ExecutionStatus.RUNNING.value
                    row.error = None
                    row.started_at = now
                    row.updated_at = row.started_at
                    row.lease_owner = tool_call.lease_owner
                    row.lease_expires_at = tool_call.lease_expires_at
                    row.fencing_token += 1
                current = self.get_tool(tool_call.idempotency_key)
                assert current is not None
            return current

    def complete_tool(
        self,
        tool_call_id: str,
        result_json: dict,
        *,
        error: str | None = None,
        expected_fencing_token: int | None = None,
    ) -> ToolCall:
        return self._finish_tool(
            tool_call_id,
            ExecutionStatus.SUCCEEDED,
            result_json,
            error,
            expected_fencing_token=expected_fencing_token,
        )

    def fail_tool(
        self, tool_call_id: str, error: str, *, expected_fencing_token: int | None = None
    ) -> ToolCall:
        return self._finish_tool(
            tool_call_id,
            ExecutionStatus.FAILED,
            None,
            error,
            expected_fencing_token=expected_fencing_token,
        )

    def _finish_tool(
        self,
        tool_call_id: str,
        status: ExecutionStatus,
        result_json: dict | None,
        error: str | None,
        *,
        expected_fencing_token: int | None = None,
    ) -> ToolCall:
        with self._session() as session:
            row = session.get(ToolCallRecord, tool_call_id)
            if row is None:
                raise KeyError(tool_call_id)
            if (
                expected_fencing_token is not None
                and row.fencing_token != expected_fencing_token
            ):
                raise PermissionError("Stale tool worker fencing token")
            row.status = status.value
            row.result_json = result_json
            row.error = error
            row.completed_at = utc_now()
            row.updated_at = row.completed_at
        with self.session_factory() as session:
            restored = session.get(ToolCallRecord, tool_call_id)
            assert restored is not None
            return self._tool(restored)

    def list_tools(self, run_id: str, *, tenant_id: str) -> list[ToolCall]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(ToolCallRecord)
                .where(
                    ToolCallRecord.run_id == run_id,
                    ToolCallRecord.tenant_id == tenant_id,
                )
                .order_by(ToolCallRecord.started_at)
            ).all()
            return [self._tool(row) for row in rows]

    @staticmethod
    def _agent(row: AgentExecutionRecord) -> AgentExecution:
        return AgentExecution.model_validate(
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
        )

    @staticmethod
    def _tool(row: ToolCallRecord) -> ToolCall:
        return ToolCall.model_validate(
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
        )
