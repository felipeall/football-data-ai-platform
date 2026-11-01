"""The rules that create Players and link Source Records to them (ADR 34). Pure functions:
the database is read and written elsewhere.

Reep, then Transfermarkt, create Players. Sofascore and Understat only link to them:
Sofascore by birth date and name against a Player's Transfermarkt record, Understat by
co-appearance with a Player's Sofascore Appearances. Proposals at or above `ACCEPT_FROM`
become links; those from `JUDGE_FROM` are the ambiguous band left to the LLM judge; lower
scores are not proposed at all.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from football_data_ai_platform.identity.names import similarity

SOFASCORE = "sofascore"
TRANSFERMARKT = "transfermarkt"
UNDERSTAT = "understat"

ACCEPT_FROM = 0.9
JUDGE_FROM = 0.6
# Two names at least this similar are the same name, give or take spelling.
SAME_NAME = 0.9
# Understat: a candidate teammate must have played in this share of the player's Matches.
MIN_CO_APPEARANCE = 0.5
# Understat: the co-appearance rule also asks this share and this minutes agreement, since
# teammates co-appear too and names can be contained in one another.
RULE_CO_APPEARANCE = 0.9
RULE_MINUTES_DIFFERENCE = 5.0
# Sofascore and Understat count stoppage time differently; past this, minutes disagree.
MINUTES_SCALE = 30.0
# Fuzzy proposals: the runner-up must stay this far below the best to accept the best.
FUZZY_MARGIN = 0.1
# Sources disagree on a birth date by a day (El Hadji Malick Diouf); more is a different man.
BIRTH_DATE_TOLERANCE_DAYS = 1


class Method:
    REEP_BRIDGE = "reep_bridge"
    TRANSFERMARKT_PLAYER = "transfermarkt_player"
    NAME_BIRTH_DATE = "name_birth_date"
    CO_APPEARANCE = "co_appearance"
    FUZZY = "fuzzy"


# The methods that create a Player when its Source Record has none yet.
CREATING_METHODS = frozenset({Method.REEP_BRIDGE, Method.TRANSFERMARKT_PLAYER})

RecordKey = tuple[str, int]  # (source, source_record_id)
# A Proposal's Player: an existing Player's ID, or the key of a Player it creates, such as
# `reep:<reep_id>` or `transfermarkt:<id>`. Proposals sharing a key share the new Player.
Target = int | str


@dataclass(frozen=True)
class SourcePlayer:
    source_record_id: int
    name: str
    birth_date: date | None = None


@dataclass(frozen=True)
class Appearance:
    """A player's minutes in a canonical Match, for a canonical Club."""

    match_id: str
    club_id: str
    player_id: int
    minutes: int


@dataclass(frozen=True)
class Proposal:
    """A proposed link from a Source Record to a Player."""

    source: str
    source_record_id: int
    player: Target
    method: str
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict[str, Any])
    # Who decided an accepted Proposal; the rule's method when None.
    decided_by: str | None = None
    # Why our data contradicts the Proposal: it then waits for the judge, whatever its
    # confidence (ADR 32).
    disagreement: str | None = None

    @property
    def accepted(self) -> bool:
        return self.confidence >= ACCEPT_FROM and self.disagreement is None


@dataclass(frozen=True)
class ReepBridge:
    """A Reep release mapping one source's player to a Reep person."""

    reep_id: str
    source: str
    source_record_id: int
    rule_id: str | None = None
    route_scope: str | None = None


def reep_proposals(
    release: str,
    bridges: Iterable[ReepBridge],
    held: Mapping[str, set[int]],
    proposed: set[RecordKey],
    links: Mapping[RecordKey, int],
    birth_dates: Mapping[RecordKey, date | None],
    sofascore: Sequence[Appearance],
    understat: Sequence[Appearance],
) -> list[Proposal]:
    """Reep decides first (ADR 32, ADR 34): every Source Record we hold (`held`) that the
    release maps to a Reep person links to that person's Player. The person's first record
    we see creates the Player; later ones find it through the records already linked
    (`links`, accepted links only).

    - A record that has a Candidate (`proposed`) is skipped, a rejected one included, so
      rejecting a Reep link never makes Reep create a Player for it again.
    - A record bridged to several Reep persons is left to the rules. A person whose records
      already link to several Players is left alone, and listed by `reep_conflicts`.
    - A Transfermarkt or Understat link our data contradicts is pending, not accepted: its
      birth date is more than a day off the Player's Sofascore record, or it never played in
      the same Match for the same Club as that record.
    """
    persons = _persons(bridges, held)
    sofascore_of = {
        player: record for (source, record), player in links.items() if source == SOFASCORE
    }
    played = _played(sofascore), _played(understat)
    proposals: list[Proposal] = []
    for reep_id, members in persons.items():
        keys = [(m.source, m.source_record_id) for m in members]
        players = {links[key] for key in keys if key in links}
        if len(players) > 1:
            continue
        target: Target = players.pop() if players else f"reep:{reep_id}"
        own = [m.source_record_id for m in members if m.source == SOFASCORE]
        if own:
            anchor: int | None = own[0]
        else:
            anchor = sofascore_of.get(target) if isinstance(target, int) else None
        for member in members:
            if (member.source, member.source_record_id) in proposed:
                continue
            proposals.append(
                Proposal(
                    member.source,
                    member.source_record_id,
                    target,
                    Method.REEP_BRIDGE,
                    1.0,
                    {
                        "release": release,
                        "reep_id": reep_id,
                        "rule_id": member.rule_id,
                        "route_scope": member.route_scope,
                    },
                    decided_by=f"reep:{release}",
                    disagreement=_disagreement(member, anchor, birth_dates, *played),
                )
            )
    return proposals


