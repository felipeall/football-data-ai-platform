import asyncio
import gzip
import json
import socket
from collections.abc import AsyncIterator

import httpx
import pytest

from football_data_ai_platform.sources.impersonation import ImpersonatingTransport
from football_data_ai_platform.sources.interface import (
    CircuitOpenError,
    SourceInterface,
    SourcePolicy,
)


@pytest.fixture
async def server() -> AsyncIterator[str]:
    """A local HTTP server. `/echo` answers, gzipped, with the request headers it received;
    `/slow` answers after two seconds."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        request_line, *header_lines = head.strip().split("\r\n")
        path = request_line.split()[1]
        if path == "/slow":
            await asyncio.sleep(2)
        headers = dict(line.split(": ", 1) for line in header_lines)
        body = gzip.compress(json.dumps({k.lower(): v for k, v in headers.items()}).encode())
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Encoding: gzip\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield f"http://127.0.0.1:{port}"


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_responses_arrive_decoded_once_with_the_browser_headers(server: str) -> None:
    async with httpx.AsyncClient(transport=ImpersonatingTransport()) as client:
        response = await client.get(f"{server}/echo")

    received = response.json()  # would fail if httpx decoded the gzip body a second time
    assert "content-encoding" not in response.headers
    # The impersonated browser's own headers, not the ones httpx.AsyncClient adds.
    assert "Chrome" in received["user-agent"]
    assert "python-httpx" not in json.dumps(received)


async def test_a_curl_timeout_is_an_httpx_timeout(server: str) -> None:
    async with httpx.AsyncClient(transport=ImpersonatingTransport(), timeout=0.5) as client:
        with pytest.raises(httpx.TimeoutException):
            await client.get(f"{server}/slow")


async def test_a_refused_connection_is_an_httpx_transport_error() -> None:
    async with httpx.AsyncClient(transport=ImpersonatingTransport()) as client:
        with pytest.raises(httpx.TransportError):
            await client.get(f"http://127.0.0.1:{_closed_port()}/")


async def test_curl_failures_count_against_the_circuit_breaker() -> None:
    source = SourceInterface(
        "fake",
        SourcePolicy(requests_per_second=1000, breaker_failures=2),
        backend=ImpersonatingTransport,
    )
    url = f"http://127.0.0.1:{_closed_port()}/"

    async with httpx.AsyncClient(transport=source.transport()) as client:
        for _ in range(2):
            with pytest.raises(httpx.TransportError):
                await client.get(url)
        with pytest.raises(CircuitOpenError):
            await client.get(url)
