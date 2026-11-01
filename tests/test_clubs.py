from typing import Any

import pytest
from psycopg import AsyncConnection

from football_intel.identity.clubs import (
    ClubPlan,
    Team,
    TransfermarktClub,
    plan_clubs,
    slug,
)
from football_intel.identity.store import ClubStore, ClubTaken


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Arsenal FC", "arsenal"),
        ("Brighton & Hove Albion", "brighton-and-hove-albion"),
        ("West Ham United", "west-ham-united"),
        ("Wolverhampton Wanderers", "wolverhampton-wanderers"),
        ("Manchester United", "manchester-united"),
        ("AFC Bournemouth", "bournemouth"),
        ("Sunderland AFC", "sunderland"),
        ("Club Atlético de Madrid", "club-atletico-de-madrid"),
        ("1. FC Köln", "1-koln"),
        ("Newell's Old Boys", "newells-old-boys"),
        ("Newell\u2019s Old Boys", "newells-old-boys"),
        ("St. Pauli", "st-pauli"),
        ("Valencia CF", "valencia"),
        ("Fcbarcelona Affc", "fcbarcelona-affc"),  # only whole tokens are dropped
        ("  Hull  City -- ", "hull-city"),
    ],
)
def test_slug_follows_the_fixed_rule(name: str, expected: str) -> None:
    assert slug(name) == expected


# Made-up IDs (ADR 4): Reep teams r1… bridge Sofascore and Understat teams to vereins.
TEAMS = [
    Team("sofascore", "42", "Arsenal"),
    Team("understat", "83", "Arsenal"),
    Team("sofascore", "38", "Chelsea"),
    Team("understat", "80", "Chelsea"),
]
REEP_TEAMS = {
    ("sofascore", "42"): {"r1"},
    ("understat", "83"): {"r1"},
    ("sofascore", "38"): {"r2"},
    ("understat", "80"): {"r2"},
}
VEREINS = {"r1": {"11"}, "r2": {"631"}}
TRANSFERMARKT = {
    "11": TransfermarktClub("11", "Arsenal FC", "Arsenal", "England"),
    "631": TransfermarktClub("631", "Chelsea FC", "Chelsea", "England"),
}


def _plan(
    teams: list[Team] = TEAMS,
    links: dict[tuple[str, str], str] | None = None,
    taken: set[str] | None = None,
    transfermarkt: dict[str, TransfermarktClub] = TRANSFERMARKT,
) -> ClubPlan:
    return plan_clubs(teams, REEP_TEAMS, VEREINS, transfermarkt, links or {}, taken or set())


def test_a_reep_team_creates_one_club_linking_every_bridged_team() -> None:
    plan = _plan()

    assert [c.club_id for c in plan.clubs] == ["arsenal", "chelsea"]
    assert plan.clubs[0].display_name == "Arsenal"
    assert plan.links == {
        ("reep", "r1"): "arsenal",
        ("sofascore", "42"): "arsenal",
        ("understat", "83"): "arsenal",
        ("transfermarkt", "11"): "arsenal",
        ("reep", "r2"): "chelsea",
        ("sofascore", "38"): "chelsea",
        ("understat", "80"): "chelsea",
        ("transfermarkt", "631"): "chelsea",
    }
    assert plan.unresolved == []


def test_a_clash_appends_the_country_and_a_second_clash_is_listed_not_created() -> None:
    clash = {"631": TransfermarktClub("631", "Arsenal", "Arsenal", "England")}

    plan = _plan(taken={"arsenal"}, transfermarkt=TRANSFERMARKT | clash)

    # r1 comes first: it takes arsenal-england, so r2 finds both slugs taken.
    assert [c.club_id for c in plan.clubs] == ["arsenal-england"]
    assert {(t.provider, t.external_id) for t in plan.unresolved} == {
        ("sofascore", "38"),
        ("understat", "80"),
    }
    assert "already taken" in plan.unresolved[0].reason
    assert ("sofascore", "38") not in plan.links


