"""Model Roles pick a Provider and model from configuration (ADR 24)."""

import pytest
from pydantic import SecretStr

from football_data_ai_platform.agent.providers.anthropic import ClaudeClient
from football_data_ai_platform.agent.roles import (
    IDENTITY_JUDGE,
    ModelRoleNotConfigured,
    llm_client,
)
from football_data_ai_platform.agent.tracing import TracedLLMClient
from football_data_ai_platform.config import ModelRole, ModelRoles, Settings


def _settings(
    role: ModelRole, public_key: str | None = None, secret_key: str | None = None
) -> Settings:
    """Settings that ignore `.env`, whose Langfuse keys would turn tracing on."""
    return Settings.model_construct(
        models=ModelRoles(identity_judge=role),
        anthropic_api_key=SecretStr("test"),
        langfuse_public_key=public_key,
        langfuse_secret_key=SecretStr(secret_key) if secret_key else None,
        langfuse_base_url="http://localhost:3000",
    )


def test_a_role_without_a_model_id_is_not_configured() -> None:
    with pytest.raises(ModelRoleNotConfigured, match="MODELS__IDENTITY_JUDGE__MODEL"):
        llm_client(IDENTITY_JUDGE, _settings(ModelRole()))


def test_a_role_with_an_unknown_provider_is_not_configured() -> None:
    with pytest.raises(ModelRoleNotConfigured, match="no Provider 'nobody'"):
        llm_client(IDENTITY_JUDGE, _settings(ModelRole(provider="nobody", model="m")))


def test_the_model_id_comes_from_configuration() -> None:
    client = llm_client(IDENTITY_JUDGE, _settings(ModelRole(model="configured-model")))

    assert isinstance(client, ClaudeClient)
    assert client.model == "configured-model"


def test_langfuse_keys_turn_tracing_on() -> None:
    settings = _settings(ModelRole(model="configured-model"), "pk-test", "sk-test")

    client = llm_client(IDENTITY_JUDGE, settings)

    assert isinstance(client, TracedLLMClient)
    assert client.model == "configured-model"