@dataclass(frozen=True)
class ReepConflict:
    """A Reep person whose held records already link to several Players: Reep says they are
    one person, our links say they are several. Reep links none of its records; a person
    decides, by rejecting the wrong links."""

    reep_id: str
    player_ids: list[int]
    records: list[RecordKey]


def reep_conflicts(
    bridges: Iterable[ReepBridge], held: Mapping[str, set[int]], links: Mapping[RecordKey, int]
) -> list[ReepConflict]:
    """The Reep persons `reep_proposals` leaves alone because their records link to several
    Players. Only a later run can find one, since a fresh run has no links yet."""
    conflicts: list[ReepConflict] = []
    for reep_id, members in _persons(bridges, held).items():
        keys = [(m.source, m.source_record_id) for m in members]
        players = sorted({links[key] for key in keys if key in links})
        if len(players) > 1:
            conflicts.append(ReepConflict(reep_id, players, [k for k in keys if k in links]))
    return conflicts


def _persons(
    bridges: Iterable[ReepBridge], held: Mapping[str, set[int]]
) -> dict[str, list[ReepBridge]]:
    """Each Reep person's bridges to records we hold, in Reep ID order. A record bridged to
    several persons is left out: the rules decide it."""
    ours = sorted(
        (b for b in bridges if b.source_record_id in held.get(b.source, ())),
        key=lambda b: (b.reep_id, b.source, b.source_record_id),
    )
    persons_of: dict[RecordKey, set[str]] = defaultdict(set)
    for bridge in ours:
        persons_of[bridge.source, bridge.source_record_id].add(bridge.reep_id)
    persons: dict[str, list[ReepBridge]] = defaultdict(list)
    for bridge in ours:
        if len(persons_of[bridge.source, bridge.source_record_id]) == 1:
            persons[bridge.reep_id].append(bridge)
    return persons


def _played(appearances: Iterable[Appearance]) -> dict[int, set[tuple[str, str]]]:
    """The (Match, Club) pairs each player played."""
    played: dict[int, set[tuple[str, str]]] = defaultdict(set)
    for appearance in appearances:
        played[appearance.player_id].add((appearance.match_id, appearance.club_id))
    return played


def _disagreement(
    member: ReepBridge,
    sofascore_record: int | None,
    birth_dates: Mapping[RecordKey, date | None],
    sofascore: Mapping[int, set[tuple[str, str]]],
    understat: Mapping[int, set[tuple[str, str]]],
) -> str | None:
    """Why our data contradicts Reep linking `member` to the Player of `sofascore_record`;
    None when it doesn't, or when there is no Sofascore record to check against."""
    if sofascore_record is None or member.source == SOFASCORE:
        return None
    if member.source == TRANSFERMARKT:
        born = birth_dates.get((TRANSFERMARKT, member.source_record_id))
        anchor_born = birth_dates.get((SOFASCORE, sofascore_record))
        if born and anchor_born and abs((born - anchor_born).days) > BIRTH_DATE_TOLERANCE_DAYS:
            return f"born {born}, but Sofascore player {sofascore_record} was born {anchor_born}"
    if member.source == UNDERSTAT and not (
        understat.get(member.source_record_id, set()) & sofascore.get(sofascore_record, set())
    ):
        return (
            f"never played in the same Match for the same Club as Sofascore player "
            f"{sofascore_record}"
        )
    return None


def transfermarkt_players(transfermarkt: Iterable[SourcePlayer]) -> list[Proposal]:
    """Each Transfermarkt player creates a Player (ADR 34). Called after Reep, for the
    records Reep did not link: the store skips any record that already has a Candidate."""
    return [
        Proposal(
            TRANSFERMARKT,
            player.source_record_id,
            f"transfermarkt:{player.source_record_id}",
            Method.TRANSFERMARKT_PLAYER,
            1.0,
            {"name": player.name},
        )
        for player in sorted(transfermarkt, key=lambda p: p.source_record_id)
    ]


