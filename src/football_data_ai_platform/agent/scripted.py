"""`ScriptedLLMClient`: a test double of the `LLMClient` port that replays scripted answers,
so tests and CI run without tokens (ADR 24)."""

from dataclasses import dataclass, field

from pydantic import BaseModel

from football_data_ai_platform.agent.llm import LLMResult, Usage


@dataclass(frozen=True)
class Call:
    system: str
    prompt: str
    output: type[BaseModel]


@dataclass
class ScriptedLLMClient:
    """Answers each call with the next scripted answer; an exception is raised instead."""

    answers: list[BaseModel | Exception]
    model: str = "scripted"
    calls: list[Call] = field(default_factory=list[Call])

    async def parse[T: BaseModel](
        self, *, system: str, prompt: str, output: type[T], max_tokens: int = 2048
    ) -> LLMResult[T]:
        self.calls.append(Call(system, prompt, output))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if not isinstance(answer, output):
            raise TypeError(f"scripted {type(answer).__name__}, asked for {output.__name__}")
        return LLMResult(answer, self.model, Usage(0, 0))
