"""An `httpx` transport that sends through `curl_cffi` with browser impersonation (ADR 14).

Sources that answer plain `httpx` with 403 use this as their source interface backend, so the
rate limit, circuit breaker, proxy and Raw Response recording apply to them unchanged.
`curl_cffi` errors become their `httpx` counterparts, so the circuit breaker, Fetch Attempts
and Temporal retries see them as they see any other timeout or transport error.
"""

from typing import cast

import httpx
from curl_cffi.requests import AsyncSession, BrowserTypeLiteral, Response
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
from curl_cffi.requests.exceptions import ConnectTimeout, RequestException, Timeout
from curl_cffi.requests.session import HttpMethod

from football_data_ai_platform.sources.interface import SourcePolicy

BROWSER: BrowserTypeLiteral = "chrome"
DEFAULT_TIMEOUT_SECONDS = 30.0
# Headers `httpx.AsyncClient` adds by default. Forwarded, they would replace the browser's
# own and give the impersonation away; curl sets them itself.
CLIENT_DEFAULT_HEADERS = frozenset(
    {"host", "user-agent", "accept", "accept-encoding", "connection", "content-length"}
)
# curl has already decoded the body; httpx must not decode it again.
DECODED_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})


class ImpersonatingTransport(httpx.AsyncBaseTransport):
    """Send each request as a browser would, through `curl_cffi`."""

    def __init__(self, proxy: str | None = None, browser: BrowserTypeLiteral = BROWSER) -> None:
        self._session: AsyncSession[Response] = AsyncSession(impersonate=browser, proxy=proxy)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        headers = [
            (key, value)
            for key, value in request.headers.multi_items()
            if key.lower() not in CLIENT_DEFAULT_HEADERS
        ]
        try:
            response = await self._session.request(
                cast(HttpMethod, request.method),
                str(request.url),
                headers=headers,
                content=await request.aread() or None,
                timeout=_timeout(request),
                allow_redirects=False,
            )
        except ConnectTimeout as error:
            raise httpx.ConnectTimeout(str(error), request=request) from error
        except Timeout as error:
            raise httpx.ReadTimeout(str(error), request=request) from error
        except CurlConnectionError as error:
            raise httpx.ConnectError(str(error), request=request) from error
        except RequestException as error:
            raise httpx.TransportError(str(error), request=request) from error
        return httpx.Response(
            response.status_code,
            headers=[
                (key, value)
                for key, value in response.headers.multi_items()
                if value is not None and key.lower() not in DECODED_HEADERS
            ],
            content=response.content,
            request=request,
        )

    async def aclose(self) -> None:
        await self._session.close()


def impersonating_backend(policy: SourcePolicy) -> httpx.AsyncBaseTransport:
    """An impersonating transport, through the policy's proxy when one is set."""
    proxy = policy.proxy.get_secret_value() if policy.proxy is not None else None
    return ImpersonatingTransport(proxy=proxy)


def _timeout(request: httpx.Request) -> float:
    """The read timeout the `httpx` client set on the request, or a default."""
    timeouts = request.extensions.get("timeout", {})
    return timeouts.get("read") or DEFAULT_TIMEOUT_SECONDS
