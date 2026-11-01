"""Transfermarkt source, through the `transfermarkt-api` SDK in-process (ADR 28)."""

from datetime import timedelta

import httpx

from football_data_ai_platform.fetch_attempts import FetchAttemptStore
from football_data_ai_platform.raw_responses import RawResponseStore
from football_data_ai_platform.sources.recording import RecordingTransport, ReplayTransport

SOURCE = "transfermarkt"

# Temporal does not retry these (ADR 28): asking again cannot change the answer.
NON_RETRYABLE_ERRORS = ("NotFound", "EndpointUnavailable", "RawResponseMissingError")

# The SDK reads this response extension as the fetch time it reports in `updatedAt`.
FETCHED_AT = "transfermarkt_fetched_at"


def is_upstream_failure(response: httpx.Response) -> bool:
    """Responses that count against the circuit breaker: the ones the SDK raises
    `UpstreamBlocked` or `UpstreamError` for, except a 404 (`NotFound`)."""
    if response.headers.get("x-amzn-waf-action"):
        return True
    if response.status_code == 202 and not response.content:
        return True
    return response.status_code in (403, 429) or response.status_code >= 500


def recording_transport(
    backend: httpx.AsyncBaseTransport,
    store: RawResponseStore,
    attempts: FetchAttemptStore,
    freshness: timedelta | None = None,
) -> RecordingTransport:
    """A transport the SDK fetches through, storing every response as a Transfermarkt Raw
    Response and every request as a Fetch Attempt."""
    return RecordingTransport(
        backend,
        store,
        attempts,
        SOURCE,
        is_upstream_failure,
        freshness,
        captured_at_extension=FETCHED_AT,
    )


def replay_transport(store: RawResponseStore) -> ReplayTransport:
    """A transport the SDK re-parses through, answering from stored Transfermarkt Raw
    Responses."""
    return ReplayTransport(store, SOURCE, captured_at_extension=FETCHED_AT)
