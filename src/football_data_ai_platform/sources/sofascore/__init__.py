"""Sofascore source: its web JSON API, through browser impersonation (ADR 3, ADR 14)."""

from datetime import timedelta

import httpx
from pydantic import Field

from football_data_ai_platform.fetch_attempts import FetchAttemptStore
from football_data_ai_platform.raw_responses import RawResponseStore
from football_data_ai_platform.sources.interface import SourcePolicy, is_server_failure
from football_data_ai_platform.sources.recording import RecordingTransport, ReplayTransport

SOURCE = "sofascore"


class SofascorePolicy(SourcePolicy):
    """Sofascore's defaults, from the first three-season backfill. A sustained 1 request per
    second drew a 403 bot challenge (`"reason": "challenge"`) within minutes, so: half that
    rate, and a 15-minute circuit-breaker cool-down for other failures. A challenge is
    answered with a Captcha Token (ADR 30); `ChallengeError` means none could be obtained.
    Defaults on a subclass, so overriding one field from the environment keeps the others."""

    requests_per_second: float = Field(default=0.5, gt=0)
    breaker_cooldown_seconds: float = Field(default=900.0, gt=0)
    # The browser that obtains Captcha Tokens (ADR 30). Headed by default: in Linux only a
    # headed browser, under Xvfb, passes the Turnstile. Headless passes on macOS.
    captcha_headless: bool = False


# Temporal does not retry these: asking again cannot change the answer.
NON_RETRYABLE_ERRORS = (
    "MatchNotFound",
    "MatchNotFinished",
    "SeasonNotFound",
    "RawResponseMissingError",
    # No Captcha Token could be obtained, and the Token Provider is backing off (ADR 30):
    # retries would only spend attempts against the challenge.
    "ChallengeError",
)


def recording_transport(
    backend: httpx.AsyncBaseTransport,
    store: RawResponseStore,
    attempts: FetchAttemptStore,
    freshness: timedelta | None = None,
) -> RecordingTransport:
    """A transport that stores every Sofascore response as a Raw Response and every request
    as a Fetch Attempt."""
    return RecordingTransport(backend, store, attempts, SOURCE, is_server_failure, freshness)


def replay_transport(store: RawResponseStore) -> ReplayTransport:
    """A transport that answers from stored Sofascore Raw Responses."""
    return ReplayTransport(store, SOURCE)
