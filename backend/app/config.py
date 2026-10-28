"""All runtime settings, read from environment variables (and .env)."""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://sieve:sieve@localhost:5434/talentsieve"
    redis_url: str = "redis://localhost:6374/0"
    upload_dir: str = "/data/uploads"

    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 8 * 60
    bcrypt_rounds: int = 12

    # Upload limits (spec: up to 50 resumes per batch)
    max_files_per_batch: int = 50
    max_file_bytes: int = 5 * 1024 * 1024
    max_job_description_chars: int = 20_000

    # --- LLM --------------------------------------------------------------
    # auto: Anthropic when a key is set, otherwise MockLLM.
    llm_provider: Literal["auto", "mock", "anthropic"] = "auto"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    anthropic_fallbacks: bool = True
    llm_timeout_seconds: float = 60
    mock_llm_min_delay: float = 0.5
    mock_llm_max_delay: float = 2.0
    mock_llm_failure_rate: float = 0.1

    # --- Queue / workers ----------------------------------------------------
    queue_name: str = "talentsieve:queue"
    # Jobs one worker process runs at the same time (LLM calls are I/O bound).
    worker_concurrency: int = 4
    # A job running longer than this is assumed stuck and cancelled. It is
    # also roughly how long a job "owned" by a crashed worker waits before
    # another worker may pick it up (see docs/DECISIONS.md, crash safety).
    job_timeout_seconds: int = 120
    max_attempts: int = 5
    retry_base_delay_seconds: float = 2.0
    retry_max_delay_seconds: float = 60.0

    # --- Global LLM rate limit (all workers together) ---------------------
    llm_rate_limit_per_minute: int = 120
    # The window is configurable only so tests can use 1 s instead of 60 s.
    llm_rate_window_seconds: float = 60.0

    db_pool_size: int = 10
    db_max_overflow: int = 20


@lru_cache
def get_settings() -> Settings:
    return Settings()
