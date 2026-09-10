from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_ROOT.parent


class Settings(BaseSettings):
    app_name: str = "ProcurePilot"
    environment: str = "fixture"
    repository_backend: str = "json"
    database_url: str | None = None
    agent_mode: str = "deterministic"
    agent_topology: str = "two_agent"
    model_provider: str = "openai"
    openai_model: str = "gpt-5-mini"
    orchestrator_model: str | None = None
    specialist_model: str | None = None
    verifier_model: str | None = None
    requirement_model: str | None = None
    sourcing_model: str | None = None
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    knowledge_embedding_provider: str = "hash"
    knowledge_embedding_model: str = "text-embedding-3-small"
    knowledge_embedding_base_url: str | None = None
    knowledge_embedding_api_key: str | None = None
    knowledge_embedding_batch_size: int = 64
    knowledge_embedding_timeout_seconds: float = 30.0
    knowledge_ocr_provider: str = "none"
    knowledge_ocr_languages: str = "chi_sim+eng"
    knowledge_ocr_timeout_seconds: float = 30.0
    knowledge_max_file_bytes: int = 10 * 1024 * 1024
    knowledge_max_pdf_pages: int = 200
    knowledge_max_docx_uncompressed_bytes: int = 50 * 1024 * 1024
    action_token_secret: str = "procure-pilot-local-demo-secret"
    run_store_path: Path = Path(".data/procure-pilot-runs.json")
    context_policy_path: Path = BACKEND_ROOT / "config/context_policy.yaml"
    memory_registry_path: Path = BACKEND_ROOT / "config/memory_registry.yaml"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    max_agent_steps: int = 20
    max_agent_turns: int = 12
    max_orchestrator_requests: int = 2
    max_specialist_requests: int = 8
    max_stage_model_requests: int = 10
    max_tool_calls: int = 21
    max_agent_read_tool_calls: int = 12
    max_workflow_read_tool_calls: int = 6
    max_commit_tool_calls: int = 3
    max_provider_calls: int = 12
    max_retries: int = 4
    max_input_tokens: int = 30_000
    max_output_tokens: int = 10_000
    max_cost_micro_usd: int = 100_000
    max_wall_time_ms: int = 25_000
    max_parallel_provider_calls: int = 4

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="PROCURE_",
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def resolved_agent_models(self) -> dict[str, str]:
        return {
            "requirement": self.requirement_model or self.specialist_model or self.openai_model,
            "sourcing": self.sourcing_model or self.specialist_model or self.openai_model,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
