"""Understat source: its site's JSON endpoints, over plain HTTP (ADR 3)."""

from datetime import timedelta

import httpx

from football_data_ai_platform.fetch_attempts import FetchAttemptStore
from football_data_ai_platform.raw_responses import RawResponseStore
from football_data_ai_platform.sources.interface import is_server_failure
from football_data_ai_platform.sources.recording import RecordingTransport, ReplayTransport

SOURCE = "understat"

# Temporal does not retry these: asking again cannot change the answer.
NON_RETRYABLE_ERRORS = ("LeagueSeasonNotFound", "RawResponseMissingError")


def recording_transport(
    backend: httpx.AsyncBaseTransport,
    store: RawResponseStore,
    attempts: FetchAttemptStore,
    freshness: timedelta | None = None,
) -> RecordingTransport:
    """A transport that stores every Understat response as a Raw Response and every request
    as a Fetch Attempt."""
    return RecordingTransport(backend, store, attempts, SOURCE, is_server_failure, freshness)


def replay_transport(store: RawResponseStore) -> ReplayTransport:
    """A transport that answers from stored Understat Raw Responses."""
    return ReplayTransport(store, SOURCE)
