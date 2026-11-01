"""The rules that create Players and link Source Records to them (ADR 34). Pure functions:
the database is read and written elsewhere.

Reep, then Transfermarkt, create Players, each only after a creation check finds no existing
Player with the same name and birth date (ADR 39). Sofascore and Understat only link to them:
Sofascore by birth date and name against a Player's Transfermarkt record, Understat by
co-appearance with a Player's Sofascore Appearances. Proposals at or above `ACCEPT_FROM`
become links; those from `JUDGE_FROM` are the ambiguous band left to the LLM judge; lower
scores are not proposed at all.
"""

from collections import defaultdict
from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
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
    # A record Reep or Transfermarkt would create a Player for, linked to an existing one
    # instead (ADR 39). Rejecting one reopens the record, to create its Player.
    CREATION_CHECK = "creation_check"


# The methods that create a Player when its Source Record has none yet.
CREATING_METHODS = frozenset({Method.REEP_BRIDGE, Method.TRANSFERMARKT_PLAYER})

RecordKey = tuple[str, int]  # (source, source_record_id)
# The sources with birth dates, each matched by name and birth date against the other.
OTHER_SOURCE = {SOFASCORE: TRANSFERMARKT, TRANSFERMARKT: SOFASCORE}
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
    records: Mapping[RecordKey, SourcePlayer],
    sofascore: Sequence[Appearance],
    understat: Sequence[Appearance],
    rejected: Mapping[RecordKey, set[int]] | None = None,
    waiting: set[RecordKey] | None = None,
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
    - Before a person creates a Player, its Sofascore and Transfermarkt records run the
      creation check (`creation_checks`; `records` gives their names and birth dates, and
      `rejected` the Players already rejected for them). The first that matches an existing
      Player links to it, and the person's other records follow it there. One that only may
      match waits for the judge, and the whole person waits with it (`waiting`: records with
      a pending creation check), so none of its records creates the Player meanwhile.
    """
    waiting = waiting or set()
    persons = _persons(bridges, held)
    existing = {source: players_without(source, links, records) for source in OTHER_SOURCE}
    checks = {
        (source, record): proposal
        for source in OTHER_SOURCE
        for record, proposal in creation_checks(
            source,
            [
                player
                for (of, _), player in sorted(records.items())
                if of == source and (of, player.source_record_id) not in proposed
            ],
            existing[source],
            rejected,
        ).items()
    }
    sofascore_of = {
        player: record for (source, record), player in links.items() if source == SOFASCORE
    }
    played = _played(sofascore), _played(understat)
    claimed: set[Target] = set()  # existing Players a creation check linked a person to
    proposals: list[Proposal] = []
    for reep_id, members in persons.items():
        keys = [(m.source, m.source_record_id) for m in members]
        players = {links[key] for key in keys if key in links}
        if len(players) > 1:
            continue
        target: Target
        checked: RecordKey | None = None
        if players:
            target = players.pop()
        elif any(key in waiting for key in keys):
            continue
        elif check := next((checks[key] for key in keys if key in checks), None):
            proposals.append(check)
            if not check.accepted or check.player in claimed:
                # The store makes a second claim on one Player pending, too.
                continue
            target = check.player
            checked = (check.source, check.source_record_id)
            claimed.add(check.player)
        else:
            target = f"reep:{reep_id}"
        own = [m.source_record_id for m in members if m.source == SOFASCORE]
        if own:
            anchor: int | None = own[0]
        else:
            anchor = sofascore_of.get(target) if isinstance(target, int) else None
        for member in members:
            key = (member.source, member.source_record_id)
            if key in proposed or key == checked:
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
                    disagreement=_disagreement(member, anchor, records, *played),
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
    records: Mapping[RecordKey, SourcePlayer],
    sofascore: Mapping[int, set[tuple[str, str]]],
    understat: Mapping[int, set[tuple[str, str]]],
) -> str | None:
    """Why our data contradicts Reep linking `member` to the Player of `sofascore_record`;
    None when it doesn't, or when there is no Sofascore record to check against."""
    if sofascore_record is None or member.source == SOFASCORE:
        return None
    if member.source == TRANSFERMARKT:
        born = _born(records, (TRANSFERMARKT, member.source_record_id))
        anchor_born = _born(records, (SOFASCORE, sofascore_record))
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


def _born(records: Mapping[RecordKey, SourcePlayer], key: RecordKey) -> date | None:
    record = records.get(key)
    return record.birth_date if record is not None else None


def transfermarkt_players(
    transfermarkt: Iterable[SourcePlayer],
    links: Mapping[RecordKey, int],
    records: Mapping[RecordKey, SourcePlayer],
    rejected: Mapping[RecordKey, set[int]] | None = None,
) -> list[Proposal]:
    """Each Transfermarkt player creates a Player (ADR 34), unless the creation check links
    it to an existing one or leaves it to the judge (`creation_checks`, ADR 39). Called
    after Reep, for the records Reep did not link: the store skips any record that already
    has a Candidate."""
    checks = creation_checks(
        TRANSFERMARKT, transfermarkt, players_without(TRANSFERMARKT, links, records), rejected
    )
    return [
        checks.get(player.source_record_id)
        or Proposal(
            TRANSFERMARKT,
            player.source_record_id,
            f"transfermarkt:{player.source_record_id}",
            Method.TRANSFERMARKT_PLAYER,
            1.0,
            {"name": player.name},
        )
        for player in sorted(transfermarkt, key=lambda p: p.source_record_id)
    ]


