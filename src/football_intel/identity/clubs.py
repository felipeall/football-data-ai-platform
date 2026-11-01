"""Clubs: created from the Reep Release, with slug IDs from Transfermarkt names (ADR 34)."""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from football_intel.identity.names import to_ascii

# Whole tokens that name a club's legal form, not the club.
_DROPPED_TOKENS = frozenset({"fc", "afc", "cf"})
SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")

LinkKey = tuple[str, str]  # (provider, external_id)


def slug(name: str) -> str:
    """A Club ID from a club's name, by ADR 34's fixed rule: ASCII and lower case, `&` as
    `and`, apostrophes and dots dropped, the whole tokens `fc`, `afc` and `cf` dropped, and
    every other run of characters one `-`. Empty when nothing is left."""
    text = to_ascii(name.replace("&", " and ")).lower()
    text = re.sub(r"['.]", "", text)
    return "-".join(t for t in re.findall(r"[a-z0-9]+", text) if t not in _DROPPED_TOKENS)


@dataclass(frozen=True)
class Team:
    """A team in the stored Matches of a Competition we cover, as one source keys it."""

    provider: str
    external_id: str
    name: str


@dataclass(frozen=True)
class TransfermarktClub:
    club_id: str
    name: str
    display_name: str
    country: str | None


@dataclass(frozen=True)
class NewClub:
    club_id: str
    name: str
    display_name: str
    country: str | None


@dataclass(frozen=True)
class UnresolvedTeam:
    """A team that gets no Club in this run, and why. A person decides, or a later Reep
    release or Transfermarkt refresh covers it."""

    provider: str
    external_id: str
    name: str
    reason: str


@dataclass
class ClubPlan:
    clubs: list[NewClub] = field(default_factory=list[NewClub])
    links: dict[LinkKey, str] = field(default_factory=dict[LinkKey, str])  # new links, to a Club ID
    unresolved: list[UnresolvedTeam] = field(default_factory=list[UnresolvedTeam])


def plan_clubs(
    teams: Iterable[Team],
    reep_teams: Mapping[LinkKey, Iterable[str]],
    reep_vereins: Mapping[str, Iterable[str]],
    transfermarkt: Mapping[str, TransfermarktClub],
    links: Mapping[LinkKey, str],
    taken: Iterable[str],
) -> ClubPlan:
    """The Clubs to create and the links to add, so every team links to a Club.

    - `reep_teams`: the Reep teams of each (provider, team ID) the pinned release bridges.
    - `reep_vereins`: each Reep team's Transfermarkt `verein` IDs.
    - `transfermarkt`: the staged Transfermarkt clubs, by `verein` ID.
    - `links`: the Club of each link already stored; `taken`: every Club ID and alias.

    A team links to the Club of its Reep team, found through any link the Reep team or its
    bridged teams already have. Otherwise the Reep team creates the Club, named after its
    Transfermarkt club. Reep teams are taken in ID order, so a fresh build always gives the
    same slugs. A link already stored is never moved: a conflict leaves the team unresolved.
    """
    plan = ClubPlan()
    known = dict(links)
    used = set(taken)
    by_reep: dict[str, list[Team]] = {}
    for team in sorted(set(teams), key=lambda t: (t.provider, t.external_id)):
        reep_ids = sorted(set(reep_teams.get((team.provider, team.external_id), ())))
        if not reep_ids:
            plan.unresolved.append(_unresolved(team, "no Reep team in the pinned release"))
        elif len(reep_ids) > 1:
            reason = f"bridged to several Reep teams: {', '.join(reep_ids)}"
            plan.unresolved.append(_unresolved(team, reason))
        else:
            by_reep.setdefault(reep_ids[0], []).append(team)

    for reep_id in sorted(by_reep):
        members = by_reep[reep_id]
        vereins = sorted(set(reep_vereins.get(reep_id, ())))
        keys = [
            ("reep", reep_id),
            *((t.provider, t.external_id) for t in members),
            *(("transfermarkt", v) for v in vereins),
        ]
        clubs = sorted({known[k] for k in keys if k in known})
        if len(clubs) > 1:
            reason = f"Reep team {reep_id} is linked to several Clubs: {', '.join(clubs)}"
            plan.unresolved.extend(_unresolved(t, reason) for t in members)
            continue
        if clubs:
            club_id = clubs[0]
        else:
            new = _new_club(reep_id, vereins, transfermarkt, used)
            if isinstance(new, str):
                plan.unresolved.extend(_unresolved(t, new) for t in members)
                continue
            plan.clubs.append(new)
            used.add(new.club_id)
            club_id = new.club_id
        for key in keys:
            if key not in known:
                known[key] = plan.links[key] = club_id
    return plan


def _new_club(
    reep_id: str,
    vereins: list[str],
    transfermarkt: Mapping[str, TransfermarktClub],
    used: set[str],
) -> NewClub | str:
    """The Club a Reep team creates, or why it creates none."""
    named = [transfermarkt[v] for v in vereins if v in transfermarkt]
    if not named:
        # Reep's own names are not loaded yet (#80).
        return f"Reep team {reep_id} has no staged Transfermarkt club to name it"
    if len(named) > 1:
        ids = ", ".join(c.club_id for c in named)
        return f"Reep team {reep_id} bridges several Transfermarkt clubs: {ids}"
    (club,) = named
    base = slug(club.name)
    if not base:
        return f"Transfermarkt club {club.club_id} ({club.name!r}) gives an empty slug"
    choices = [base]
    if club.country and slug(club.country):
        choices.append(f"{base}-{slug(club.country)}")
    for choice in choices:
        if choice not in used:
            return NewClub(choice, club.name, club.display_name, club.country)
    return f"slug {' and '.join(choices)} already taken: a person names this Club"


def _unresolved(team: Team, reason: str) -> UnresolvedTeam:
    return UnresolvedTeam(team.provider, team.external_id, team.name, reason)
