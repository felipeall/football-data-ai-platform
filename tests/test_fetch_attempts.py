from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from psycopg import AsyncConnection

from football_data_ai_platform.fetch_attempts import (
    FetchAttempt,
    FetchAttemptStore,
    FetchOutcome,
    SuccessRate,
)
from football_data_ai_platform.ingestion.cli import format_success_rates
from football_data_ai_platform.raw_responses import RawResponseStore
from football_data_ai_platform.sources.interface import (
    CircuitOpenError,
    SourceInterface,
    SourcePolicy,
    is_server_failure,
)
from football_data_ai_platform.sources.recording import RecordingTransport
from football_data_ai_platform.sources.transfermarkt import recording_transport
from football_data_ai_platform.sources.transfermarkt.squads import fetch_squad
from tests.transfermarkt_stub import CLUB_ID, SEASON_ID, TransfermarktStub

BASE = "https://fake.example"
STATUSES = {"/ok": 200, "/missing": 404, "/forbidden": 403, "/throttled": 429, "/bad": 400}


def _handle(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/slow":
        raise httpx.ReadTimeout("timed out", request=request)
    if path == "/refused":
        raise httpx.ConnectError("refused", request=request)
    if path == "/down":
        return httpx.Response(503)
    return httpx.Response(STATUSES[path], json={})


async def test_every_request_produces_exactly_one_fetch_attempt(
    connection: AsyncConnection[Any],
) -> None:
    # Six consecutive failures open the breaker: 403, 429, 503, timeout, transport error, 503.
    policy = SourcePolicy(requests_per_second=1000, breaker_failures=6, freshness_hours=1)
    source = SourceInterface("fake", policy, backend=lambda: httpx.MockTransport(_handle))
    attempts = FetchAttemptStore(connection)
    transport = RecordingTransport(
        source.transport(),
        RawResponseStore(connection),
        attempts,
        "fake",
        is_server_failure,
        freshness=source.freshness,
    )

    async def send(path: str) -> None:
        await transport.handle_async_request(httpx.Request("GET", BASE + path))

    for path in ["/ok", "/missing", "/bad", "/forbidden", "/throttled", "/down"]:
        await send(path)
    with pytest.raises(httpx.ReadTimeout):
        await send("/slow?token=s3cret")
    with pytest.raises(httpx.ConnectError):
        await send("/refused")
    await send("/down")  # the sixth failure: the breaker opens
    await send("/ok")  # a fresh stored 200 answers; the breaker is never asked
    with pytest.raises(CircuitOpenError):
        await send("/new")

    cursor = await connection.execute(
        "SELECT source, url, outcome, status_code, failed FROM raw.fetch_attempts ORDER BY id"
    )
    rows = await cursor.fetchall()
    assert [(outcome, status, failed) for _, _, outcome, status, failed in rows] == [
        ("answered", 200, False),
        ("answered", 404, False),
        ("answered", 400, False),
        ("answered", 403, True),
        ("answered", 429, True),
        ("answered", 503, True),
        ("timeout", None, None),
        ("transport_error", None, None),
        ("answered", 503, True),
        ("reused", 200, False),
        ("circuit_open", None, None),
    ]
    assert {row[0] for row in rows} == {"fake"}
    # URLs are redacted as for Raw Responses.
    assert f"{BASE}/slow?token=REDACTED" in [row[1] for row in rows]
    assert "s3cret" not in str(rows)

    today = datetime.now(UTC).date()
    assert await attempts.success_rates(today, today) == [
        SuccessRate(
            source="fake",
            day=today,
            attempts=11,
            success=1,
            not_found=1,
            blocked=2,
            other_client_error=1,
            server_error=2,
            timeout_or_transport_error=2,
            circuit_open=1,
            reused=1,
            # The 200 and the 404 over the nine attempts that reached the source: all but the
            # reuse and the circuit-open rejection.
            success_rate=0.2222,
        )
    ]


async def test_success_rates_are_per_source_and_day_within_the_range(
    connection: AsyncConnection[Any],
) -> None:
    attempts = FetchAttemptStore(connection)

    async def attempt(source: str, day: int, outcome: FetchOutcome, status: int | None) -> None:
        at = datetime(2026, 10, day, 23, 30, tzinfo=UTC)
        failed = None if status is None else status >= 500
        await attempts.save(FetchAttempt(source, "GET", f"{BASE}/x", outcome, status, failed, at))

    await attempt("fake", 1, FetchOutcome.ANSWERED, 200)
    await attempt("fake", 2, FetchOutcome.ANSWERED, 200)
    await attempt("fake", 2, FetchOutcome.ANSWERED, 500)
    await attempt("other", 2, FetchOutcome.CIRCUIT_OPEN, None)
    await attempt("fake", 3, FetchOutcome.ANSWERED, 200)

    rates = await attempts.success_rates(date(2026, 10, 2), date(2026, 10, 2))

    assert [(r.source, r.day, r.attempts, r.success_rate) for r in rates] == [
        ("fake", date(2026, 10, 2), 2, 0.5),
        # Nothing reached the source: no rate rather than zero.
        ("other", date(2026, 10, 2), 1, None),
    ]


async def test_answers_the_source_rejects_count_as_blocked_not_success(
    connection: AsyncConnection[Any],
) -> None:
    responses = {
        "/waf": httpx.Response(200, headers={"x-amzn-waf-action": "challenge"}, text="<html>"),
        "/empty": httpx.Response(202),
        "/ok": httpx.Response(200, json={}),
    }
    backend = httpx.MockTransport(lambda request: responses[request.url.path])
    attempts = FetchAttemptStore(connection)
    transport = recording_transport(backend, RawResponseStore(connection), attempts)

    for path in responses:
        await transport.handle_async_request(httpx.Request("GET", BASE + path))

    today = datetime.now(UTC).date()
    (rate,) = await attempts.success_rates(today, today)
    assert (rate.success, rate.blocked, rate.success_rate) == (1, 2, 0.3333)


async def test_a_transfermarkt_squad_records_one_fetch_attempt_per_upstream_request(
    connection: AsyncConnection[Any],
) -> None:
    stub = TransfermarktStub()
    transport = recording_transport(
        stub.transport(), RawResponseStore(connection), FetchAttemptStore(connection)
    )

    await fetch_squad(transport, CLUB_ID, SEASON_ID)

    cursor = await connection.execute(
        "SELECT count(*), count(*) FILTER (WHERE outcome = 'answered') FROM raw.fetch_attempts"
        " WHERE source = 'transfermarkt'"
    )
    assert await cursor.fetchone() == (len(stub.requests), len(stub.requests))


def test_success_rates_print_as_a_table() -> None:
    row = SuccessRate("transfermarkt", date(2026, 10, 12), 560, 540, 12, 2, 0, 1, 1, 0, 4, 0.9709)

    lines = format_success_rates([row]).splitlines()

    assert lines[0].split()[:3] == ["source", "day", "attempts"]
    assert lines[1].split() == [
        "transfermarkt",
        "2026-10-12",
        "560",
        "540",
        "12",
        "2",
        "0",
        "1",
        "1",
        "0",
        "4",
        "97.1%",
    ]
    assert format_success_rates([]) == "No Fetch Attempts in this range."
