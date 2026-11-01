"""The `LLMClient` port: one structured call to a model (ADR 24).

Core code talks to this port only. A Provider implements it under
`agent/providers/<provider>/`, and configuration picks the Provider and model per Model Role.
"""

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    output: T
    model: str  # the model that answered, as the Provider names it
    usage: Usage
    trace_id: str | None = None  # the Langfuse trace, when the call was traced


class LLMRefusal(Exception):
    """The model declined to answer, or its answer did not fit the output type."""


class LLMClient(Protocol):
    """A model bound to one Model Role: `model` is its configured model ID."""

    @property
    def model(self) -> str: ...

    async def parse[T: BaseModel](
        self, *, system: str, prompt: str, output: type[T], max_tokens: int = 2048
    ) -> LLMResult[T]:
        """Ask once, and return the answer validated as `output`. Raises `LLMRefusal` when
        there is no valid answer; transport errors propagate for the caller to retry."""
        ...
