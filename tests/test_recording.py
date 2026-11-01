import logging
from datetime import timedelta
from typing import Any

import httpx
import pytest
from psycopg import AsyncConnection

from football_data_ai_platform.fetch_attempts import FetchAttemptStore
from football_data_ai_platform.raw_responses import RawResponseStore
from football_data_ai_platform.sources.recording import (
    RawResponseMissingError,
    RecordingTransport,
    ReplayTransport,
)
from football_data_ai_platform.sources.transfermarkt import recording_transport, replay_transport
from football_data_ai_platform.sources.transfermarkt.squads import fetch_squad
from tests.transfermarkt_stub import CLUB_ID, PLAYER_IDS, SEASON_ID, TransfermarktStub


async def test_recording_stores_one_raw_response_per_upstream_request(
    connection: AsyncConnection[Any],
) -> None:
    stub = TransfermarktStub()
    transport = recording_transport(
        stub.transport(), RawResponseStore(connection), FetchAttemptStore(connection)
    )

    squad = await fetch_squad(transport, CLUB_ID, SEASON_ID)

    assert [player.id for player in squad.players] == list(PLAYER_IDS)
    cursor = await connection.execute(
        "SELECT url, status_code, body FROM raw.raw_responses ORDER BY id"
    )
    rows = await cursor.fetchall()
    assert len(rows) == len(stub.requests) == transport.recorded
    assert sorted(url for url, _, _ in rows) == sorted(str(r.url) for r in stub.requests)
    # Error responses are Raw Responses too: player 103 has no transfer history.
    assert any(status == 404 for _, status, _ in rows)
    # Bodies are stored as received.
    bodies = {url: bytes(body) for url, _, body in rows}
    expected = TransfermarktStub()
    for request in list(stub.requests):
        assert bodies[str(request.url)] == expected.handle(request).content


async def test_replay_reassembles_the_same_squad_without_network(
    connection: AsyncConnection[Any],
) -> None:
    store = RawResponseStore(connection)
    live = await fetch_squad(
        recording_transport(TransfermarktStub().transport(), store, FetchAttemptStore(connection)),
        CLUB_ID,
        SEASON_ID,
    )

    # The replay transport has no backend at all: it can only answer from the store.
    replayed = await fetch_squad(replay_transport(store), CLUB_ID, SEASON_ID)

    assert replayed == live


async def test_replay_of_an_unrecorded_request_is_an_error(
    connection: AsyncConnection[Any],
) -> None:
    with pytest.raises(RawResponseMissingError):
        await fetch_squad(replay_transport(RawResponseStore(connection)), CLUB_ID, SEASON_ID)


async def test_stored_and_logged_urls_contain_no_credentials(
    connection: AsyncConnection[Any], caplog: pytest.LogCaptureFixture
) -> None:
    backend = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    transport = recording_transport(
        backend, RawResponseStore(connection), FetchAttemptStore(connection)
    )
    request = httpx.Request("GET", "https://user:s3cret@example.com/club/1?token=t0ken&season=2025")

    with caplog.at_level(logging.DEBUG):
        await transport.handle_async_request(request)

    cursor = await connection.execute("SELECT url FROM raw.raw_responses")
    (stored_url,) = await cursor.fetchone() or ("",)
    assert stored_url == "https://example.com/club/1?token=REDACTED&season=2025"
    for secret in ("user", "s3cret", "t0ken"):
        assert secret not in stored_url
        assert secret not in caplog.text


async def test_stored_failures_are_fetched_again_inside_the_freshness_window(
    connection: AsyncConnection[Any],
) -> None:
    statuses = iter([503, 200])
    backend = httpx.MockTransport(lambda request: httpx.Response(next(statuses), json={}))
    transport = recording_transport(
        backend,
        RawResponseStore(connection),
        FetchAttemptStore(connection),
        freshness=timedelta(hours=1),
    )
    request = httpx.Request("GET", "https://example.com/club/1")

    first = await transport.handle_async_request(request)
    second = await transport.handle_async_request(request)
    third = await transport.handle_async_request(request)

    assert [first.status_code, second.status_code, third.status_code] == [503, 200, 200]
    assert (transport.recorded, transport.reused) == (2, 1)


async def test_cache_hits_are_logged(
    connection: AsyncConnection[Any], caplog: pytest.LogCaptureFixture
) -> None:
    store = RawResponseStore(connection)
    stub = TransfermarktStub()
    attempts = FetchAttemptStore(connection)
    await fetch_squad(recording_transport(stub.transport(), store, attempts), CLUB_ID, SEASON_ID)

    with caplog.at_level(logging.INFO):
        await fetch_squad(
            recording_transport(stub.transport(), store, attempts, freshness=timedelta(hours=1)),
            CLUB_ID,
            SEASON_ID,
        )

    assert "Reused Raw Response GET" in caplog.text


async def test_a_second_source_records_and_reuses_under_its_own_name_and_failure_test(
    connection: AsyncConnection[Any],
) -> None:
    # A fake source whose failure test is unlike Transfermarkt's: a 418 is a failure, a 503 is not.
    def is_teapot(response: httpx.Response) -> bool:
        return response.status_code == 418

    store = RawResponseStore(connection)
    statuses = {"https://fake.example/teapot": 418, "https://fake.example/down": 503}
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(statuses[str(request.url)], json={})

    def transport() -> RecordingTransport:
        return RecordingTransport(
            httpx.MockTransport(handle),
            store,
            FetchAttemptStore(connection),
            "fake",
            is_teapot,
            freshness=timedelta(hours=1),
        )

    for url in statuses:
        await transport().handle_async_request(httpx.Request("GET", url))
    second = transport()
    for url in statuses:
        await second.handle_async_request(httpx.Request("GET", url))

    cursor = await connection.execute("SELECT DISTINCT source FROM raw.raw_responses")
    assert await cursor.fetchall() == [("fake",)]
    # Its own failure test decides: the stored 418 is fetched again, the stored 503 answers.
    assert calls == [*statuses, "https://fake.example/teapot"]
    assert (second.recorded, second.reused) == (1, 1)
    # Another source's replay never sees these Raw Responses.
    with pytest.raises(RawResponseMissingError):
        await replay_transport(store).handle_async_request(
            httpx.Request("GET", "https://fake.example/down")
        )
    replayed = await ReplayTransport(store, "fake").handle_async_request(
        httpx.Request("GET", "https://fake.example/down")
    )
    assert replayed.status_code == 503
