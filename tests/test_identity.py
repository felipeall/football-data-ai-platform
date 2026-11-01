from datetime import date
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import AsyncConnection, sql

from football_data_ai_platform.config import Settings
from football_data_ai_platform.identity.activities import Resolution, SourceRecords, resolve
from football_data_ai_platform.identity.names import normalize, similarity
from football_data_ai_platform.identity.rules import (
    ACCEPT_FROM,
    JUDGE_FROM,
    Appearance,
    Method,
    Proposal,
    ReepBridge,
    ReepConflict,
    SourcePlayer,
    sofascore_proposals,
    understat_proposals,
)
from football_data_ai_platform.identity.store import IdentityStore

BORN = date(1999, 5, 1)
RELEASE = "2026-01-02.1"


def test_names_compare_without_accents_punctuation_or_word_order() -> None:
    assert normalize("Đorđe Petrović") == "dorde petrovic"
    assert normalize("Max Weiß") == "max weiss"
    assert normalize("Jean-Mattéo Bahoya") == "jean matteo bahoya"
    assert similarity("Konsa Ngoyo, Ezri", "Ezri Konsa") == 1.0
    assert similarity("Andrew Robertson", "Andy Robertson") < 0.9


def test_sofascore_links_on_birth_date_and_the_same_name() -> None:
    transfermarkt = {
        1: SourcePlayer(10, "Đorđe Petrović", BORN),
        2: SourcePlayer(11, "Someone Else", BORN),
    }

    (proposal,) = sofascore_proposals([SourcePlayer(5, "Djordje Petrović", BORN)], transfermarkt)

    assert (proposal.source, proposal.player, proposal.method, proposal.confidence) == (
        "sofascore",
        1,
        Method.NAME_BIRTH_DATE,
        1.0,
    )
    assert proposal.accepted


def test_sofascore_leaves_a_nickname_to_the_judge() -> None:
    (proposal,) = sofascore_proposals(
        [SourcePlayer(5, "Andy Robertson", BORN)], {1: SourcePlayer(10, "Andrew Robertson", BORN)}
    )

    assert proposal.method == Method.FUZZY
    assert JUDGE_FROM <= proposal.confidence < ACCEPT_FROM


def test_sofascore_does_not_link_on_birth_date_alone() -> None:
    assert (
        sofascore_proposals(
            [SourcePlayer(5, "Jadon Sancho", BORN)], {1: SourcePlayer(10, "Tom McGill", BORN)}
        )
        == []
    )
    assert sofascore_proposals([SourcePlayer(5, "Tom McGill", None)], {}) == []


def test_sofascore_leaves_two_same_named_players_to_the_judge() -> None:
    transfermarkt = {1: SourcePlayer(10, "Joe Smith", BORN), 2: SourcePlayer(11, "Joe Smith", BORN)}

    (proposal,) = sofascore_proposals([SourcePlayer(5, "Joe Smith", BORN)], transfermarkt)

    assert not proposal.accepted


def test_sofascore_links_the_same_name_born_a_day_apart() -> None:
    # Omari Hutchinson: Sofascore 2003-10-29, Transfermarkt 2003-10-30.
    transfermarkt = {1: SourcePlayer(741238, "Omari Hutchinson", date(2003, 10, 30))}

    (proposal,) = sofascore_proposals(
        [SourcePlayer(1090963, "Omari Hutchinson", date(2003, 10, 29))], transfermarkt
    )

    assert (proposal.player, proposal.method, proposal.accepted) == (
        1,
        Method.NAME_BIRTH_DATE,
        True,
    )
    assert proposal.evidence["birth_date_gap_days"] == 1


def test_sofascore_does_not_link_the_same_name_born_two_days_apart() -> None:
    transfermarkt = {1: SourcePlayer(10, "Joe Smith", date(1999, 5, 3))}

    assert sofascore_proposals([SourcePlayer(5, "Joe Smith", BORN)], transfermarkt) == []


