from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from procure_pilot.agents.multi_agent import (
    FixtureTwoAgentAdapter,
    TwoAgentPlanner,
)
from procure_pilot.agents.provider_adapter import OpenAICompatibleTwoAgentAdapter
from procure_pilot.agents.runtime import (
    DeterministicAgentPlanner,
    topology_description,
)
from procure_pilot.config import Settings, get_settings
from procure_pilot.domain.memory import (
    LongTermMemory,
    MemoryAuditEvent,
    MemoryConsent,
    MemoryScope,
)
from procure_pilot.domain.models import ProcurementRun, RunResponse
from procure_pilot.fixtures.catalog import FixtureCatalogAdapter
from procure_pilot.fixtures.enterprise import FixtureEnterpriseGateway
from procure_pilot.harness.runtime import (
    AgentHarness,
    PersistenceConflictError,
    RunNotFoundError,
    RunRepository,
)
from procure_pilot.persistence import SqlAlchemyMemoryRepository, SqlAlchemyRunRepository
from procure_pilot.services.context import ContextPolicy
from procure_pilot.services.engine import ProcurementEngine
from procure_pilot.services.memory import MemoryService
from procure_pilot.services.memory_registry import MemoryRegistry


class StartRunRequest(BaseModel):
    message: str = Field(min_length=2, max_length=2000)
    scenario: str | None = None


class ContinueRunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    scenario: str | None = None


class ConfirmRunRequest(BaseModel):
    action_token: str


class RequestContext(BaseModel):
    tenant_id: str
    user_id: str


class CreateMemoryRequest(BaseModel):
    scope: MemoryScope
    key: str = Field(min_length=1, max_length=128)
    category_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=8000)
    value_json: dict
    tags: list[str] = Field(default_factory=list, max_length=20)
    effective_at: datetime | None = None
    expires_at: datetime | None = None
    request_id: str = Field(min_length=1, max_length=128)
    consent_displayed_text: str = Field(min_length=1, max_length=2000)
    consent_acknowledged: Literal[True]


class UpdateMemoryRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    value_json: dict
    tags: list[str] = Field(default_factory=list, max_length=20)
    effective_at: datetime | None = None
    expires_at: datetime | None = None
    expected_version: int = Field(ge=0)


FIXTURE_SESSIONS = {
    "fixture-session-cn-employee-001": RequestContext(tenant_id="demo_cn", user_id="employee_001"),
    "fixture-session-id-employee-101": RequestContext(tenant_id="demo_id", user_id="employee_101"),
}


def request_context(
    authorization: Annotated[str | None, Header()] = None,
) -> RequestContext:
    if not authorization or not authorization.startswith("Bearer "):
        raise PermissionError("A valid fixture session is required")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        return FIXTURE_SESSIONS[token].model_copy(deep=True)
    except KeyError as exc:
        raise PermissionError("A valid fixture session is required") from exc


