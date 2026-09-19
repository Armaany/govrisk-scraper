"""Shared typed fetch-failure contract for portal adapters.

``PortalFetchError`` is the single typed exception raised by an active portal
adapter when a whole-adapter fetch, authentication, or whole-response parse
fails. The orchestrator (``main.run_scraper``) owns alerting and error
counting: it catches this exception, increments ``errors``, sends exactly one
component-specific alert, and continues with later adapters.

Credential safety is the core design constraint. The user-visible ``str()`` of a
``PortalFetchError`` is constructed ONLY from controlled, safe fields (portal,
operation, category, attempts, optional HTTP status, and a curated reason /
remediation string). It never includes ``str(cause)``/``repr(cause)``, request
URLs (which may carry ``api_key=`` query strings), request/response headers
(which may carry ``Authorization: Bearer ...``), request bodies, or any
credential material. The original exception is preserved through ``raise ...
from exc`` for debugging via ``__cause__`` / traceback, but is deliberately
kept out of the string representation.
"""
from __future__ import annotations

from typing import Optional

# Controlled operation labels.
OP_LISTING_FETCH = "listing_fetch"
OP_AUTHENTICATION = "authentication"
OP_RESPONSE_PARSE = "response_parse"

# Controlled failure categories.
CATEGORY_TIMEOUT = "timeout"
CATEGORY_CONNECTION = "connection"
CATEGORY_HTTP_STATUS = "http_status"
CATEGORY_AUTHENTICATION = "authentication"
CATEGORY_RESPONSE_PARSE = "response_parse"

_VALID_CATEGORIES = frozenset(
    {
        CATEGORY_TIMEOUT,
        CATEGORY_CONNECTION,
        CATEGORY_HTTP_STATUS,
        CATEGORY_AUTHENTICATION,
        CATEGORY_RESPONSE_PARSE,
    }
)


class PortalFetchError(Exception):
    """Typed, credential-safe failure raised by a portal adapter.

    All fields passed here MUST be controlled/safe values. In particular,
    ``reason`` must never be built from a raw exception message, a request URL,
    or any header/body content. Use a short, curated phrase such as an
    exception class name (e.g. ``"ReadTimeout"``) or a remediation hint
    (e.g. ``"check DEVEX_EMAIL and DEVEX_PASSWORD"``).

    Preserve the underlying cause via ``raise PortalFetchError(...) from exc``;
    it remains available on ``__cause__`` but is never rendered by ``str()``.
    """

    def __init__(
        self,
        portal: str,
        operation: str,
        category: str,
        attempts: int,
        *,
        http_status: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> None:
        self.portal = portal
        self.operation = operation
        self.category = category if category in _VALID_CATEGORIES else "unknown"
        self.attempts = int(attempts)
        self.http_status = http_status
        # ``reason`` is expected to be a controlled string. We store it as-is,
        # trusting call sites (never pass raw cause text here).
        self.reason = reason
        super().__init__(self._safe_message())

    def _attempts_phrase(self) -> str:
        n = self.attempts
        unit = "attempt" if n == 1 else "attempts"
        return f"{n} {unit}"

    def _safe_message(self) -> str:
        """Build the user-visible message from controlled fields only."""
        base = (
            f"{self.portal} {self.operation} failed after "
            f"{self._attempts_phrase()}"
        )

        detail: str
        if self.category == CATEGORY_HTTP_STATUS and self.http_status is not None:
            # e.g. "non-retryable HTTP 404" or "HTTP 503".
            detail = (
                f"{self.reason} HTTP {self.http_status}"
                if self.reason
                else f"HTTP {self.http_status}"
            )
        elif self.category == CATEGORY_AUTHENTICATION:
            # Authentication failures render the curated remediation phrase
            # directly, e.g. "check DEVEX_EMAIL and DEVEX_PASSWORD".
            detail = self.reason or self.category
        elif self.reason:
            # e.g. "timeout (ReadTimeout)" or "connection (ConnectError)".
            detail = f"{self.category} ({self.reason})"
        else:
            detail = self.category

        return f"{base}: {detail}"

    def __str__(self) -> str:  # noqa: D401 - simple override
        return self._safe_message()

    def __repr__(self) -> str:
        return (
            f"PortalFetchError(portal={self.portal!r}, operation={self.operation!r}, "
            f"category={self.category!r}, attempts={self.attempts!r}, "
            f"http_status={self.http_status!r})"
        )
