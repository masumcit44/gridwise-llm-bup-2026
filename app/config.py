"""Application configuration.

All config values come from environment variables (or an optional `.env` file).
`.env.example` documents the variable names with empty placeholder values.
No real secrets are ever committed to the repository.
"""

from __future__ import annotations

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "GridWise LLM BUP 2026"
    environment: str = "development"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    # Language-model configuration. Used by the LLM interpretation task.
    # An empty value means "not configured" — never store a real API key here.
    llm_provider: str = ""
    llm_model: str = ""
    llm_api_key: str = ""

    # Runtime provider chain: primary with one-time failover to secondary.
    llm_primary_provider: str = "groq"
    llm_secondary_provider: str = "gemini"
    groq_api_key: str = ""
    groq_model: str = ""
    gemini_api_key: str = ""
    gemini_model: str = ""
    llm_timeout_seconds: float = 6.0
    llm_repair_attempts: int = 1

    # Performance targets per the Participant Guide.
    request_timeout_seconds: float = 30.0
    health_ready_timeout_seconds: float = 60.0


settings = Settings()