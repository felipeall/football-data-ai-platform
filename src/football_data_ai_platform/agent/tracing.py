"""Langfuse tracing for any `LLMClient` (ADR 18), so every Provider is traced the same way."""

from dataclasses import replace
from typing import Any

from langfuse import Langfuse
from pydantic import BaseModel

from football_data_ai_platform.agent.llm import LLMClient, LLMResult


class TracedLLMClient:
    """Records each call as a Langfuse generation named after its Model Role. Langfuse
    exports in the background: a Langfuse outage loses the trace, never the call."""

    def __init__(
        self,
        inner: LLMClient,
        langfuse: Langfuse,
        role: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._inner = inner
        self._langfuse = langfuse
        self._role = role
        self._metadata = metadata or {}

    @property
    def model(self) -> str:
        return self._inner.model

    async def parse[T: BaseModel](
        self, *, system: str, prompt: str, output: type[T], max_tokens: int = 2048
    ) -> LLMResult[T]:
        with self._langfuse.start_as_current_observation(
            name=self._role,
            as_type="generation",
            model=self._inner.model,
            input=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            metadata={"model_role": self._role, **self._metadata},
            model_parameters={"max_tokens": max_tokens},
        ) as generation:
            try:
                result = await self._inner.parse(
                    system=system, prompt=prompt, output=output, max_tokens=max_tokens
                )
            except Exception as error:
                generation.update(level="ERROR", status_message=str(error))
                raise
            generation.update(
                output=result.output.model_dump(mode="json"),
                model=result.model,
                usage_details={
                    "input": result.usage.input_tokens,
                    "output": result.usage.output_tokens,
                },
            )
            trace_id = self._langfuse.get_current_trace_id()
        return replace(result, trace_id=trace_id)
