"""The Captcha Token path (ADR 30): a fake browser and a fake Sofascore, no network."""

import asyncio
import base64
import json
import logging

import httpx
import pytest

from football_data_ai_platform.sources.sofascore.api import (
    ChallengeError,
    client,
    raise_for_failure,
)
from football_data_ai_platform.sources.sofascore.captcha import (
    BACKOFF_SECONDS,
    EXCHANGE_URL,
    EXPIRY_MARGIN_SECONDS,
    MAX_ATTEMPTS_PER_HOUR,
    OBTAIN_TIMEOUT_SECONDS,
    CaptchaTransport,
    TokenProvider,
    _playwright_proxy,  # pyright: ignore[reportPrivateUsage]
)

EVENT = "/event/1001"
TURNSTILE = json.dumps({"response": "turnstile-response", "provider": "turnstile"}).encode()


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _jwt(exp: float, n: int) -> str:
    def part(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'typ': 'JWT'})}.{part({'exp': exp, 'ip': '1.2.3.4', 'f': f'{n:06x}'})}.sig"


class Sofascore:
    """Challenges every request without a valid token; exchanges Turnstile responses for
    tokens valid for an hour. `lifted` answers without a token."""

    def __init__(self, clock: Clock, lifted: bool = False) -> None:
        self.clock = clock
        self.lifted = lifted
        self.issued: list[str] = []
        self.rejected: set[str] = set()
        self.api_requests: list[httpx.Request] = []
        self.exchange_status = 200
        # The token each API request carried when it was sent; requests are resent mutated.
        self.sent: list[str | None] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == EXCHANGE_URL:
            assert request.content == TURNSTILE
            if self.exchange_status != 200:
                return httpx.Response(self.exchange_status, json={"error": {"code": 400}})
            token = _jwt(self.clock.now + 3600, len(self.issued))
            self.issued.append(token)
            return httpx.Response(200, json={"token": token})
        self.api_requests.append(request)
        token = request.headers.get("x-captcha")
        self.sent.append(token)
        if self.lifted or (token in self.issued and token not in self.rejected):
            return httpx.Response(200, json={"event": {"id": 1001}})
        reason = "jwt:invalid" if token else "challenge"
        return httpx.Response(403, json={"error": {"code": 403, "reason": reason}})


class Browser:
    def __init__(self, fails: bool = False) -> None:
        self.fails = fails
        self.launches = 0
        self.gate: asyncio.Event | None = None  # when set, a solve waits for it

    async def solve(self) -> bytes:
        self.launches += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fails:
            raise TimeoutError("the Turnstile did not pass")
        return TURNSTILE


def _transport(sofascore: Sofascore, provider: TokenProvider) -> CaptchaTransport:
    return CaptchaTransport(httpx.MockTransport(sofascore.handle), provider)


async def _get(transport: CaptchaTransport) -> httpx.Response:
    async with client(transport) as api:
        return await api.get(EVENT)


async def test_without_a_challenge_no_browser_is_launched() -> None:
    clock, browser = Clock(), Browser()
    sofascore = Sofascore(clock, lifted=True)
    transport = _transport(sofascore, TokenProvider(browser.solve, clock))

    assert (await _get(transport)).status_code == 200
    assert browser.launches == 0
    assert sofascore.sent == [None]


async def test_a_challenge_obtains_a_token_and_resends_with_it() -> None:
    clock, browser = Clock(), Browser()
    sofascore = Sofascore(clock)
    transport = _transport(sofascore, TokenProvider(browser.solve, clock))

    first = await _get(transport)
    second = await _get(transport)

    assert (first.status_code, second.status_code) == (200, 200)
    assert browser.launches == 1  # the second request reuses the token
    assert len(sofascore.issued) == 1
    assert sofascore.sent == [None, sofascore.issued[0], sofascore.issued[0]]


async def test_a_token_near_expiry_is_dropped_and_the_next_request_goes_plain() -> None:
    clock, browser = Clock(), Browser()
    sofascore = Sofascore(clock)
    transport = _transport(sofascore, TokenProvider(browser.solve, clock))
    await _get(transport)

    clock.now += 3600 - EXPIRY_MARGIN_SECONDS
    sofascore.lifted = True  # the challenge lifted meanwhile
    assert (await _get(transport)).status_code == 200

    assert sofascore.sent[-1] is None
    assert browser.launches == 1


async def test_a_rejected_token_is_renewed() -> None:
    clock, browser = Clock(), Browser()
    sofascore = Sofascore(clock)
    transport = _transport(sofascore, TokenProvider(browser.solve, clock))
    await _get(transport)

    sofascore.rejected.add(sofascore.issued[0])  # `jwt:…`, as when Sofascore revokes it
    assert (await _get(transport)).status_code == 200

    assert browser.launches == 2
    assert sofascore.sent[-1] == sofascore.issued[1]