@lru_cache
def get_engine() -> ProcurementEngine:
    settings = get_settings()
    memory_registry = MemoryRegistry.from_yaml(settings.memory_registry_path)
    planner = DeterministicAgentPlanner()
    if settings.agent_topology in {"two_agent", "explicit_multi_agent", "direct_tools"}:
        if settings.agent_mode == "deterministic":
            planner = TwoAgentPlanner(FixtureTwoAgentAdapter())
        elif settings.agent_mode == "openai":
            if not settings.openai_api_key:
                raise RuntimeError(
                    "PROCURE_OPENAI_API_KEY is required when PROCURE_AGENT_MODE=openai"
                )
            models = settings.resolved_agent_models
            planner = TwoAgentPlanner(
                OpenAICompatibleTwoAgentAdapter(
                    api_key=settings.openai_api_key,
                    provider=settings.model_provider,
                    base_url=settings.openai_base_url,
                    requirement_model=models["requirement"],
                    sourcing_model=models["sourcing"],
                )
            )
        else:
            raise RuntimeError(f"Unsupported PROCURE_AGENT_MODE: {settings.agent_mode}")
    else:
        raise RuntimeError(
            "Unsupported PROCURE_AGENT_TOPOLOGY. Use two_agent "
            "(explicit_multi_agent and direct_tools are accepted as migration aliases)."
        )
    memory_repository = None
    if settings.repository_backend == "postgres":
        if not settings.database_url:
            raise RuntimeError(
                "PROCURE_DATABASE_URL is required when PROCURE_REPOSITORY_BACKEND=postgres"
            )
        repository = SqlAlchemyRunRepository(settings.database_url)
        memory_repository = SqlAlchemyMemoryRepository(settings.database_url)
    elif settings.repository_backend == "json":
        repository = RunRepository(settings.run_store_path)
    else:
        raise RuntimeError(f"Unsupported PROCURE_REPOSITORY_BACKEND: {settings.repository_backend}")
    return ProcurementEngine(
        catalog=FixtureCatalogAdapter(),
        enterprise=FixtureEnterpriseGateway(),
        repository=repository,
        harness=AgentHarness(
            secret=settings.action_token_secret,
            max_steps=settings.max_agent_steps,
            max_agent_turns=settings.max_agent_turns,
            max_orchestrator_requests=settings.max_orchestrator_requests,
            max_specialist_requests=settings.max_specialist_requests,
            max_stage_model_requests=settings.max_stage_model_requests,
            max_tool_calls=settings.max_tool_calls,
            max_agent_read_tool_calls=settings.max_agent_read_tool_calls,
            max_workflow_read_tool_calls=settings.max_workflow_read_tool_calls,
            max_commit_tool_calls=settings.max_commit_tool_calls,
            max_provider_calls=settings.max_provider_calls,
            max_retries=settings.max_retries,
            max_input_tokens=settings.max_input_tokens,
            max_output_tokens=settings.max_output_tokens,
            max_cost_micro_usd=settings.max_cost_micro_usd,
            max_wall_time_ms=settings.max_wall_time_ms,
            max_parallel_provider_calls=settings.max_parallel_provider_calls,
        ),
        planner=planner,
        memory_repository=memory_repository,
        context_policy=ContextPolicy.from_yaml(settings.context_policy_path),
        memory_registry=memory_registry,
    )


