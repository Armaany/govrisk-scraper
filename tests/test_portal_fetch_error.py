"""Unit tests for the shared PortalFetchError typed failure contract.

Covers:
  - The documented safe message shapes.
  - str()/__str__ is built ONLY from controlled fields.
  - The original cause is preserved via chaining but never rendered by str().
  - URL query-string sanitization helper.
"""
import pytest

from portals.errors import (
    CATEGORY_AUTHENTICATION,
    CATEGORY_CONNECTION,
    CATEGORY_HTTP_STATUS,
    CATEGORY_RESPONSE_PARSE,
    CATEGORY_TIMEOUT,
    OP_AUTHENTICATION,
    OP_LISTING_FETCH,
    OP_RESPONSE_PARSE,
    PortalFetchError,
)
from portals.base_adapter import _sanitize_url, _safe_cause_label


# ---------------------------------------------------------------------------
# Documented message shapes
# ---------------------------------------------------------------------------

def test_message_shape_timeout_plural_attempts():
    err = PortalFetchError(
        "UNDP", OP_LISTING_FETCH, CATEGORY_TIMEOUT, attempts=3, reason="ReadTimeout"
    )
    assert str(err) == "UNDP listing_fetch failed after 3 attempts: timeout (ReadTimeout)"


def test_message_shape_non_retryable_http_singular_attempt():
    err = PortalFetchError(
        "UNDP", OP_LISTING_FETCH, CATEGORY_HTTP_STATUS, attempts=1,
        http_status=404, reason="non-retryable",
    )
    assert str(err) == "UNDP listing_fetch failed after 1 attempt: non-retryable HTTP 404"


def test_message_shape_authentication_remediation():
    err = PortalFetchError(
        "Devex", OP_AUTHENTICATION, CATEGORY_AUTHENTICATION, attempts=1,
        reason="check DEVEX_EMAIL and DEVEX_PASSWORD",
    )
    assert str(err) == (
        "Devex authentication failed after 1 attempt: "
        "check DEVEX_EMAIL and DEVEX_PASSWORD"
    )


def test_http_status_without_reason():
    err = PortalFetchError(
        "SAM.gov", OP_LISTING_FETCH, CATEGORY_HTTP_STATUS, attempts=1, http_status=503
    )
    assert str(err) == "SAM.gov listing_fetch failed after 1 attempt: HTTP 503"


def test_connection_category_with_label():
    err = PortalFetchError(
        "World Bank", OP_LISTING_FETCH, CATEGORY_CONNECTION, attempts=1,
        reason="ConnectionError",
    )
    assert str(err) == (
        "World Bank listing_fetch failed after 1 attempt: connection (ConnectionError)"
    )


# ---------------------------------------------------------------------------
# Sanitization: str() built only from controlled fields
# ---------------------------------------------------------------------------

def test_str_never_includes_raw_cause_message():
    """A cause whose message contains a secret must not appear in str()."""
    secret = "SENTINEL_SECRET_TOKEN_0001"
    try:
        raise ValueError(f"boom with api_key={secret} and Authorization: Bearer {secret}")
    except ValueError as cause:
        err = PortalFetchError(
            "SAM.gov", OP_LISTING_FETCH, CATEGORY_HTTP_STATUS, attempts=1,
            http_status=403,
        )
        err.__cause__ = cause  # simulate "raise ... from cause"

    text = str(err)
    assert secret not in text
    assert "api_key" not in text
    assert "Bearer" not in text
    # Controlled fields remain.
    assert "SAM.gov" in text
    assert "HTTP 403" in text
    # Cause is still available for debugging.
    assert isinstance(err.__cause__, ValueError)
    assert secret in str(err.__cause__)


def test_chaining_preserves_cause_but_not_in_str():
    def _raise():
        try:
            raise TimeoutError("read timed out on https://x/y?api_key=SENTINEL_9999")
        except TimeoutError as exc:
            raise PortalFetchError(
                "UNDP", OP_LISTING_FETCH, CATEGORY_TIMEOUT, attempts=3,
                reason=_safe_cause_label(exc),
            ) from exc

    with pytest.raises(PortalFetchError) as excinfo:
        _raise()

    err = excinfo.value
    assert "SENTINEL_9999" not in str(err)
    assert "api_key" not in str(err)
    # Safe label is the class name only.
    assert "TimeoutError" in str(err)
    assert isinstance(err.__cause__, TimeoutError)


def test_repr_is_controlled_fields_only():
    err = PortalFetchError(
        "Perplexity", OP_RESPONSE_PARSE, CATEGORY_RESPONSE_PARSE, attempts=1,
        reason="JSONDecodeError",
    )
    r = repr(err)
    assert "Perplexity" in r
    assert "response_parse" in r
    # No secret material possible here, but ensure attempts/status are present.
    assert "attempts=1" in r


def test_invalid_category_normalized_to_unknown():
    err = PortalFetchError("X", OP_LISTING_FETCH, "not-a-category", attempts=2)
    assert err.category == "unknown"
    assert "unknown" in str(err)


# ---------------------------------------------------------------------------
# URL sanitization helper
# ---------------------------------------------------------------------------

def test_sanitize_url_strips_query_string():
    url = "https://api.sam.gov/opportunities/v2/search?api_key=SENTINEL_SAM_KEY_5522&q=x"
    safe = _sanitize_url(url)
    assert safe == "https://api.sam.gov/opportunities/v2/search"
    assert "api_key" not in safe
    assert "SENTINEL_SAM_KEY_5522" not in safe


def test_sanitize_url_without_query_is_unchanged():
    url = "https://procurement-notices.undp.org/"
    assert _sanitize_url(url) == url


def test_safe_cause_label_is_class_name():
    assert _safe_cause_label(ValueError("secret")) == "ValueError"
    assert _safe_cause_label(TimeoutError("secret")) == "TimeoutError"
