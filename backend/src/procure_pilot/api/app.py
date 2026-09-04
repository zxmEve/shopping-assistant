from __future__ import annotations

import json
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from procure_pilot.agents.runtime import (
    DeterministicAgentPlanner,
    OpenAIAgentsPlanner,
    topology_description,
)
from procure_pilot.config import Settings, get_settings
from procure_pilot.domain.models import ProcurementRun, RunResponse
from procure_pilot.fixtures.catalog import FixtureCatalogAdapter
from procure_pilot.fixtures.enterprise import FixtureEnterpriseGateway
from procure_pilot.harness.runtime import AgentHarness, RunNotFoundError, RunRepository
from procure_pilot.services.engine import ProcurementEngine


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
    planner = DeterministicAgentPlanner()
    if settings.agent_mode == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("PROCURE_OPENAI_API_KEY is required when PROCURE_AGENT_MODE=openai")
        planner = OpenAIAgentsPlanner(settings.openai_model, settings.openai_api_key)
    return ProcurementEngine(
        catalog=FixtureCatalogAdapter(),
        enterprise=FixtureEnterpriseGateway(),
        repository=RunRepository(settings.run_store_path),
        harness=AgentHarness(
            secret=settings.action_token_secret,
            max_steps=settings.max_agent_steps,
            max_agent_turns=settings.max_agent_turns,
            max_tool_calls=settings.max_tool_calls,
            max_provider_calls=settings.max_provider_calls,
            max_retries=settings.max_retries,
            max_input_tokens=settings.max_input_tokens,
            max_output_tokens=settings.max_output_tokens,
            max_cost_micro_usd=settings.max_cost_micro_usd,
            max_wall_time_ms=settings.max_wall_time_ms,
            max_parallel_provider_calls=settings.max_parallel_provider_calls,
        ),
        planner=planner,
    )


def create_app(
    settings: Settings | None = None, engine: ProcurementEngine | None = None
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

    @app.exception_handler(RunNotFoundError)
    async def not_found_handler(_, exc: RunNotFoundError):
        return _json_error(404, "RUN_NOT_FOUND", str(exc))

    @app.exception_handler(PermissionError)
    async def permission_handler(_, exc: PermissionError):
        return _json_error(403, "FORBIDDEN", str(exc))

    @app.exception_handler(ValueError)
    async def validation_handler(_, exc: ValueError):
        return _json_error(409, "INVALID_STATE", str(exc))

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": settings.agent_mode, "environment": settings.environment}

    @app.get("/api/agent/topology")
    async def agent_topology() -> dict:
        return topology_description(settings.agent_mode, settings.openai_model)

    @app.get("/api/agent/tools")
    async def agent_tools(
        context: Annotated[RequestContext, Depends(request_context)],
    ) -> dict:
        employee = active_engine.enterprise.identity(context.tenant_id, context.user_id)
        return {
            "registry_version": active_engine.harness.tool_registry.version,
            "tools": active_engine.harness.available_tools(employee.roles),
        }

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
            return {"status": "not_run", "total": 26, "passed": 0, "veto_failures": []}

    return app


def _json_error(status_code: int, code: str, message: str):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )


app = create_app()


def run() -> None:
    uvicorn.run("procure_pilot.api.app:app", host="0.0.0.0", port=8000, reload=False)
