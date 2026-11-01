"""Sofascore's Captcha Token (ADR 30).

Sofascore answers API requests with its bot challenge until a Turnstile captcha is solved and
the solution exchanged for a Captcha Token. The token is bound to the client that exchanges
it, so a browser only produces the Turnstile response and the impersonating transport, which
sends every request, exchanges it.

`CaptchaTransport` sits between the source interface and the impersonating transport. It
sends each request with the current token, or without one when there is none. A challenged
answer makes it obtain a token from the `TokenProvider` and send the request once more. A
request without a token is the plain probe ADR 30 asks for: while Sofascore does not
challenge, no browser is launched.

The exchange and the resend bypass the rate limit and are not recorded: the exchange's answer
is the token itself, and there are at most a few of them an hour. The recorded answer is the
last one.
"""

import asyncio
import base64
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import httpx
from pydantic import SecretStr

from football_data_ai_platform.sources.sofascore.api import challenge_reason

logger = logging.getLogger(__name__)

ORIGIN = "https://www.sofascore.com"
CAPTCHA_PAGE = f"{ORIGIN}/captcha.html?redirectUrl=https%3A%2F%2Fwww.sofascore.com%2F"
EXCHANGE_URL = f"{ORIGIN}/api/v1/token/captcha"
# A token this close to its expiry is dropped; the next request goes without one.
EXPIRY_MARGIN_SECONDS = 300.0
# Waits after the 1st, 2nd, 3rd and later consecutive failed attempts, from a prior soak
# of the same challenge (ADR 30).
BACKOFF_SECONDS = (180.0, 180.0, 360.0, 600.0)
MAX_ATTEMPTS_PER_HOUR = 8
# The whole attempt, solve and exchange: requests waiting on it stay inside the activities'
# one-minute heartbeat timeout.
OBTAIN_TIMEOUT_SECONDS = 40.0
# camoufox passes the Turnstile headed; headless Linux has no WebGL and is caught.
WINDOW = (1920, 1080)

Clock = Callable[[], float]
# Produces the JSON body the captcha page would POST to the exchange: the Turnstile response.
Solve = Callable[[], Awaitable[bytes]]
Exchange = Callable[[bytes], Awaitable[httpx.Response]]


class TokenUnavailableError(Exception):
    """No Captcha Token could be obtained: the solve or the exchange failed."""


@dataclass(frozen=True)
class CaptchaToken:
    value: SecretStr
    expires_at: float  # Unix seconds, from the JWT's `exp`


class TokenProvider:
    """Obtains Captcha Tokens and keeps the current one; shared by every Sofascore request.

    Attempts back off after failures and are capped per hour, so a challenge that solving
    cannot pass costs a few browser launches an hour, not one per request.
    """

    def __init__(self, solve: Solve, clock: Clock = time.time) -> None:
        self._solve = solve
        self._clock = clock
        self._lock = asyncio.Lock()
        self._token: CaptchaToken | None = None
        self._attempts: deque[float] = deque()
        self._failures = 0
        self._retry_at = float("-inf")

    def current(self) -> SecretStr | None:
        """The token to send, or None when there is none or it is about to expire."""
        if self._token is not None and self._clock() >= (
            self._token.expires_at - EXPIRY_MARGIN_SECONDS
        ):
            self._token = None
        return self._token.value if self._token is not None else None

    async def renew(self, rejected: SecretStr | None, exchange: Exchange) -> SecretStr | None:
        """A new token in place of `rejected`, or None while attempts are backing off.

        Requests challenged together share one attempt: a request that waited on the lock
        gets the token the first one obtained.
        """
        async with self._lock:
            if self._token is not None and self._token.value != rejected:
                return self._token.value
            self._token = None
            now = self._clock()
            while self._attempts and self._attempts[0] <= now - 3600:
                self._attempts.popleft()
            if now < self._retry_at or len(self._attempts) >= MAX_ATTEMPTS_PER_HOUR:
                return None
            self._attempts.append(now)
            try:
                token = await asyncio.wait_for(self._obtain(exchange), OBTAIN_TIMEOUT_SECONDS)
            except Exception as error:  # a browser can fail in many ways; all mean "no token"
                self._failures += 1
                wait = BACKOFF_SECONDS[min(self._failures, len(BACKOFF_SECONDS)) - 1]
                self._retry_at = now + wait
                logger.warning(
                    "No Sofascore Captcha Token (%s: %s); next attempt in %.0fs",
                    type(error).__name__,
                    error,
                    wait,
                )
                return None
            self._failures = 0
            self._token = token
            logger.info(
                "Obtained a Sofascore Captcha Token, valid for %.0f min",
                (token.expires_at - now) / 60,
            )
            return token.value

    async def _obtain(self, exchange: Exchange) -> CaptchaToken:
        body = await self._solve()
        response = await exchange(body)
        try:
            if response.status_code != 200:
                raise TokenUnavailableError(f"the exchange answered {response.status_code}")
            value = response.json()["token"]
        finally:
            await response.aclose()
        return CaptchaToken(SecretStr(value), _expiry(value))


