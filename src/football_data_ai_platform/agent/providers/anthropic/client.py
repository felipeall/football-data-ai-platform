"""`ClaudeClient`: the `LLMClient` port on the Anthropic API (ADR 24, ADR 25)."""

import anthropic
from pydantic import BaseModel

from football_data_ai_platform.agent.llm import LLMRefusal, LLMResult, Usage


class ClaudeClient:
    """One structured call through `messages.parse`. Only the first-party `anthropic`
    backend exists so far (ADR 25). The SDK retries rate limits and server errors itself;
    what it still raises is left to the caller's retries (a Temporal activity's)."""

    def __init__(self, model: str, api_key: str | None = None, effort: str = "low") -> None:
        self._model = model
        self._effort = effort
        # None lets the SDK find credentials itself (ANTHROPIC_API_KEY, an `ant` profile).
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    @property
    def model(self) -> str:
        return self._model

    async def parse[T: BaseModel](
        self, *, system: str, prompt: str, output: type[T], max_tokens: int = 2048
    ) -> LLMResult[T]:
        response = await self._client.messages.parse(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": self._effort},  # pyright: ignore[reportArgumentType]
            output_format=output,
        )
        if response.stop_reason == "refusal":
            details = response.stop_details
            raise LLMRefusal(f"{self._model} refused: {details}")
        if response.parsed_output is None:
            raise LLMRefusal(f"{self._model} gave no {output.__name__} ({response.stop_reason})")
        return LLMResult(
            response.parsed_output,
            response.model,
            Usage(response.usage.input_tokens, response.usage.output_tokens),
        )
