"""Raw Responses: untouched source payloads, stored before any parsing (ADR 4, ADR 28)."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb


@dataclass(frozen=True)
class RawResponse:
    """One source response to one request, as received."""

    source: str
    method: str
    url: str
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    captured_at: datetime


class RawResponseStore:
    """Raw Responses in Postgres (`raw.raw_responses`). Rows are only ever inserted.

    Give it an autocommit connection, so each Raw Response is durable once `save` returns.
    Concurrent calls are safe: psycopg serialises operations on one connection.
    """

    def __init__(self, connection: AsyncConnection[Any]) -> None:
        self._connection = connection

    async def save(self, response: RawResponse) -> None:
        """Insert one Raw Response."""
        await self._connection.execute(
            """
            INSERT INTO raw.raw_responses
                (source, method, url, status_code, headers, body, body_json, captured_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                response.source,
                response.method,
                response.url,
                response.status_code,
                Jsonb([list(header) for header in response.headers]),
                response.body,
                _json_or_none(response.body),
                response.captured_at,
            ),
        )

    async def latest(
        self, source: str, method: str, url: str, captured_since: datetime | None = None
    ) -> RawResponse | None:
        """The most recent Raw Response for a request, or None if none is stored.

        With `captured_since`, only a Raw Response captured at or after that time counts.
        """
        cursor = await self._connection.execute(
            """
            SELECT status_code, headers, body, captured_at
            FROM raw.raw_responses
            WHERE source = %s AND method = %s AND url = %s
              AND (%s::timestamptz IS NULL OR captured_at >= %s)
            ORDER BY captured_at DESC, id DESC
            LIMIT 1
            """,
            (source, method, url, captured_since, captured_since),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        status_code, headers, body, captured_at = row
        return RawResponse(
            source=source,
            method=method,
            url=url,
            status_code=status_code,
            headers=tuple((key, value) for key, value in headers),
            body=bytes(body),
            captured_at=captured_at,
        )


def _json_or_none(body: bytes) -> Jsonb | None:
    """A queryable JSONB copy of the body; `body` itself stays byte-for-byte untouched."""
    try:
        return Jsonb(json.loads(body))
    except ValueError:
        return None