def players_without(
    source: str, links: Mapping[RecordKey, int], records: Mapping[RecordKey, SourcePlayer]
) -> dict[int, SourcePlayer]:
    """The Players a `source` record may belong to by name and birth date: each one holding
    a record of the other source (`OTHER_SOURCE`) and none of `source`'s, with that record.
    `links` holds accepted links only; `records` the held records, by key."""
    other = OTHER_SOURCE[source]
    holding = {player for (of, _), player in links.items() if of == source}
    return {
        player: records[key]
        for key, player in links.items()
        if key[0] == other and player not in holding and key in records
    }


def creation_checks(
    source: str,
    players: Iterable[SourcePlayer],
    existing: Mapping[int, SourcePlayer],
    rejected: Mapping[RecordKey, set[int]] | None = None,
) -> dict[int, Proposal]:
    """The creation check (ADR 39): before Reep or Transfermarkt creates a Player for a
    `source` record, the name and birth date rule of `sofascore_proposals` runs against
    `existing` (`players_without`), less the Players already rejected for the record.

    Returns, by record, a Proposal to the existing Player: accepted for the same name,
    pending for the judge when the name is only similar or a second same-named Player is
    inside the window. A record left out creates its own Player.
    """
    rejected = rejected or {}
    index = _by_birth_date(existing)
    checks: dict[int, Proposal] = {}
    for player in players:
        match = _match(
            player, index, OTHER_SOURCE[source], rejected.get((source, player.source_record_id))
        )
        if match is None:
            continue
        player_id, sure, score, evidence = match
        confidence = 1.0 if sure else min(score, ACCEPT_FROM - 0.01)
        checks[player.source_record_id] = Proposal(
            source, player.source_record_id, player_id, Method.CREATION_CHECK, confidence, evidence
        )
    return checks


def sofascore_proposals(
    sofascore: Iterable[SourcePlayer], transfermarkt: Mapping[int, SourcePlayer]
) -> list[Proposal]:
    """Link a Sofascore player to the Player whose Transfermarkt record (`transfermarkt`, by
    Player ID) was born within `BIRTH_DATE_TOLERANCE_DAYS` of it. The same name: a link. A
    similar name (a nickname, a transliteration), or a second same-named record inside the
    window: left to the judge. Birth date only: no proposal."""
    index = _by_birth_date(transfermarkt)
    proposals: list[Proposal] = []
    for player in sofascore:
        match = _match(player, index, TRANSFERMARKT)
        if match is None:
            continue
        player_id, sure, score, evidence = match
        if sure:
            method, confidence = Method.NAME_BIRTH_DATE, 1.0
        else:
            # Below ACCEPT_FROM whatever the name score, so the judge decides.
            method, confidence = Method.FUZZY, min(score, ACCEPT_FROM - 0.01)
        proposals.append(
            Proposal(SOFASCORE, player.source_record_id, player_id, method, confidence, evidence)
        )
    return proposals


_BirthDates = dict[date, list[tuple[int, SourcePlayer]]]


def _by_birth_date(records: Mapping[int, SourcePlayer]) -> _BirthDates:
    """Each Player's record (`records`, by Player ID), by its birth date."""
    index: _BirthDates = defaultdict(list)
    for player_id, record in records.items():
        if record.birth_date is not None:
            index[record.birth_date].append((player_id, record))
    return index


def _match(
    player: SourcePlayer,
    index: _BirthDates,
    other_source: str,
    skip: Container[int] | None = None,
) -> tuple[int, bool, float, dict[str, Any]] | None:
    """The name and birth date rule: the Player whose record in `index` was born within
    `BIRTH_DATE_TOLERANCE_DAYS` of `player` with the most similar name, unless it is in
    `skip`. Returns its ID, whether the name decides (the same name, and no second
    same-named record in the window), the name score, and the evidence; None when no name
    in the window reaches `JUDGE_FROM`."""
    if player.birth_date is None:
        return None
    window = range(-BIRTH_DATE_TOLERANCE_DAYS, BIRTH_DATE_TOLERANCE_DAYS + 1)
    scored = sorted(
        (
            (similarity(player.name, record.name), abs(gap), player_id, record)
            for gap in window
            for player_id, record in index.get(player.birth_date + timedelta(gap), [])
            if skip is None or player_id not in skip
        ),
        key=lambda scored: (-scored[0], scored[1], scored[2]),
    )
    if not scored or scored[0][0] < JUDGE_FROM:
        return None
    name_score, gap, player_id, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    evidence = {
        "name": player.name,
        "candidate": best.name,
        f"{other_source}_record_id": best.source_record_id,
        "name_similarity": round(name_score, 3),
        "birth_date_gap_days": gap,
    }
    sure = name_score >= SAME_NAME and runner_up < SAME_NAME
    return player_id, sure, name_score, evidence


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
