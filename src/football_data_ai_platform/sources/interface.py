"""The common source interface: what keeps ingestion polite and resilient (ADR 4, ADR 14).

Each source only says what to fetch. Its `SourceInterface` owns the rate limit, the circuit
breaker and the optional proxy, and supplies the `httpx` transport every upstream request goes
through. Retries with backoff are Temporal activity retry policies, set by each workflow.

One `SourceInterface` per source per worker process: every activity of that source shares its
rate limit and circuit breaker.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta

import httpx
from pydantic import BaseModel, Field, SecretStr

logger = logging.getLogger(__name__)

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class SourcePolicy(BaseModel):
    """Per-source settings: rate limit, circuit breaker, Raw Response freshness and a proxy."""

    requests_per_second: float = Field(default=1.0, gt=0)
    breaker_failures: int = Field(default=5, ge=1)
    breaker_cooldown_seconds: float = Field(default=60.0, gt=0)
    # A stored Raw Response younger than this answers its request instead of the source; 0 = off.
    freshness_hours: float = Field(default=24.0, ge=0)
    # May carry credentials: a SecretStr never shows them in reprs or logs.
    proxy: SecretStr | None = None


class CircuitOpenError(Exception):
    """The source's circuit breaker is open: no request was sent.

    The SDKs let errors from our transport through unchanged, so this reaches the activity.
    """

    def __init__(self, source: str, retry_after: float) -> None:
        super().__init__(f"Circuit breaker for {source} is open for another {retry_after:.1f}s")
        self.source = source
        self.retry_after = retry_after


class RateLimiter:
    """Spaces requests at least `1 / requests_per_second` apart, across all concurrent callers."""

    def __init__(
        self,
        requests_per_second: float,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._interval = 1 / requests_per_second
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._next_at = float("-inf")

    async def acquire(self) -> None:
        """Wait for this request's slot."""
        async with self._lock:
            wait = self._next_at - self._clock()
            if wait > 0:
                await self._sleep(wait)
            # Measured after the wait, so a late wake-up never shortens the next gap.
            self._next_at = self._clock() + self._interval


class CircuitBreaker:
    """Opens after `failures` consecutive failures and rejects requests for `cooldown` seconds.

    After the cool-down, requests go through again: a success closes the circuit, a failure
    opens it for another cool-down.
    """

    def __init__(
        self, source: str, failures: int, cooldown: float, clock: Clock = time.monotonic
    ) -> None:
        self._source = source
        self._threshold = failures
        self._cooldown = cooldown
        self._clock = clock
        self._failures = 0
        self._open_until: float | None = None

    def check(self) -> None:
        """Raise `CircuitOpenError` while the circuit is open."""
        if self._open_until is None:
            return
        remaining = self._open_until - self._clock()
        if remaining > 0:
            raise CircuitOpenError(self._source, remaining)

    def record_success(self) -> None:
        if self._open_until is not None:
            logger.info("Circuit breaker for %s closed", self._source)
        self._failures = 0
        self._open_until = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._open_until = self._clock() + self._cooldown
            logger.warning(
                "Circuit breaker for %s opened after %d consecutive failures; cooling down %.0fs",
                self._source,
                self._failures,
                self._cooldown,
            )


def is_server_failure(response: httpx.Response) -> bool:
    """Default failure test: the source refused the request or failed to answer it."""
    return response.status_code in (403, 429) or response.status_code >= 500


class GuardedTransport(httpx.AsyncBaseTransport):
    """Send through `backend` within the rate limit, while the circuit breaker is closed."""

    def __init__(
        self,
        backend: httpx.AsyncBaseTransport,
        limiter: RateLimiter,
        breaker: CircuitBreaker,
        is_failure: Callable[[httpx.Response], bool],
    ) -> None:
        self._backend = backend
        self._limiter = limiter
        self._breaker = breaker
        self._is_failure = is_failure

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await self._limiter.acquire()
        self._breaker.check()
        try:
            response = await self._backend.handle_async_request(request)
            # Read here, so a timeout mid-body counts as a failure and `is_failure` sees the body.
            try:
                await response.aread()
            finally:
                await response.aclose()
        except httpx.HTTPError:
            self._breaker.record_failure()
            raise
        if self._is_failure(response):
            self._breaker.record_failure()
        else:
            self._breaker.record_success()
        return response

    async def aclose(self) -> None:
        await self._backend.aclose()


def http_backend(policy: SourcePolicy) -> httpx.AsyncBaseTransport:
    """A plain `httpx` transport, through the policy's proxy when one is set."""
    proxy = policy.proxy.get_secret_value() if policy.proxy is not None else None
    return httpx.AsyncHTTPTransport(proxy=proxy)


class SourceInterface:
    """One source's rate limit, circuit breaker and network access, shared by its activities."""

    def __init__(
        self,
        source: str,
        policy: SourcePolicy,
        *,
        is_failure: Callable[[httpx.Response], bool] = is_server_failure,
        backend: Callable[[], httpx.AsyncBaseTransport] | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        self.source = source
        self._policy = policy
        self._is_failure = is_failure
        self._backend = backend or (lambda: http_backend(policy))
        self.limiter = RateLimiter(policy.requests_per_second, clock=clock)
        self.breaker = CircuitBreaker(
            source, policy.breaker_failures, policy.breaker_cooldown_seconds, clock=clock
        )

    @property
    def freshness(self) -> timedelta:
        return timedelta(hours=self._policy.freshness_hours)

    def transport(self) -> httpx.AsyncBaseTransport:
        """A transport for one client's lifetime; closing it closes only its own backend."""
        return GuardedTransport(self._backend(), self.limiter, self.breaker, self._is_failure)