async def test_failed_attempts_back_off_and_the_challenge_reaches_the_caller() -> None:
    clock, browser = Clock(), Browser(fails=True)
    transport = _transport(Sofascore(clock), TokenProvider(browser.solve, clock))

    with pytest.raises(ChallengeError):
        raise_for_failure(await _get(transport))
    assert browser.launches == 1

    clock.now += BACKOFF_SECONDS[0] - 1
    await _get(transport)
    assert browser.launches == 1  # still backing off: no launch

    clock.now += 1
    await _get(transport)
    assert browser.launches == 2

    clock.now += BACKOFF_SECONDS[1]
    browser.fails = False
    assert (await _get(transport)).status_code == 200
    assert browser.launches == 3


async def test_attempts_are_capped_per_hour() -> None:
    clock, browser = Clock(), Browser()
    sofascore = Sofascore(clock)
    provider = TokenProvider(browser.solve, clock)
    transport = _transport(sofascore, provider)

    for _ in range(MAX_ATTEMPTS_PER_HOUR + 2):
        await _get(transport)
        sofascore.rejected.update(sofascore.issued)  # every token is rejected straight away
    assert browser.launches == MAX_ATTEMPTS_PER_HOUR

    clock.now += 3600
    await _get(transport)
    assert browser.launches == MAX_ATTEMPTS_PER_HOUR + 1


async def test_requests_challenged_together_share_one_attempt() -> None:
    clock, browser = Clock(), Browser()
    browser.gate = asyncio.Event()
    sofascore = Sofascore(clock)
    transport = _transport(sofascore, TokenProvider(browser.solve, clock))

    requests = asyncio.gather(*(_get(transport) for _ in range(3)))
    await asyncio.sleep(0)  # all three are challenged and wait on the one attempt
    browser.gate.set()
    responses = await requests

    assert [r.status_code for r in responses] == [200, 200, 200]
    assert browser.launches == 1
    assert len(sofascore.issued) == 1


async def test_a_rejected_token_that_cannot_be_renewed_is_a_challenge_error() -> None:
    clock, browser = Clock(), Browser()
    sofascore = Sofascore(clock)
    transport = _transport(sofascore, TokenProvider(browser.solve, clock))
    await _get(transport)

    sofascore.rejected.add(sofascore.issued[0])
    browser.fails = True
    response = await _get(transport)

    assert response.json()["error"]["reason"] == "jwt:invalid"
    with pytest.raises(ChallengeError):
        raise_for_failure(response)


async def test_a_failed_exchange_backs_off_like_a_failed_solve() -> None:
    clock, browser = Clock(), Browser()
    sofascore = Sofascore(clock)
    sofascore.exchange_status = 400
    transport = _transport(sofascore, TokenProvider(browser.solve, clock))

    assert (await _get(transport)).status_code == 403
    await _get(transport)

    assert browser.launches == 1  # backing off after the failed exchange
    assert sofascore.issued == []


async def test_an_attempt_that_hangs_times_out_and_backs_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "football_data_ai_platform.sources.sofascore.captcha.OBTAIN_TIMEOUT_SECONDS", 0.05
    )
    clock, browser = Clock(), Browser()
    browser.gate = asyncio.Event()  # never set: the solve hangs
    transport = _transport(Sofascore(clock), TokenProvider(browser.solve, clock))

    assert (await _get(transport)).status_code == 403
    await _get(transport)

    assert browser.launches == 1
    assert OBTAIN_TIMEOUT_SECONDS < 60  # inside the activities' heartbeat timeout


async def test_closing_a_client_leaves_the_shared_session_open() -> None:
    clock, closed = Clock(), list[bool]()

    class Backend(httpx.MockTransport):
        async def aclose(self) -> None:
            closed.append(True)

    sofascore = Sofascore(clock, lifted=True)
    transport = CaptchaTransport(Backend(sofascore.handle), TokenProvider(Browser().solve, clock))
    await _get(transport)  # `client` closes its transport on exit

    assert closed == []


async def test_the_token_never_reaches_logs_or_reprs(caplog: pytest.LogCaptureFixture) -> None:
    clock, browser = Clock(), Browser()
    sofascore = Sofascore(clock)
    provider = TokenProvider(browser.solve, clock)
    caplog.set_level(logging.DEBUG)

    await _get(_transport(sofascore, provider))

    token = sofascore.issued[0]
    assert token not in caplog.text
    assert token not in repr(provider.current())
    assert token not in repr(provider._token)  # pyright: ignore[reportPrivateUsage]


def test_proxy_credentials_go_apart_from_the_address() -> None:
    assert _playwright_proxy("http://user:secret@proxy.example:8080") == {
        "server": "http://proxy.example:8080",
        "username": "user",
        "password": "secret",
    }
    assert _playwright_proxy("socks5://proxy.example:1080") == {
        "server": "socks5://proxy.example:1080"
    }
    assert _playwright_proxy("http://proxy.example") == {"server": "http://proxy.example"}
    assert _playwright_proxy("http://[2001:db8::1]:8080") == {"server": "http://[2001:db8::1]:8080"}
    assert _playwright_proxy("http://us%40er:p%40ss@proxy.example:8080") == {
        "server": "http://proxy.example:8080",
        "username": "us@er",
        "password": "p@ss",
    }