def test_sofascore_records_no_gap_for_the_same_birth_date() -> None:
    (proposal,) = sofascore_proposals(
        [SourcePlayer(5, "Joe Smith", BORN)], {1: SourcePlayer(10, "Joe Smith", BORN)}
    )

    assert proposal.evidence["birth_date_gap_days"] == 0


def test_sofascore_leaves_two_same_named_players_a_day_apart_to_the_judge() -> None:
    # The exact birth date alone would link; a same-named record a day off makes it
    # ambiguous, so the judge decides, proposed the exact-date record first.
    transfermarkt = {
        1: SourcePlayer(10, "Joe Smith", date(1999, 4, 30)),
        2: SourcePlayer(11, "Joe Smith", BORN),
    }

    (proposal,) = sofascore_proposals([SourcePlayer(5, "Joe Smith", BORN)], transfermarkt)

    assert (proposal.player, proposal.method, proposal.accepted) == (2, Method.FUZZY, False)


# Sofascore players 1 and 2 link to Players 501 and 502.
LINKED = {1: 501, 2: 502}


def _season(player_id: int, minutes: list[int], club: str = "arsenal") -> list[Appearance]:
    return [Appearance(f"m{i}", club, player_id, m) for i, m in enumerate(minutes)]


def test_understat_links_the_same_named_teammate_who_played_the_same_minutes() -> None:
    understat = _season(100, [90, 90, 80, 90])
    sofascore = _season(1, [90, 90, 81, 90]) + _season(2, [90, 90, 90, 90])

    (proposal,) = understat_proposals(
        understat,
        sofascore,
        {100: "Ezri Konsa Ngoyo"},
        {1: "Ezri Konsa", 2: "Ollie Watkins"},
        LINKED,
    )

    assert (proposal.player, proposal.method, proposal.confidence) == (
        501,
        Method.CO_APPEARANCE,
        1.0,
    )


def test_understat_does_not_link_a_contained_name_whose_minutes_disagree() -> None:
    # "Gabriel" is contained in "Gabriel Martinelli", but the minutes are Gabriel
    # Magalhães's: the co-appearance rule must not pick Martinelli.
    understat = _season(100, [90, 90, 90, 90])
    sofascore = _season(1, [60, 45, 70, 30]) + _season(2, [90, 90, 90, 90])

    (proposal,) = understat_proposals(
        understat,
        sofascore,
        {100: "Gabriel"},
        {1: "Gabriel Martinelli", 2: "Gabriel Magalhães"},
        LINKED,
    )

    assert proposal.player == 502
    assert proposal.method == Method.FUZZY


def test_understat_does_not_link_a_lone_contained_name_whose_minutes_disagree() -> None:
    # Only Martinelli is in the Sofascore data: the name contains "Gabriel" and he played
    # every Match, but his minutes are not this player's.
    understat = _season(100, [90, 90, 90, 90])
    sofascore = _season(1, [60, 45, 70, 30])

    (proposal,) = understat_proposals(
        understat, sofascore, {100: "Gabriel"}, {1: "Gabriel Martinelli"}, LINKED
    )

    assert proposal.method == Method.FUZZY
    assert not proposal.accepted


def test_understat_scores_a_nickname_on_name_co_appearance_and_minutes() -> None:
    understat = _season(100, [90] * 10)
    sofascore = _season(1, [90] * 10) + _season(2, [90] * 10)

    (proposal,) = understat_proposals(
        understat, sofascore, {100: "Matthew Cash"}, {1: "Matty Cash", 2: "Ollie Watkins"}, LINKED
    )

    assert (proposal.player, proposal.method) == (501, Method.FUZZY)
    assert proposal.accepted


def test_understat_leaves_a_player_whose_sofascore_record_has_no_player_unlinked() -> None:
    # An academy debutant: his own Sofascore record (3) is unlinked, and a teammate who came
    # on in his only Match must not be proposed in his place.
    understat = _season(100, [20])
    sofascore = _season(1, [90]) + _season(3, [20])

    assert (
        understat_proposals(
            understat, sofascore, {100: "Shea Lacey"}, {1: "Luke Shaw", 3: "Shea Lacey"}, LINKED
        )
        == []
    )


