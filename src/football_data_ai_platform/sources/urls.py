"""URL redaction, applied before a URL is logged or stored."""

import httpx

REDACTED = "REDACTED"
SENSITIVE_QUERY_KEYS = frozenset(
    {"access_token", "api_key", "apikey", "key", "password", "secret", "signature", "token"}
)


def redact_url(url: str | httpx.URL) -> str:
    """Return `url` without user info, and with sensitive query values replaced."""
    parsed = httpx.URL(url).copy_with(username=None, password=None)
    params = [
        (key, REDACTED if key.lower() in SENSITIVE_QUERY_KEYS else value)
        for key, value in parsed.params.multi_items()
    ]
    if params:
        parsed = parsed.copy_with(params=params)
    return str(parsed)
