import asyncio
import itertools
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from psycopg import AsyncConnection
from pydantic import SecretStr
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from football_data_ai_platform.config import Settings
from football_data_ai_platform.fetch_attempts import FetchAttemptStore
from football_data_ai_platform.ingestion.activities import TransfermarktActivities
from football_data_ai_platform.ingestion.models import SquadIngestInput
from football_data_ai_platform.raw_responses import RawResponseStore
from football_data_ai_platform.sources.interface import (
    CircuitOpenError,
    SourceInterface,
    SourcePolicy,
)
from football_data_ai_platform.sources.transfermarkt import (
    SOURCE,
    is_upstream_failure,
    recording_transport,
)
from football_data_ai_platform.sources.transfermarkt.squads import fetch_squad
from tests.transfermarkt_stub import CLUB_ID, PLAYER_IDS, SEASON_ID, TransfermarktStub


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _failing(status: int) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status))


async def test_fan_out_of_one_sdk_call_stays_within_the_rate_limit() -> None:
    stub = TransfermarktStub()
    sent_at: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent_at.append(time.monotonic())
        return stub.handle(request)

    rate = 50.0
    source = SourceInterface(
        SOURCE,
        SourcePolicy(requests_per_second=rate),
        backend=lambda: httpx.MockTransport(handle),
    )

    # The SDK sends up to 10 requests concurrently; the limiter must still space them.
    squad = await fetch_squad(source.transport(), CLUB_ID, SEASON_ID)

    assert [player.id for player in squad.players] == list(PLAYER_IDS)
    assert len(sent_at) == len(stub.requests) > 5
    gaps = [later - earlier for earlier, later in itertools.pairwise(sent_at)]
    # asyncio may wake a sleeper up to one clock tick early.
    assert min(gaps) >= 1 / rate - 0.002


async def test_rate_limit_is_shared_by_every_transport_of_a_source() -> None:
    sent_at: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent_at.append(time.monotonic())
        return httpx.Response(200)

    rate = 50.0
    source = SourceInterface(
        SOURCE, SourcePolicy(requests_per_second=rate), backend=lambda: httpx.MockTransport(handle)
    )

    # Two concurrent activities, each with its own transport.
    async def send(n: int) -> None:
        transport = source.transport()
        for _ in range(n):
            await transport.handle_async_request(httpx.Request("GET", "https://example.com/"))

    await asyncio.gather(send(4), send(4))

    gaps = [later - earlier for earlier, later in itertools.pairwise(sent_at)]
    assert len(sent_at) == 8
    assert min(gaps) >= 1 / rate - 0.002


async def test_repeated_failures_open_the_breaker_and_the_cool_down_closes_it() -> None:
    clock = FakeClock()
    sent: list[httpx.Request] = []
    answer = {"status": 503}

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(answer["status"], json={"success": True, "data": {}})

    source = SourceInterface(
        SOURCE,
        SourcePolicy(requests_per_second=1000, breaker_failures=3, breaker_cooldown_seconds=60),
        is_failure=is_upstream_failure,
        backend=lambda: httpx.MockTransport(handle),
        clock=clock,
    )
    transport = source.transport()
    request = httpx.Request("GET", "https://example.com/")

    for _ in range(3):
        await transport.handle_async_request(request)
    with pytest.raises(CircuitOpenError):
        await transport.handle_async_request(request)
    assert len(sent) == 3  # The open breaker sent nothing.

    clock.now += 61
    answer["status"] = 200
    response = await transport.handle_async_request(request)

    assert response.status_code == 200
    assert len(sent) == 4
    await transport.handle_async_request(request)  # Closed: no error.


async def test_a_failure_after_the_cool_down_opens_the_breaker_again() -> None:
    clock = FakeClock()
    source = SourceInterface(
        SOURCE,
        SourcePolicy(requests_per_second=1000, breaker_failures=2, breaker_cooldown_seconds=60),
        backend=lambda: _failing(503),
        clock=clock,
    )
    transport = source.transport()
    request = httpx.Request("GET", "https://example.com/")
    for _ in range(2):
        await transport.handle_async_request(request)

    clock.now += 61
    await transport.handle_async_request(request)

    with pytest.raises(CircuitOpenError):
        await transport.handle_async_request(request)


async def test_not_found_and_success_do_not_count_against_the_breaker() -> None:
    source = SourceInterface(
        SOURCE,
        SourcePolicy(requests_per_second=1000, breaker_failures=1),
        is_failure=is_upstream_failure,
        backend=lambda: _failing(404),
    )
    transport = source.transport()

    for _ in range(3):
        response = await transport.handle_async_request(httpx.Request("GET", "https://e.com/"))
        assert response.status_code == 404


