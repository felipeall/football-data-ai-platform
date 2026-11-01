"""Fetch Attempts: one row per upstream request a source interface handles (ADR 4).

Raw Responses only record requests that got an answer. Fetch Attempts also record timeouts,
transport errors, circuit-breaker rejections and answers from a fresh stored Raw Response, so
the per-source success rates show breakage instead of hiding it.
"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from psycopg import AsyncConnection


class FetchOutcome(StrEnum):
    ANSWERED = "answered"  # the source answered, with `status_code`
    TIMEOUT = "timeout"
    TRANSPORT_ERROR = "transport_error"
    CIRCUIT_OPEN = "circuit_open"  # the open circuit breaker rejected it; nothing was sent
    REUSED = "reused"  # a fresh stored Raw Response answered; nothing was sent


@dataclass(frozen=True)
class FetchAttempt:
    """One upstream request and how it ended."""

    source: str
    method: str
    url: str  # credentials redacted, as for Raw Responses
    outcome: FetchOutcome
    status_code: int | None  # of the answer, fetched or reused
    # Whether the source's failure test rejected the answer; None when nothing answered.
    failed: bool | None
    attempted_at: datetime


@dataclass(frozen=True)
class SuccessRate:
    """One source's Fetch Attempts on one day (UTC), by outcome; a row of
    `raw.source_success_rates`."""

    source: str
    day: date
    attempts: int
    success: int
    not_found: int
    blocked: int  # 403/429, or another non-5xx answer the source's failure test rejected
    other_client_error: int
    server_error: int
    timeout_or_transport_error: int
    circuit_open: int
    reused: int
    # Successes and 404s over the attempts that reached the source; None if none did.
    success_rate: float | None


class FetchAttemptStore:
    """Fetch Attempts in Postgres (`raw.fetch_attempts`). Rows are only ever inserted.

    Give it an autocommit connection, so each Fetch Attempt is durable once `save` returns.
    """

    def __init__(self, connection: AsyncConnection[Any]) -> None:
        self._connection = connection

    async def save(self, attempt: FetchAttempt) -> None:
        """Insert one Fetch Attempt."""
        await self._connection.execute(
            """
            INSERT INTO raw.fetch_attempts
                (source, method, url, outcome, status_code, failed, attempted_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                attempt.source,
                attempt.method,
                attempt.url,
                attempt.outcome.value,
                attempt.status_code,
                attempt.failed,
                attempt.attempted_at,
            ),
        )

    async def success_rates(self, first_day: date, last_day: date) -> list[SuccessRate]:
        """Per-source, per-day success rates from `first_day` to `last_day`, both included."""
        cursor = await self._connection.execute(
            """
            SELECT source, day, attempts, success, not_found, blocked, other_client_error,
                   server_error, timeout_or_transport_error, circuit_open, reused, success_rate
            FROM raw.source_success_rates
            WHERE day BETWEEN %s AND %s
            ORDER BY source, day
            """,
            (first_day, last_day),
        )
        return [
            SuccessRate(*row[:11], success_rate=None if row[11] is None else float(row[11]))
            for row in await cursor.fetchall()
        ]