def test_a_rerun_in_any_order_creates_no_club_and_changes_no_slug() -> None:
    first = _plan()
    links = first.links
    taken = {c.club_id for c in first.clubs}

    for teams in (TEAMS, list(reversed(TEAMS)), TEAMS[2:] + TEAMS[:2]):
        again = _plan(teams, links=links, taken=taken)
        assert (again.clubs, again.links, again.unresolved) == ([], {}, [])


def test_a_fresh_build_gives_the_same_slugs_in_any_order() -> None:
    clash = {"631": TransfermarktClub("631", "Arsenal", "Arsenal", "England")}
    plans = [_plan(teams, transfermarkt=TRANSFERMARKT | clash) for teams in (TEAMS, TEAMS[::-1])]

    assert [c.club_id for c in plans[0].clubs] == ["arsenal", "arsenal-england"]
    assert plans[0].clubs == plans[1].clubs


def test_a_team_new_to_a_known_reep_team_joins_its_club() -> None:
    links = {("reep", "r1"): "arsenal", ("sofascore", "42"): "arsenal"}

    plan = _plan(TEAMS[:2], links=links, taken={"arsenal"})

    assert plan.clubs == []
    assert plan.links == {("understat", "83"): "arsenal", ("transfermarkt", "11"): "arsenal"}


def test_teams_that_get_no_club_are_listed_with_the_reason() -> None:
    teams = [
        Team("sofascore", "99", "Nowhere Town"),  # no Reep team
        Team("sofascore", "38", "Chelsea"),  # its verein has no staged name
    ]

    plan = _plan(teams, transfermarkt={})

    assert plan.clubs == [] and plan.links == {}
    reasons = {t.external_id: t.reason for t in plan.unresolved}
    assert reasons["99"] == "no Reep team in the pinned release"
    assert "no staged Transfermarkt club" in reasons["38"]


def test_a_link_already_stored_is_never_moved() -> None:
    # The Sofascore team is linked to one Club and its Reep team to another.
    links = {("sofascore", "42"): "arsenal", ("reep", "r1"): "gunners"}

    plan = _plan(TEAMS[:2], links=links, taken={"arsenal", "gunners"})

    assert plan.clubs == [] and plan.links == {}
    assert "several Clubs: arsenal, gunners" in plan.unresolved[0].reason


async def _store_arsenal(connection: AsyncConnection[Any]) -> ClubStore:
    store = ClubStore(connection)
    await store.apply(_plan(TEAMS[:2]), "reep:test")
    return store


async def test_a_rename_keeps_the_old_slug_as_an_alias(connection: AsyncConnection[Any]) -> None:
    store = await _store_arsenal(connection)

    await store.rename("arsenal", "the-arsenal")

    assert await store.taken() == {"the-arsenal", "arsenal"}
    links = await store.links()
    assert set(links.values()) == {"the-arsenal"}
    cursor = await connection.execute("SELECT alias, club_id FROM identity.club_aliases")
    assert await cursor.fetchall() == [("arsenal", "the-arsenal")]

    # Renaming back reclaims the former slug.
    await store.rename("the-arsenal", "arsenal")
    cursor = await connection.execute("SELECT alias, club_id FROM identity.club_aliases")
    assert await cursor.fetchall() == [("the-arsenal", "arsenal")]


@pytest.mark.parametrize("new_id", ["Arsenal FC", "chelsea", "gunners"])
async def test_a_rename_to_a_taken_or_invalid_slug_is_refused(
    connection: AsyncConnection[Any], new_id: str
) -> None:
    store = await _store_arsenal(connection)
    await connection.execute(
        "INSERT INTO identity.clubs (club_id, name, display_name) VALUES ('chelsea', 'C', 'C')"
    )
    await connection.execute(
        "INSERT INTO identity.club_aliases (alias, club_id) VALUES ('gunners', 'chelsea')"
    )

    with pytest.raises(ClubTaken):
        await store.rename("arsenal", new_id)
