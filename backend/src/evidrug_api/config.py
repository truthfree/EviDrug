from functools import lru_cache
from typing import Literal, cast

from fastapi import Request
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from EVIDRUG-prefixed environment variables."""

    app_name: str = "EviDrug API"
    environment: Literal["local", "test", "staging", "production"] = "local"
    api_v1_prefix: str = "/api/v1"
    database_url: str = "postgresql+psycopg://evidrug:evidrug@localhost:5432/evidrug"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    access_code_hash: SecretStr | None = None
    session_secret: SecretStr | None = None
    session_cookie_name: str = "evidrug_session"
    session_ttl_seconds: int = Field(default=8 * 60 * 60, ge=300, le=7 * 24 * 60 * 60)
    visitor_cookie_name: str = "evidrug_visitor"
    visitor_ttl_seconds: int = Field(
        default=90 * 24 * 60 * 60, ge=24 * 60 * 60, le=365 * 24 * 60 * 60
    )
    auth_attempt_limit: int = Field(default=5, ge=1, le=100)
    auth_attempt_window_seconds: int = Field(default=5 * 60, ge=1, le=60 * 60)
    api_rate_window_seconds: int = Field(default=60, ge=1, le=3600)
    api_total_limit: int = Field(default=120, ge=1, le=10000)
    api_login_limit: int = Field(default=20, ge=1, le=1000)
    api_search_limit: int = Field(default=30, ge=1, le=1000)
    api_validation_limit: int = Field(default=10, ge=1, le=1000)
    api_analysis_limit: int = Field(default=5, ge=1, le=1000)
    api_max_concurrent_requests: int = Field(default=4, ge=1, le=32)
    api_max_body_bytes: int = Field(default=32768, ge=1024, le=1048576)
    api_body_timeout_seconds: float = Field(default=10, gt=0, le=60)
    orchestration_lease_seconds: int = Field(default=1200, ge=60, le=7200)
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://dacon-apim-hackathon-0903.azure-api.net/hackathon/openai/v1"
    openai_model: Literal["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"] = "gpt-5.6-sol"
    openai_timeout_seconds: float = Field(default=60, gt=0, le=300)
    openai_max_retries: int = Field(default=2, ge=0, le=5)
    open_targets_graphql_url: str = "https://api.platform.opentargets.org/api/v4/graphql"
    open_targets_timeout_seconds: float = Field(default=15, gt=0, le=60)
    disease_candidate_limit: int = Field(default=5, ge=1, le=20)
    uniprot_base_url: str = "https://rest.uniprot.org"
    pharos_graphql_url: str = "https://pharos-api.ncats.io/graphql"
    pharos_enabled: bool = True
    target_lookup_timeout_seconds: float = Field(default=20, gt=0, le=60)
    target_candidate_limit: int = Field(default=5, ge=1, le=20)
    target_shortlist_limit: int = Field(default=3, ge=1, le=5)
    poc_models_enabled: bool = False
    ctoxpred2_recall_enabled: bool = False
    dta_assay_providers_enabled: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="EVIDRUG_",
        extra="ignore",
    )

    @field_validator("database_url", mode="before")
    @classmethod
    def select_async_postgres_driver(cls, value: object) -> object:
        """Render의 표준 PostgreSQL URL을 설치된 psycopg async dialect로 고정한다."""

        if not isinstance(value, str):
            return value
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return f"postgresql+psycopg://{value.removeprefix(prefix)}"
        return value

    @property
    def use_secure_cookies(self) -> bool:
        """운영과 스테이징에서만 HTTPS 전용 쿠키를 사용한다."""

        return self.environment in {"staging", "production"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_runtime_settings(request: Request) -> Settings:
    """현재 FastAPI 애플리케이션에 고정된 설정을 반환한다."""

    return cast(Settings, request.app.state.settings)