async def test_transport_errors_count_against_the_breaker() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    source = SourceInterface(
        SOURCE,
        SourcePolicy(requests_per_second=1000, breaker_failures=2),
        backend=lambda: httpx.MockTransport(handle),
    )
    transport = source.transport()
    request = httpx.Request("GET", "https://example.com/")

    for _ in range(2):
        with pytest.raises(httpx.ReadTimeout):
            await transport.handle_async_request(request)
    with pytest.raises(CircuitOpenError):
        await transport.handle_async_request(request)


async def test_open_breaker_error_passes_through_the_sdk_unchanged() -> None:
    source = SourceInterface(
        SOURCE,
        SourcePolicy(requests_per_second=1000, breaker_failures=1),
        backend=lambda: _failing(503),
    )
    source.breaker.record_failure()

    with pytest.raises(CircuitOpenError):
        await fetch_squad(source.transport(), CLUB_ID, SEASON_ID)


async def test_open_breaker_fails_the_activity_until_the_cool_down_ends(
    database_url: str, connection: AsyncConnection[Any]
) -> None:
    source = SourceInterface(
        SOURCE,
        SourcePolicy(requests_per_second=1000, breaker_failures=1, breaker_cooldown_seconds=30),
        backend=TransfermarktStub().transport,
    )
    source.breaker.record_failure()
    activities = TransfermarktActivities(database_url, source)

    with pytest.raises(ApplicationError) as failure:
        await ActivityEnvironment().run(
            activities.ingest_club_squad, SquadIngestInput(club_id=CLUB_ID, season_id=SEASON_ID)
        )

    assert failure.value.type == "CircuitOpenError"
    assert isinstance(failure.value.__cause__, CircuitOpenError)
    assert failure.value.next_retry_delay is not None
    assert 0 < failure.value.next_retry_delay.total_seconds() <= 30


@pytest.fixture
async def proxy() -> AsyncIterator[tuple[str, list[bytes]]]:
    """A forward HTTP proxy on localhost that answers every request itself."""
    received: list[bytes] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.readuntil(b"\r\n\r\n"))
        body = b'{"ok": true}'
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body)
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield f"http://proxy-user:pr0xy-s3cret@127.0.0.1:{port}", received


async def test_requests_go_through_the_configured_proxy_without_leaking_credentials(
    proxy: tuple[str, list[bytes]],
    connection: AsyncConnection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    proxy_url, received = proxy
    policy = SourcePolicy(requests_per_second=1000, proxy=SecretStr(proxy_url))
    transport = recording_transport(
        SourceInterface(SOURCE, policy).transport(),
        RawResponseStore(connection),
        FetchAttemptStore(connection),
    )

    with caplog.at_level(logging.DEBUG):
        response = await transport.handle_async_request(
            httpx.Request("GET", "http://upstream.invalid/club/1")
        )
        await transport.aclose()

    assert response.status_code == 200
    assert len(received) == 1
    # Absolute-form request line: the proxy, not upstream.invalid, received it.
    assert received[0].startswith(b"GET http://upstream.invalid/club/1 HTTP/1.1")
    assert b"Proxy-Authorization: Basic" in received[0]
    cursor = await connection.execute("SELECT url, headers::text FROM raw.raw_responses")
    stored = str(await cursor.fetchall())
    for secret in ("proxy-user", "pr0xy-s3cret"):
        assert secret not in stored
        assert secret not in caplog.text
        assert secret not in repr(policy)


def test_source_policy_reads_from_nested_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSFERMARKT__REQUESTS_PER_SECOND", "0.5")
    monkeypatch.setenv("TRANSFERMARKT__PROXY", "http://u:p@proxy.example:8080")

    policy = Settings(_env_file=None).transfermarkt  # pyright: ignore[reportCallIssue]

    assert policy.requests_per_second == 0.5
    assert policy.proxy is not None
    assert policy.proxy.get_secret_value() == "http://u:p@proxy.example:8080"
    assert policy.proxy.get_secret_value() not in repr(Settings(_env_file=None))  # pyright: ignore[reportCallIssue]


def test_overriding_one_sofascore_setting_keeps_its_other_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOFASCORE__REQUESTS_PER_SECOND", "0.25")

    policy = Settings(_env_file=None).sofascore  # pyright: ignore[reportCallIssue]

    assert policy.requests_per_second == 0.25
    assert policy.breaker_cooldown_seconds == 900
    assert Settings(_env_file=None).transfermarkt.breaker_cooldown_seconds == 60  # pyright: ignore[reportCallIssue]