def test_understat_ignores_players_of_another_club_and_rare_teammates() -> None:
    understat = _season(100, [90] * 4)
    # Same name, other club; and a teammate in only one of the four Matches.
    sofascore = [*_season(1, [90] * 4, club="chelsea"), Appearance("m0", "arsenal", 2, 90)]

    assert (
        understat_proposals(
            understat, sofascore, {100: "Joe Smith"}, {1: "Joe Smith", 2: "Joe Smith"}, LINKED
        )
        == []
    )


@pytest.fixture
async def identity(connection: AsyncConnection[Any]) -> IdentityStore:
    await connection.execute(
        "TRUNCATE identity.identity_candidates, identity.players RESTART IDENTITY"
    )
    return IdentityStore(connection)


async def _candidates(connection: AsyncConnection[Any]) -> list[tuple[Any, ...]]:
    cursor = await connection.execute(
        "SELECT source, source_record_id, player_id, method, status"
        " FROM identity.identity_candidates ORDER BY candidate_id"
    )
    return await cursor.fetchall()


async def _players(connection: AsyncConnection[Any]) -> int:
    cursor = await connection.execute("SELECT count(*) FROM identity.players")
    row = await cursor.fetchone()
    assert row is not None
    return row[0]


def _records(
    sofascore: list[SourcePlayer] | None = None,
    transfermarkt: list[SourcePlayer] | None = None,
    understat: list[SourcePlayer] | None = None,
    sofascore_appearances: list[Appearance] | None = None,
    understat_appearances: list[Appearance] | None = None,
) -> SourceRecords:
    return SourceRecords(
        sofascore or [],
        transfermarkt or [],
        understat or [],
        sofascore_appearances or [],
        understat_appearances or [],
    )


def _unlinked(resolution: Resolution) -> list[tuple[str, int, bool]]:
    return [(s, p.source_record_id, pending) for s, p, pending in resolution.unlinked]


# Person A: Sofascore 1, Transfermarkt 10, Understat 100. Transfermarkt 11 has no Reep person.
# Sofascore 2 and Understat 101 match no Player.
REEP = [
    ReepBridge("rA", "sofascore", 1),
    ReepBridge("rA", "transfermarkt", 10),
    ReepBridge("rA", "understat", 100),
]
WORLD = _records(
    sofascore=[
        SourcePlayer(1, "Ezri Konsa", BORN),
        SourcePlayer(2, "Lone Debutant", date(2007, 1, 1)),
    ],
    transfermarkt=[
        SourcePlayer(10, "Ezri Konsa", BORN),
        SourcePlayer(11, "Tom McGill", date(2000, 3, 25)),
    ],
    understat=[SourcePlayer(100, "Ezri Konsa"), SourcePlayer(101, "Lone Debutant")],
    sofascore_appearances=_season(1, [90, 90]) + _season(2, [10, 10]),
    understat_appearances=_season(100, [90, 90]) + _season(101, [10, 10], club="chelsea"),
)


async def test_reep_creates_the_player_before_transfermarkt_and_match_sources_create_none(
    identity: IdentityStore, connection: AsyncConnection[Any]
) -> None:
    resolution = await resolve(identity, WORLD, RELEASE, REEP)

    assert resolution.players_created == await _players(connection) == 2
    assert await _candidates(connection) == [
        ("sofascore", "1", 1, "reep_bridge", "accepted"),
        ("transfermarkt", "10", 1, "reep_bridge", "accepted"),
        ("understat", "100", 1, "reep_bridge", "accepted"),
        ("transfermarkt", "11", 2, "transfermarkt_player", "accepted"),
    ]
    assert _unlinked(resolution) == [("sofascore", 2, False), ("understat", 101, False)]


async def test_without_a_reep_release_transfermarkt_creates_every_player(
    identity: IdentityStore, connection: AsyncConnection[Any]
) -> None:
    resolution = await resolve(identity, WORLD, None, None)

    # Sofascore 1 links to Transfermarkt 10's Player by name and birth date, and Understat
    # 100 by co-appearance with Sofascore 1.
    assert resolution.players_created == 2
    assert await _candidates(connection) == [
        ("transfermarkt", "10", 1, "transfermarkt_player", "accepted"),
        ("transfermarkt", "11", 2, "transfermarkt_player", "accepted"),
        ("sofascore", "1", 1, "name_birth_date", "accepted"),
        ("understat", "100", 1, "co_appearance", "accepted"),
    ]


