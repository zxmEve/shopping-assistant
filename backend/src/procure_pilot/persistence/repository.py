from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from procure_pilot.domain.models import ProcurementRun, utc_now
from procure_pilot.harness.runtime import PersistenceConflictError, RunNotFoundError
from procure_pilot.persistence.models import (
    Base,
    ProcurementRunRecord,
    RunMessageRecord,
    RunTraceEventRecord,
)


class SqlAlchemyRunRepository:
    """Tenant-scoped run store with atomic snapshots and optimistic concurrency."""

    def __init__(self, database_url: str, *, engine: Engine | None = None) -> None:
        self.engine = engine or create_engine(database_url, pool_pre_ping=True)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)

    def create_schema(self) -> None:
        """Test/development helper; production uses Alembic migrations."""
        Base.metadata.create_all(self.engine)

    @contextmanager
    def _session(self) -> Iterator[Session]:
        with self.session_factory() as session, session.begin():
            yield session

    def save(self, run: ProcurementRun) -> ProcurementRun:
        run.updated_at = utc_now()
        document = run.model_dump(mode="json")
        values = {
            "tenant_id": run.tenant_id,
            "user_id": run.user_id,
            "state": run.state.value,
            "state_version": run.state_version,
            "document": document,
            "draft_id": run.draft.draft_id if run.draft else None,
            "draft_idempotency_key": run.draft.idempotency_key if run.draft else None,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
        }
        try:
            with self._session() as session:
                if run._persistence_version is None:
                    session.add(
                        ProcurementRunRecord(
                            run_id=run.run_id,
                            lock_version=0,
                            **values,
                        )
                    )
                    next_version = 0
                else:
                    next_version = run._persistence_version + 1
                    result = session.execute(
                        update(ProcurementRunRecord)
                        .where(
                            ProcurementRunRecord.run_id == run.run_id,
                            ProcurementRunRecord.tenant_id == run.tenant_id,
                            ProcurementRunRecord.lock_version == run._persistence_version,
                        )
                        .values(**values, lock_version=next_version)
                    )
                    if result.rowcount != 1:
                        raise PersistenceConflictError(
                            f"Run was changed by another writer: {run.run_id}"
                        )
                self._replace_messages(session, run)
                self._replace_trace(session, run)
        except IntegrityError as exc:
            raise PersistenceConflictError(
                f"Run or draft uniqueness constraint failed: {run.run_id}"
            ) from exc
        run._persistence_version = next_version
        return run

    def get(
        self,
        run_id: str,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> ProcurementRun:
        statement = select(
            ProcurementRunRecord.document,
            ProcurementRunRecord.lock_version,
        ).where(ProcurementRunRecord.run_id == run_id)
        if tenant_id is not None:
            statement = statement.where(ProcurementRunRecord.tenant_id == tenant_id)
        if user_id is not None:
            statement = statement.where(ProcurementRunRecord.user_id == user_id)
        with self.session_factory() as session:
            row = session.execute(statement).one_or_none()
        if row is None:
            if tenant_id is not None or user_id is not None:
                raise PermissionError("Run is not accessible in the current tenant context")
            raise RunNotFoundError(run_id)
        run = ProcurementRun.model_validate(row.document)
        run._persistence_version = row.lock_version
        return run

    def list(
        self,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> list[ProcurementRun]:
        statement = select(
            ProcurementRunRecord.document,
            ProcurementRunRecord.lock_version,
        ).order_by(ProcurementRunRecord.created_at)
        if tenant_id is not None:
            statement = statement.where(ProcurementRunRecord.tenant_id == tenant_id)
        if user_id is not None:
            statement = statement.where(ProcurementRunRecord.user_id == user_id)
        with self.session_factory() as session:
            rows = session.execute(statement).all()
        runs = []
        for row in rows:
            run = ProcurementRun.model_validate(row.document)
            run._persistence_version = row.lock_version
            runs.append(run)
        return runs

    @staticmethod
    def _replace_messages(session: Session, run: ProcurementRun) -> None:
        session.execute(delete(RunMessageRecord).where(RunMessageRecord.run_id == run.run_id))
        session.add_all(
            RunMessageRecord(
                run_id=run.run_id,
                tenant_id=run.tenant_id,
                position=position,
                role=message.role,
                content=message.content,
                created_at=message.created_at,
            )
            for position, message in enumerate(run.messages)
        )

    @staticmethod
    def _replace_trace(session: Session, run: ProcurementRun) -> None:
        session.execute(delete(RunTraceEventRecord).where(RunTraceEventRecord.run_id == run.run_id))
        session.add_all(
            RunTraceEventRecord(
                event_id=event.event_id,
                run_id=run.run_id,
                tenant_id=run.tenant_id,
                position=position,
                timestamp=event.timestamp,
                actor=event.actor,
                kind=event.kind,
                name=event.name,
                status=event.status,
                detail=event.detail,
                duration_ms=event.duration_ms,
            )
            for position, event in enumerate(run.trace)
        )