def sofascore_proposals(
    sofascore: Iterable[SourcePlayer], transfermarkt: Mapping[int, SourcePlayer]
) -> list[Proposal]:
    """Link a Sofascore player to the Player whose Transfermarkt record (`transfermarkt`, by
    Player ID) has the same birth date. The same name: a link. A similar name (a nickname, a
    transliteration): left to the judge. Same birth date only: no proposal."""
    by_birth_date: dict[date, list[tuple[int, SourcePlayer]]] = defaultdict(list)
    for player_id, record in transfermarkt.items():
        if record.birth_date is not None:
            by_birth_date[record.birth_date].append((player_id, record))
    proposals: list[Proposal] = []
    for player in sofascore:
        if player.birth_date is None:
            continue
        scored = sorted(
            (
                (similarity(player.name, record.name), player_id, record)
                for player_id, record in by_birth_date[player.birth_date]
            ),
            key=lambda scored: (-scored[0], scored[1]),
        )
        if not scored or scored[0][0] < JUDGE_FROM:
            continue
        name_score, player_id, best = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        evidence = {
            "name": player.name,
            "candidate": best.name,
            "transfermarkt_record_id": best.source_record_id,
            "name_similarity": round(name_score, 3),
        }
        if name_score >= SAME_NAME and runner_up < SAME_NAME:
            method, confidence = Method.NAME_BIRTH_DATE, 1.0
        else:
            # Below ACCEPT_FROM whatever the name score, so the judge decides.
            method, confidence = Method.FUZZY, min(name_score, ACCEPT_FROM - 0.01)
        proposals.append(
            Proposal(SOFASCORE, player.source_record_id, player_id, method, confidence, evidence)
        )
    return proposals


@dataclass(frozen=True)
class _Candidate:
    sofascore_player_id: int
    name_similarity: float
    co_appearance: float  # share of the Understat player's Matches it also played
    minutes_difference: float  # mean, over those Matches

    @property
    def score(self) -> float:
        minutes_agreement = 1 - min(self.minutes_difference, MINUTES_SCALE) / MINUTES_SCALE
        return 0.5 * self.name_similarity + 0.3 * self.co_appearance + 0.2 * minutes_agreement


def understat_proposals(
    understat: Sequence[Appearance],
    sofascore: Sequence[Appearance],
    understat_names: Mapping[int, str],
    sofascore_names: Mapping[int, str],
    players: Mapping[int, int],
) -> list[Proposal]:
    """Link an Understat player to the Player of the Sofascore teammate who played the same
    Matches for the same Club, chosen by name. `players` is each linked Sofascore player's
    Player.

    The co-appearance rule links outright when the names match and the minutes agree. Any
    other candidate is scored on name, co-appearance and minutes: a clear best links as
    fuzzy, a close or weaker one is left to the judge. Every Sofascore teammate is scored,
    linked or not, so a player whose own Sofascore record has no Player is left unlinked
    rather than proposed to the next-best teammate.
    """
    sofascore_minutes: dict[tuple[str, str], dict[int, int]] = defaultdict(dict)
    for appearance in sofascore:
        sofascore_minutes[(appearance.match_id, appearance.club_id)][appearance.player_id] = (
            appearance.minutes
        )
    by_player: dict[int, list[Appearance]] = defaultdict(list)
    for appearance in understat:
        by_player[appearance.player_id].append(appearance)

    proposals: list[Proposal] = []
    for player_id, appearances in sorted(by_player.items()):
        differences: dict[int, list[int]] = defaultdict(list)
        for appearance in appearances:
            for teammate, minutes in sofascore_minutes[
                (appearance.match_id, appearance.club_id)
            ].items():
                differences[teammate].append(abs(minutes - appearance.minutes))
        name = understat_names[player_id]
        candidates = sorted(
            (
                _Candidate(
                    teammate,
                    similarity(name, sofascore_names[teammate]),
                    len(diffs) / len(appearances),
                    sum(diffs) / len(diffs),
                )
                for teammate, diffs in differences.items()
                if len(diffs) / len(appearances) >= MIN_CO_APPEARANCE
            ),
            key=lambda candidate: (-candidate.score, candidate.sofascore_player_id),
        )
        if not candidates:
            continue
        best = candidates[0]
        if best.sofascore_player_id not in players:
            continue
        runner_up = candidates[1] if len(candidates) > 1 else None
        evidence = {
            "name": name,
            "candidate": sofascore_names[best.sofascore_player_id],
            "sofascore_record_id": best.sofascore_player_id,
            "name_similarity": round(best.name_similarity, 3),
            "co_appearance": round(best.co_appearance, 3),
            "minutes_difference": round(best.minutes_difference, 1),
            "matches": len(appearances),
        }
        same_name_teammates = sum(1 for c in candidates if c.name_similarity >= SAME_NAME)
        if (
            best.name_similarity >= SAME_NAME
            and same_name_teammates == 1
            and best.co_appearance >= RULE_CO_APPEARANCE
            and best.minutes_difference <= RULE_MINUTES_DIFFERENCE
        ):
            method, confidence = Method.CO_APPEARANCE, 1.0
        else:
            method, confidence = Method.FUZZY, round(best.score, 3)
            if runner_up is not None and best.score - runner_up.score < FUZZY_MARGIN:
                confidence = min(confidence, ACCEPT_FROM - 0.01)
                evidence["runner_up"] = sofascore_names[runner_up.sofascore_player_id]
            if confidence < JUDGE_FROM:
                continue
        proposals.append(
            Proposal(
                UNDERSTAT,
                player_id,
                players[best.sofascore_player_id],
                method,
                confidence,
                evidence,
            )
        )
    return proposals