async def test_a_sofascore_player_without_a_reep_person_links_by_name_and_birth_date(
    identity: IdentityStore, connection: AsyncConnection[Any]
) -> None:
    records = _records(
        sofascore=[*WORLD.sofascore, SourcePlayer(3, "Tom McGill", date(2000, 3, 25))],
        transfermarkt=WORLD.transfermarkt,
    )

    await resolve(identity, records, RELEASE, REEP)

    assert ("sofascore", "3", 2, "name_birth_date", "accepted") in await _candidates(connection)


async def test_an_understat_player_links_by_co_appearance_with_a_player_s_sofascore_appearances(
    identity: IdentityStore, connection: AsyncConnection[Any]
) -> None:
    # Reep maps only Sofascore 1; Understat 100 links to its Player by co-appearance.
    await resolve(identity, WORLD, RELEASE, REEP[:1])

    assert ("understat", "100", 1, "co_appearance", "accepted") in await _candidates(connection)


async def test_a_reep_bridge_links_a_new_record_to_the_player_of_a_linked_one(
    identity: IdentityStore, connection: AsyncConnection[Any]
) -> None:
    await resolve(identity, _records(sofascore=WORLD.sofascore[:1]), RELEASE, REEP)

    # A later run holds Transfermarkt 10, which person A's Player already has a link from.
    resolution = await resolve(
        identity,
        _records(sofascore=WORLD.sofascore[:1], transfermarkt=WORLD.transfermarkt[:1]),
        RELEASE,
        REEP,
    )

    assert resolution.players_created == 0
    assert await _candidates(connection) == [
        ("sofascore", "1", 1, "reep_bridge", "accepted"),
        ("transfermarkt", "10", 1, "reep_bridge", "accepted"),
    ]


async def test_a_reep_person_whose_records_link_to_two_players_is_listed_not_linked(
    identity: IdentityStore, connection: AsyncConnection[Any]
) -> None:
    # Without Reep, Sofascore 1 and Transfermarkt 10 link to Transfermarkt 10's Player, and
    # Transfermarkt 11 gets its own. A later release says 10 and 11 are one person.
    await resolve(identity, WORLD, None, None)
    first = await _candidates(connection)
    bridges = [ReepBridge("rA", "transfermarkt", 10), ReepBridge("rA", "transfermarkt", 11)]

    resolution = await resolve(identity, WORLD, RELEASE, bridges)

    assert resolution.reep_conflicts == [
        ReepConflict("rA", [1, 2], [("transfermarkt", 10), ("transfermarkt", 11)])
    ]
    assert await _candidates(connection) == first


async def test_a_rerun_creates_no_players_and_changes_no_ids(
    identity: IdentityStore, connection: AsyncConnection[Any]
) -> None:
    await resolve(identity, WORLD, RELEASE, REEP)
    first = await _candidates(connection)

    again = await resolve(identity, WORLD, RELEASE, REEP)

    assert (again.players_created, again.candidates_stored) == (0, 0)
    assert await _candidates(connection) == first


async def test_reep_disagreeing_with_understat_co_appearance_waits_for_the_judge(
    identity: IdentityStore, connection: AsyncConnection[Any]
) -> None:
    # Wolves' André: Reep maps Sofascore 1 to Understat 802, who never played with him; our
    # data says Understat 13022.
    records = _records(
        sofascore=[SourcePlayer(1, "André", BORN)],
        understat=[SourcePlayer(802, "André"), SourcePlayer(13022, "André")],
        sofascore_appearances=_season(1, [90, 90], club="wolves"),
        understat_appearances=[
            *_season(13022, [90, 90], club="wolves"),
            Appearance("m9", "fulham", 802, 90),
        ],
    )
    bridges = [ReepBridge("rA", "sofascore", 1), ReepBridge("rA", "understat", 802)]

    await resolve(identity, records, RELEASE, bridges)

    assert await _candidates(connection) == [
        ("sofascore", "1", 1, "reep_bridge", "accepted"),
        ("understat", "802", 1, "reep_bridge", "pending"),
        ("understat", "13022", 1, "co_appearance", "accepted"),
    ]
    cursor = await connection.execute(
        "SELECT evidence->>'disagreement' FROM identity.identity_candidates"
        " WHERE source_record_id = '802'"
    )
    assert await cursor.fetchone() == (
        "never played in the same Match for the same Club as Sofascore player 1",
    )


