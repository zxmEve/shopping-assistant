from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "ProcurePilot"
    environment: str = "fixture"
    agent_mode: str = "deterministic"
    openai_model: str = "gpt-5-mini"
    openai_api_key: str | None = None
    action_token_secret: str = "procure-pilot-local-demo-secret"
    run_store_path: Path = Path(".data/procure-pilot-runs.json")
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    max_agent_steps: int = 20
    max_agent_turns: int = 12
    max_tool_calls: int = 12
    max_provider_calls: int = 12
    max_retries: int = 4
    max_input_tokens: int = 30_000
    max_output_tokens: int = 10_000
    max_cost_micro_usd: int = 100_000
    max_wall_time_ms: int = 25_000
    max_parallel_provider_calls: int = 4

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PROCURE_",
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
