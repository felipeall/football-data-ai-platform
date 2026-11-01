from football_data_ai_platform.sources.urls import redact_url


def test_strips_user_info() -> None:
    assert redact_url("https://user:pass@example.com/club/1") == "https://example.com/club/1"


def test_masks_sensitive_query_values_and_keeps_the_rest() -> None:
    redacted = redact_url("https://example.com/players?ids[]=1&ids[]=2&token=abc&API_KEY=xyz")

    assert "abc" not in redacted
    assert "xyz" not in redacted
    assert "ids%5B%5D=1&ids%5B%5D=2" in redacted


def test_leaves_plain_urls_unchanged() -> None:
    url = "https://tmapi.transfermarkt.technology/club/985/squad?season=2025"

    assert redact_url(url) == url