async def test_reep_disagreeing_with_a_transfermarkt_birth_date_waits_for_the_judge(
    identity: IdentityStore, connection: AsyncConnection[Any]
) -> None:
    records = _records(
        sofascore=[SourcePlayer(1, "Ezri Konsa", BORN)],
        transfermarkt=[
            SourcePlayer(10, "Ezri Konsa", date(1999, 5, 2)),  # a day off: the same man
            SourcePlayer(11, "Ezri Konsa", date(1997, 5, 1)),
        ],
    )
    # Reep bridges both to person A; 11's birth date is two years off.
    bridges = [
        ReepBridge("rA", "sofascore", 1),
        ReepBridge("rA", "transfermarkt", 10),
        ReepBridge("rA", "transfermarkt", 11),
    ]

    await resolve(identity, records, RELEASE, bridges)

    assert await _candidates(connection) == [
        ("sofascore", "1", 1, "reep_bridge", "accepted"),
        ("transfermarkt", "10", 1, "reep_bridge", "accepted"),
        ("transfermarkt", "11", 1, "reep_bridge", "pending"),
    ]


async def test_a_rejected_candidate_is_not_proposed_again(
    identity: IdentityStore, connection: AsyncConnection[Any]
) -> None:
    await identity.propose([Proposal("understat", 100, "new", Method.FUZZY, 0.7)])
    await connection.execute(
        "UPDATE identity.identity_candidates SET status = 'rejected' WHERE source = 'understat'"
    )

    stored, created = await identity.propose(
        [Proposal("understat", 100, 1, Method.CO_APPEARANCE, 1.0)]
    )

    assert (stored, created) == (0, 0)
    assert (await _candidates(connection))[-1] == ("understat", "100", 1, "fuzzy", "rejected")


async def test_a_second_link_to_a_player_from_one_source_waits_for_the_judge(
    identity: IdentityStore, connection: AsyncConnection[Any]
) -> None:
    await identity.propose(
        [
            Proposal("sofascore", 1, "new", Method.NAME_BIRTH_DATE, 1.0),
            Proposal("sofascore", 2, "new", Method.NAME_BIRTH_DATE, 1.0),
        ]
    )

    assert [row[2:] for row in await _candidates(connection)] == [
        (1, "name_birth_date", "accepted"),
        (1, "name_birth_date", "pending"),
    ]


