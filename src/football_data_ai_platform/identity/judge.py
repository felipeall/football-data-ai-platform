"""The LLM judge of the ambiguous band (ADR 6): what it is shown, and what its answer
decides. Pure: the database and the model are reached elsewhere.

A pending Identity Candidate is a proposed link from a Source Record to a Player that the
rules could not settle. The judge sees the Source Record, the records already linked to the
Player, and the evidence that proposed the link, and answers accept, reject or defer. Only a
confident accept or reject decides; anything else waits for human review.
"""

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

# The judge decides a Candidate only at this confidence or above; below, a person reviews it.
JUDGE_DECIDES_FROM = 0.9

LLM_JUDGE = "llm_judge"  # the method of a Candidate the judge has answered


@dataclass(frozen=True)
class RecordFacts:
    """What we hold on one Source Record."""

    source: str
    source_record_id: int
    name: str | None  # None when staging no longer has the record
    birth_date: date | None = None
    matches: int | None = None  # Sofascore and Understat only
    minutes: int | None = None


@dataclass(frozen=True)
class JudgeCase:
    candidate_id: int
    player_id: int
    method: str  # the method that proposed the link
    confidence: float  # the proposing method's score
    record: RecordFacts
    # The Source Records already linked to the Player, one per source at most.
    player_records: list[RecordFacts]
    evidence: dict[str, Any] = field(default_factory=dict[str, Any])


class Verdict(BaseModel):
    """The judge's answer for one Candidate."""

    decision: Literal["accept", "reject", "defer"] = Field(
        description="accept: the same footballer; reject: different footballers; "
        "defer: the facts do not settle it"
    )
    confidence: float = Field(ge=0, le=1, description="how sure the decision is, 0 to 1")
    reasoning: str = Field(description="one or two sentences naming the facts that decided it")


def status_after(verdict: Verdict) -> Literal["accepted", "rejected", "pending"]:
    """The Candidate's status after the verdict: decided only when the judge is sure."""
    if verdict.confidence < JUDGE_DECIDES_FROM or verdict.decision == "defer":
        return "pending"
    return "accepted" if verdict.decision == "accept" else "rejected"


SYSTEM = """\
You judge whether two football data records describe the same footballer.

A platform links each provider's player records (Sofascore, Transfermarkt, Understat) to one
canonical Player. Rules settle most links. You see the ones they could not settle: usually the
same birth date or the same Matches, under a name that differs (a nickname, a shortened or
full first name, a transliteration, a second surname).

Weigh the facts:
- Birth dates that agree, or differ by a day, are strong evidence; a larger gap is strong
  evidence against.
- Understat has no birth dates. There, co-appearance (the share of the Understat player's
  Matches in which the candidate played for the same Club) and the minutes difference per
  Match carry the weight: 1.0 and a few minutes mean the same appearances.
- A name can be a known short form or variant of the other (Dan for Daniel, Max for
  Maximilian, Fer for Fernando, Charly for Carlos). Two different players with the same birth
  date and similar names at the same club are rare but possible.
- `runner_up` names another candidate who scored close.

Answer accept, reject or defer, with your confidence from 0 to 1. Defer when the facts do not
settle it; a person reviews what you defer and what you are less than 0.9 sure of.\
"""


def prompt(case: JudgeCase) -> str:
    """The case as the judge reads it: plain JSON of the facts, nothing else."""
    return (
        "Is this Source Record the same footballer as the Player its records describe?\n\n"
        + json.dumps(
            {
                "source_record": _facts(case.record),
                "player_records": [_facts(r) for r in case.player_records],
                "proposed_by": case.method,
                "proposal_score": case.confidence,
                "evidence": case.evidence,
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )


def _facts(record: RecordFacts) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "source": record.source,
        "id": record.source_record_id,
        "name": record.name,
    }
    if record.birth_date is not None:
        facts["birth_date"] = record.birth_date.isoformat()
    if record.matches is not None:
        facts["matches"] = record.matches
        facts["minutes"] = record.minutes
    return facts
