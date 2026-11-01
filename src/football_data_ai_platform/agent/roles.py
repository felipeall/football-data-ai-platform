"""Model Roles: configuration picks each role's Provider and model (ADR 24)."""

from collections.abc import Callable

from langfuse import Langfuse

from football_data_ai_platform.agent.llm import LLMClient
from football_data_ai_platform.agent.providers.anthropic import ClaudeClient
from football_data_ai_platform.agent.tracing import TracedLLMClient
from football_data_ai_platform.config import ModelRole, Settings

IDENTITY_JUDGE = "identity_judge"


class ModelRoleNotConfigured(Exception):
    """A Model Role has no model ID, or names a Provider that is not registered."""


def _anthropic(role: ModelRole, model: str, settings: Settings) -> LLMClient:
    key = settings.anthropic_api_key
    return ClaudeClient(model, key.get_secret_value() if key else None, role.effort)


# Providers register by name; a new one is done when it passes the conformance suite.
PROVIDERS: dict[str, Callable[[ModelRole, str, Settings], LLMClient]] = {
    "anthropic": _anthropic,
}


def llm_client(name: str, settings: Settings) -> LLMClient:
    """The `LLMClient` of Model Role `name`, traced in Langfuse when its keys are set."""
    role: ModelRole = getattr(settings.models, name)
    if role.model is None:
        variable = f"MODELS__{name.upper()}__MODEL"
        raise ModelRoleNotConfigured(f"Set {variable} to the model the {name} role runs on")
    if role.provider not in PROVIDERS:
        raise ModelRoleNotConfigured(
            f"Model Role {name}: no Provider {role.provider!r} (known: {', '.join(PROVIDERS)})"
        )
    client = PROVIDERS[role.provider](role, role.model, settings)
    if settings.langfuse_public_key and settings.langfuse_secret_key:
        langfuse = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key.get_secret_value(),
            base_url=settings.langfuse_base_url,
        )
        return TracedLLMClient(client, langfuse, name)
    return client