@pytest.mark.parametrize(
    ("status", "decided_by"),
    [("accepted", "judge:claude-haiku"), ("accepted", None), ("rejected", "rule:fuzzy")],
)
def test_recreating_players_stops_when_a_judge_or_reviewer_decided(
    status: str, decided_by: str | None, worker: str
) -> None:
    settings = Settings()
    # `_0008` and the worker's number, like `_test`: worktree database names leave no more room.
    database = f"{settings.postgres_db}_0008{worker}"
    url = settings.model_copy(update={"postgres_db": database}).database_url
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False
    try:
        admin = psycopg.connect(settings.database_url, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as error:
        pytest.skip(f"Postgres unreachable ({error})")
    with admin:
        name = sql.Identifier(database)
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(name))
    try:
        command.upgrade(config, "0007")
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("INSERT INTO identity.players DEFAULT VALUES")
            conn.execute(
                "INSERT INTO identity.identity_candidates"
                " (source, source_record_id, player_id, method, confidence, status, decided_by)"
                " VALUES ('sofascore', '1', 1, 'sofascore_anchor', 1, 'accepted', 'rule:x'),"
                " ('understat', '100', 1, 'fuzzy', 0.7, %s, %s)",
                (status, decided_by),
            )

            with pytest.raises(Exception, match="decided by the LLM judge or a reviewer"):
                command.upgrade(config, "head")

            cursor = conn.execute("SELECT count(*) FROM identity.identity_candidates")
            assert cursor.fetchone() == (2,)
            # With only rule decisions, it clears both tables and keeps counting Player IDs.
            conn.execute(
                "UPDATE identity.identity_candidates SET status = 'pending', decided_by = NULL"
                " WHERE source = 'understat'"
            )
            command.upgrade(config, "head")
            cursor = conn.execute(
                "SELECT (SELECT count(*) FROM identity.identity_candidates),"
                " (SELECT count(*) FROM identity.players)"
            )
            assert cursor.fetchone() == (0, 0)
            cursor = conn.execute("INSERT INTO identity.players DEFAULT VALUES RETURNING player_id")
            assert cursor.fetchone() == (2,)
    finally:
        with psycopg.connect(settings.database_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )


# The creation check (ADR 39): Reep's person rA has only Sofascore 1; Transfermarkt 10 is the
# same man, with no Reep bridge.
ONLY_SOFASCORE = [ReepBridge("rA", "sofascore", 1), ReepBridge("rA", "understat", 100)]


def _konsa(sofascore: str = "Ezri Konsa", transfermarkt: str = "Ezri Konsa") -> SourceRecords:
    return _records(
        sofascore=[SourcePlayer(1, sofascore, BORN)],
        transfermarkt=[SourcePlayer(10, transfermarkt, date(1999, 5, 2))],
        understat=[SourcePlayer(100, "Ezri Konsa")],
        sofascore_appearances=_season(1, [90, 90]),
        understat_appearances=_season(100, [90, 90]),
    )


async def _reject(connection: AsyncConnection[Any], source: str, record: int) -> None:
    await connection.execute(
        "UPDATE identity.identity_candidates SET status = 'rejected'"
        " WHERE source = %s AND source_record_id = %s",
        (source, str(record)),
    )


async def test_transfermarkt_links_to_the_player_reep_created_without_its_bridge(
    identity: IdentityStore, connection: AsyncConnection[Any]
) -> None:
    resolution = await resolve(identity, _konsa(), RELEASE, ONLY_SOFASCORE)

    assert resolution.players_created == await _players(connection) == 1
    assert await _candidates(connection) == [
        ("sofascore", "1", 1, "reep_bridge", "accepted"),
        ("understat", "100", 1, "reep_bridge", "accepted"),
        ("transfermarkt", "10", 1, "creation_check", "accepted"),
    ]


async def test_reep_links_to_the_player_transfermarkt_created_a_run_before(
    identity: IdentityStore, connection: AsyncConnection[Any]
) -> None:
    # Run N holds only the Transfermarkt record; run N+1 a new Reep release bridging the
    # Sofascore and Understat records, but not the Transfermarkt one.
    await resolve(identity, _records(transfermarkt=_konsa().transfermarkt), None, None)

    resolution = await resolve(identity, _konsa(), RELEASE, ONLY_SOFASCORE)

    assert resolution.players_created == 0
    assert await _players(connection) == 1
    assert await _candidates(connection) == [
        ("transfermarkt", "10", 1, "transfermarkt_player", "accepted"),
        ("sofascore", "1", 1, "creation_check", "accepted"),
        ("understat", "100", 1, "reep_bridge", "accepted"),
    ]


async def test_a_similar_name_waits_for_the_judge_and_creates_no_player(
    identity: IdentityStore, connection: AsyncConnection[Any]
) -> None:
    resolution = await resolve(
        identity, _konsa("Andy Robertson", "Andrew Robertson"), RELEASE, ONLY_SOFASCORE
    )

    assert resolution.players_created == await _players(connection) == 1
    assert (await _candidates(connection))[-1] == (
        "transfermarkt",
        "10",
        1,
        "creation_check",
        "pending",
    )


