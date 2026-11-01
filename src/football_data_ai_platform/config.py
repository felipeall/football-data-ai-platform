"""Settings read from the environment and `.env` (never from code)."""

from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from football_data_ai_platform.sources.interface import SourcePolicy
from football_data_ai_platform.sources.sofascore import SofascorePolicy


class ReepSettings(BaseModel):
    """The pinned release of the Reep register (ADR 32), e.g. REEP__RELEASE=2026-10-31.1."""

    release: str | None = None


class ModelRole(BaseModel):
    """The Provider and model a Model Role runs on (ADR 24), e.g.
    MODELS__IDENTITY_JUDGE__MODEL. Model IDs live in configuration only, never in code."""

    provider: str = "anthropic"
    model: str | None = None
    effort: str = "low"


class ModelRoles(BaseModel):
    identity_judge: ModelRole = ModelRole()


class Settings(BaseSettings):
    """Connection settings. Defaults suit the host side of the compose stack."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_nested_delimiter="__")

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "football_data_ai_platform"
    postgres_user: str = "football_data_ai_platform"
    postgres_password: str = ""

    temporal_host: str = "localhost"
    temporal_port: int = 7233
    temporal_namespace: str = "default"
    temporal_task_queue: str = "ingestion"

    # One policy per source, e.g. TRANSFERMARKT__REQUESTS_PER_SECOND=0.5.
    transfermarkt: SourcePolicy = SourcePolicy()
    sofascore: SofascorePolicy = SofascorePolicy()
    understat: SourcePolicy = SourcePolicy()
    reep: ReepSettings = ReepSettings()
    # The Season the PoC covers, by its start year (ADR 35): 2025 is 25/26.
    covered_season: int = 2025

    models: ModelRoles = ModelRoles()
    anthropic_api_key: SecretStr | None = None
    # LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_BASE_URL, the Langfuse SDK's names.
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_base_url: str = "http://localhost:3000"

    @property
    def database_url(self) -> str:
        """libpq connection URL for the application database."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def temporal_address(self) -> str:
        """host:port of the Temporal frontend."""
        return f"{self.temporal_host}:{self.temporal_port}"