class CaptchaTransport(httpx.AsyncBaseTransport):
    """Send through `backend` with the current Captcha Token; on a challenge, renew it and
    send once more.

    `backend` is shared by every client of the worker and outlives each one, so closing this
    transport leaves it open. One session keeps whatever Sofascore pairs with the token (its
    `sofa_captcha` cookie, say) for every request. Requests waiting on a renewal resend
    together when the token arrives, one per concurrent activity, past the rate limit.
    """

    def __init__(self, backend: httpx.AsyncBaseTransport, provider: TokenProvider) -> None:
        self._backend = backend
        self._provider = provider

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        token = self._provider.current()
        response = await self._send(request, token)
        if challenge_reason(response) is None:
            return response
        renewed = await self._provider.renew(token, self._exchange)
        if renewed is None:
            return response
        await response.aclose()
        return await self._send(request, renewed)

    async def aclose(self) -> None:
        pass  # the shared backend stays open

    async def _send(self, request: httpx.Request, token: SecretStr | None) -> httpx.Response:
        if token is None:
            request.headers.pop("x-captcha", None)
        else:
            request.headers["x-captcha"] = token.get_secret_value()
        response = await self._backend.handle_async_request(request)
        await response.aread()
        return response

    async def _exchange(self, body: bytes) -> httpx.Response:
        request = httpx.Request(
            "POST",
            EXCHANGE_URL,
            content=body,
            headers={"content-type": "application/json", "origin": ORIGIN, "referer": CAPTCHA_PAGE},
        )
        response = await self._backend.handle_async_request(request)
        await response.aread()
        return response


def camoufox_solver(headless: bool, proxy: SecretStr | None = None) -> Solve:
    """A `Solve` that loads the captcha page in camoufox and captures the Turnstile response
    the page would exchange. The exchange itself is aborted, so the response stays unspent.

    A fresh profile each time: with a `sofa_captcha` cookie the page would redirect without
    posting a new response.
    """

    async def solve() -> bytes:
        # Imported here: tests and replays never launch a browser.
        from camoufox.async_api import AsyncCamoufox
        from playwright.async_api import Route

        body: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

        async def capture(route: Route) -> None:
            if route.request.method == "POST" and not body.done():
                body.set_result(route.request.post_data_buffer or b"")
                await route.abort()
            else:
                await route.continue_()

        # geoip: timezone and locale follow the exit IP. A UTC browser on a Spanish IP gets
        # the interactive checkbox instead of passing.
        options: dict[str, object] = {"headless": headless, "window": WINDOW, "geoip": True}
        if proxy is not None:
            options["proxy"] = _playwright_proxy(proxy.get_secret_value())
        async with AsyncCamoufox(**options) as browser:
            page = await browser.new_page()
            await page.route("**/api/v1/token/captcha", capture)
            await page.goto(CAPTCHA_PAGE)
            return await body

    return solve


def _expiry(token: str) -> float:
    """The JWT's `exp`. Read, not verified: Sofascore verifies it."""
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return float(claims["exp"])


def _playwright_proxy(url: str) -> dict[str, str]:
    """Playwright takes the proxy's credentials apart from its address, decoded, as
    `curl_cffi` reads them from the same URL."""
    parts = urlsplit(url)
    proxy = {"server": f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}"}
    if parts.username:
        proxy["username"] = unquote(parts.username)
        proxy["password"] = unquote(parts.password or "")
    return proxy