async def test_a_rejected_creation_check_creates_the_player_on_the_next_run(
    identity: IdentityStore, connection: AsyncConnection[Any]
) -> None:
    records = _konsa("Andy Robertson", "Andrew Robertson")
    await resolve(identity, records, RELEASE, ONLY_SOFASCORE)
    await _reject(connection, "transfermarkt", 10)

    resolution = await resolve(identity, records, RELEASE, ONLY_SOFASCORE)

    assert resolution.players_created == 1
    assert (await _candidates(connection))[-1] == (
        "transfermarkt",
        "10",
        2,
        "transfermarkt_player",
        "accepted",
    )


async def test_a_reep_person_waits_while_its_creation_check_waits(
    identity: IdentityStore, connection: AsyncConnection[Any]
) -> None:
    records = _konsa("Andy Robertson", "Andrew Robertson")
    await resolve(identity, _records(transfermarkt=records.transfermarkt), None, None)

    # Sofascore 1 may be Transfermarkt 10's Player: neither it nor Understat 100, the same
    # Reep person, creates a Player, on this run or the next.
    await resolve(identity, records, RELEASE, ONLY_SOFASCORE)
    resolution = await resolve(identity, records, RELEASE, ONLY_SOFASCORE)

    assert await _players(connection) == 1
    assert await _candidates(connection) == [
        ("transfermarkt", "10", 1, "transfermarkt_player", "accepted"),
        ("sofascore", "1", 1, "creation_check", "pending"),
    ]
    assert _unlinked(resolution) == [("sofascore", 1, True), ("understat", 100, False)]

    # A person rejects the link: the Reep person creates its own Player.
    await _reject(connection, "sofascore", 1)
    resolution = await resolve(identity, records, RELEASE, ONLY_SOFASCORE)

    assert resolution.players_created == 1
    assert (await _candidates(connection))[-2:] == [
        ("sofascore", "1", 2, "reep_bridge", "accepted"),
        ("understat", "100", 2, "reep_bridge", "accepted"),
    ]


async def test_an_accepted_creation_check_brings_the_reep_person_s_other_records(
    identity: IdentityStore, connection: AsyncConnection[Any]
) -> None:
    records = _konsa("Andy Robertson", "Andrew Robertson")
    await resolve(identity, _records(transfermarkt=records.transfermarkt), None, None)
    await resolve(identity, records, RELEASE, ONLY_SOFASCORE)
    await connection.execute(
        "UPDATE identity.identity_candidates SET status = 'accepted' WHERE status = 'pending'"
    )

    resolution = await resolve(identity, records, RELEASE, ONLY_SOFASCORE)

    assert resolution.players_created == 0
    assert (await _candidates(connection))[-1] == (
        "understat",
        "100",
        1,
        "reep_bridge",
        "accepted",
    )


async def test_two_reep_persons_matching_one_player_link_only_the_first(
    identity: IdentityStore, connection: AsyncConnection[Any]
) -> None:
    await resolve(identity, _records(transfermarkt=_konsa().transfermarkt), None, None)
    records = _records(
        sofascore=[SourcePlayer(1, "Ezri Konsa", BORN), SourcePlayer(2, "Ezri Konsa", BORN)],
        transfermarkt=_konsa().transfermarkt,
        understat=[SourcePlayer(100, "Ezri Konsa")],
        sofascore_appearances=_season(2, [90, 90]),
        understat_appearances=_season(100, [90, 90]),
    )
    bridges = [
        ReepBridge("rA", "sofascore", 1),
        ReepBridge("rB", "sofascore", 2),
        ReepBridge("rB", "understat", 100),
    ]

    await resolve(identity, records, RELEASE, bridges)

    # Reep says 1 and 2 are two men, so the judge decides the second; rB waits with it.
    assert await _players(connection) == 1
    assert await _candidates(connection) == [
        ("transfermarkt", "10", 1, "transfermarkt_player", "accepted"),
        ("sofascore", "1", 1, "creation_check", "accepted"),
        ("sofascore", "2", 1, "creation_check", "pending"),
    ]