def create_app(
    settings: Settings | None = None,
    engine: ProcurementEngine | None = None,
    memory_repository: SqlAlchemyMemoryRepository | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="ProcurePilot API",
        version="0.1.0",
        description="Evidence-grounded enterprise procurement agent demo",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    active_engine = engine or get_engine()
    active_memory_registry = active_engine.memory_registry or MemoryRegistry.from_yaml(
        settings.memory_registry_path
    )
    active_memory_repository = memory_repository or active_engine.memory_repository
    if active_memory_repository is None and settings.repository_backend == "postgres":
        if not settings.database_url:
            raise RuntimeError(
                "PROCURE_DATABASE_URL is required when PROCURE_REPOSITORY_BACKEND=postgres"
            )
        active_memory_repository = SqlAlchemyMemoryRepository(settings.database_url)
    if active_memory_repository is not active_engine.memory_repository:
        active_engine.configure_memory_context(
            active_memory_repository,
            memory_registry=active_memory_registry,
        )
    active_memory_service = (
        MemoryService(active_memory_repository, active_memory_registry)
        if active_memory_repository is not None
        else None
    )

    def memories() -> SqlAlchemyMemoryRepository:
        if active_memory_repository is None:
            raise HTTPException(
                status_code=503,
                detail="Long-term memory requires the PostgreSQL repository backend",
            )
        return active_memory_repository

    def memory_service() -> MemoryService:
        if active_memory_service is None:
            raise HTTPException(
                status_code=503,
                detail="Long-term memory requires the PostgreSQL repository backend",
            )
        return active_memory_service

    @app.exception_handler(RunNotFoundError)
    async def not_found_handler(_, exc: RunNotFoundError):
        return _json_error(404, "RUN_NOT_FOUND", str(exc))

    @app.exception_handler(PermissionError)
    async def permission_handler(_, exc: PermissionError):
        return _json_error(403, "FORBIDDEN", str(exc))

    @app.exception_handler(ValueError)
    async def validation_handler(_, exc: ValueError):
        return _json_error(409, "INVALID_STATE", str(exc))

    @app.exception_handler(PersistenceConflictError)
    async def persistence_conflict_handler(_, exc: PersistenceConflictError):
        return _json_error(409, "VERSION_CONFLICT", str(exc))

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "mode": settings.agent_mode,
            "topology": settings.agent_topology,
            "environment": settings.environment,
        }

    @app.get("/api/agent/topology")
    async def agent_topology() -> dict:
        return topology_description(
            settings.agent_mode,
            settings.openai_model,
            settings.model_provider,
            settings.agent_topology,
            models=settings.resolved_agent_models,
        )

    @app.get("/api/agent/tools")
    async def agent_tools(
        context: Annotated[RequestContext, Depends(request_context)],
    ) -> dict:
        employee = active_engine.enterprise.identity(context.tenant_id, context.user_id)
        return {
            "registry_version": active_engine.harness.tool_registry.version,
            "tools": active_engine.harness.available_tools(employee.roles),
        }

    @app.get("/api/memory-definitions")
    async def memory_definitions(
        context: Annotated[RequestContext, Depends(request_context)],
    ) -> dict:
        active_engine.enterprise.identity(context.tenant_id, context.user_id)
        return {
            "registry_version": active_memory_registry.version,
            "definitions": [
                item.model_dump(mode="json")
                for item in active_memory_registry.list_definitions()
            ],
        }

    @app.get("/api/categories")
    async def categories(
        context: Annotated[RequestContext, Depends(request_context)],
        query: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    ) -> dict:
        active_engine.enterprise.identity(context.tenant_id, context.user_id)
        resolved = (
            [active_memory_registry.resolve_category(query)]
            if query is not None
            else active_memory_registry.list_categories()
        )
        return {
            "registry_version": active_memory_registry.version,
            "categories": [
                item.model_dump(mode="json") for item in resolved
            ],
        }

    @app.get("/api/memory-tags")
    async def memory_tags(
        context: Annotated[RequestContext, Depends(request_context)],
    ) -> dict:
        active_engine.enterprise.identity(context.tenant_id, context.user_id)
        return {
            "registry_version": active_memory_registry.version,
            "tags": [item.model_dump(mode="json") for item in active_memory_registry.list_tags()],
        }

    @app.post("/api/memories", response_model=LongTermMemory)
    async def create_memory(
        request: CreateMemoryRequest,
        context: Annotated[RequestContext, Depends(request_context)],
    ) -> LongTermMemory:
        employee = active_engine.enterprise.identity(context.tenant_id, context.user_id)
        return memory_service().create(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            team_id=employee.department,
            roles=employee.roles,
            scope=request.scope,
            key=request.key,
            category_id=request.category_id,
            content=request.content,
            value_json=request.value_json,
            tags=request.tags,
            effective_at=request.effective_at,
            expires_at=request.expires_at,
            request_id=request.request_id,
            displayed_text=request.consent_displayed_text,
        )

    @app.get("/api/memories", response_model=list[LongTermMemory])
    async def list_memories(
        context: Annotated[RequestContext, Depends(request_context)],
        scope: Annotated[MemoryScope | None, Query()] = None,
    ) -> list[LongTermMemory]:
        employee = active_engine.enterprise.identity(context.tenant_id, context.user_id)
        return memories().list(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            team_id=employee.department,
            scope=scope,
        )

    @app.patch("/api/memories/{memory_id}", response_model=LongTermMemory)
    async def update_memory(
        memory_id: str,
        request: UpdateMemoryRequest,
        context: Annotated[RequestContext, Depends(request_context)],
    ) -> LongTermMemory:
        employee = active_engine.enterprise.identity(context.tenant_id, context.user_id)
        return memory_service().update(
            memory_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            team_id=employee.department,
            roles=employee.roles,
            content=request.content,
            value_json=request.value_json,
            tags=request.tags,
            effective_at=request.effective_at,
            expires_at=request.expires_at,
            expected_version=request.expected_version,
        )

    @app.delete("/api/memories/{memory_id}")
    async def delete_memory(
        memory_id: str,
        context: Annotated[RequestContext, Depends(request_context)],
        expected_version: Annotated[int, Query(ge=0)],
    ) -> dict[str, str]:
        employee = active_engine.enterprise.identity(context.tenant_id, context.user_id)
        memory_service().delete(
            memory_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            team_id=employee.department,
            roles=employee.roles,
            expected_version=expected_version,
        )
        return {"status": "deleted"}

    @app.get("/api/memories/{memory_id}/audit", response_model=list[MemoryAuditEvent])
    async def memory_audit(
        memory_id: str,
        context: Annotated[RequestContext, Depends(request_context)],
    ) -> list[MemoryAuditEvent]:
        return memories().audit(
            memory_id,
            tenant_id=context.tenant_id,
            owner_user_id=context.user_id,
        )

    @app.get("/api/memories/{memory_id}/consent", response_model=MemoryConsent)
    async def memory_consent(
        memory_id: str,
        context: Annotated[RequestContext, Depends(request_context)],
    ) -> MemoryConsent:
        return memories().consent_for_memory(
            memory_id,
            tenant_id=context.tenant_id,
            owner_user_id=context.user_id,
        )

    @app.post("/api/runs", response_model=RunResponse)
    async def start_run(
        request: StartRunRequest,
        context: Annotated[RequestContext, Depends(request_context)],
    ) -> RunResponse:
        return await active_engine.start(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            message=request.message,
            scenario=request.scenario,
        )

    @app.post("/api/runs/{run_id}/messages", response_model=RunResponse)
    async def continue_run(
        run_id: str,
        request: ContinueRunRequest,
        context: Annotated[RequestContext, Depends(request_context)],
    ) -> RunResponse:
        return await active_engine.continue_run(
            run_id=run_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            message=request.message,
            scenario=request.scenario,
        )

    @app.post("/api/runs/{run_id}/confirm", response_model=RunResponse)
    async def confirm_run(
        run_id: str,
        request: ConfirmRunRequest,
        context: Annotated[RequestContext, Depends(request_context)],
    ) -> RunResponse:
        return await active_engine.confirm(
            run_id=run_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            action_token=request.action_token,
        )

    @app.get("/api/runs/{run_id}", response_model=ProcurementRun)
    async def get_run(
        run_id: str,
        context: Annotated[RequestContext, Depends(request_context)],
    ) -> ProcurementRun:
        return active_engine.get(run_id, tenant_id=context.tenant_id, user_id=context.user_id)

    @app.get("/api/runs/{run_id}/events")
    async def run_events(
        run_id: str,
        context: Annotated[RequestContext, Depends(request_context)],
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        run = active_engine.get(run_id, tenant_id=context.tenant_id, user_id=context.user_id)

        async def stream() -> AsyncIterator[str]:
            for index, event in enumerate(run.trace[after:], start=after):
                yield f"id: {index}\nevent: trace\ndata: {event.model_dump_json()}\n\n"
            yield f"event: state\ndata: {json.dumps({'state': run.state})}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/evals/summary")
    async def eval_summary() -> dict:
        try:
            from procure_pilot.evals.runner import load_latest_report

            return load_latest_report()
        except FileNotFoundError:
            return {"status": "not_run", "total": 36, "passed": 0, "veto_failures": []}

    return app


def _json_error(status_code: int, code: str, message: str):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )


app = create_app()


def run() -> None:
    uvicorn.run("procure_pilot.api.app:app", host="0.0.0.0", port=8000, reload=False)
