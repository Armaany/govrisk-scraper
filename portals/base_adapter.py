"""Abstract base class for all portal adapters."""
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

from config import Config

# Re-exported so adapters (and tests) can import the shared typed failure
# contract from either ``portals.errors`` or ``portals.base_adapter``.
from portals.errors import (  # noqa: F401
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

logger = logging.getLogger(__name__)


def _safe_cause_label(exc: BaseException) -> str:
    """Return a credential-safe label for an exception cause.

    Uses only the exception's class name (e.g. ``"ReadTimeout"``), never its
    message, so no URL/header/credential content can leak into a
    ``PortalFetchError`` reason.
    """
    return type(exc).__name__


def _sanitize_url(url: object) -> str:
    """Return a URL string with any query string removed.

    Query strings can carry secrets (e.g. ``?api_key=...``); this keeps only
    the scheme/host/path portion for safe logging.
    """
    text = str(url)
    return text.split("?", 1)[0]


def classify_requests_error(portal: str, exc: Exception) -> PortalFetchError:
    """Map a ``requests`` fetch exception onto a credential-safe PortalFetchError.

    Categorises timeout / connection / HTTP-status failures. The reason is only
    ever the exception's class name (via ``_safe_cause_label``); an HTTP status
    code, when known, is attached as a controlled field. Raw messages and URLs
    (which may embed query-string secrets) are never used.
    """
    import requests  # local import to avoid a hard dependency at import time

    if isinstance(exc, requests.exceptions.Timeout):
        return PortalFetchError(
            portal, OP_LISTING_FETCH, CATEGORY_TIMEOUT, attempts=1,
            reason=_safe_cause_label(exc),
        )
    if isinstance(exc, requests.exceptions.HTTPError):
        status = None
        response = getattr(exc, "response", None)
        if response is not None:
            status = getattr(response, "status_code", None)
        return PortalFetchError(
            portal, OP_LISTING_FETCH, CATEGORY_HTTP_STATUS, attempts=1,
            http_status=status,
        )
    if isinstance(exc, requests.exceptions.ConnectionError):
        return PortalFetchError(
            portal, OP_LISTING_FETCH, CATEGORY_CONNECTION, attempts=1,
            reason=_safe_cause_label(exc),
        )
    # Any other requests error (or unexpected error) → connection category.
    return PortalFetchError(
        portal, OP_LISTING_FETCH, CATEGORY_CONNECTION, attempts=1,
        reason=_safe_cause_label(exc),
    )


class BasePortalAdapter(ABC):
    """Abstract base class all portal adapters must implement."""

    def __init__(self, config: Config) -> None:
        self.config = config

    @property
    @abstractmethod
    def portal_name(self) -> str:
        """Human-readable portal identifier, e.g. 'devex', 'samgov', 'perplexity'."""
        ...

    @abstractmethod
    async def fetch_opportunities(self) -> list[dict]:
        """Fetch and return normalized Opportunity_Dict instances from this portal."""
        ...

    # --- Helper logging methods ---
    #
    # CREDENTIAL SAFETY: for expected adapter failures these helpers log ONLY
    # controlled information — portal, an optional detail/operation label, the
    # exception CLASS name (never its message), an optional HTTP status, and a
    # query-string-stripped URL when the exception carries one. They never
    # render the raw exception message, headers, body, credentials, or a
    # secret-bearing traceback (no ``exc_info``). The original cause is
    # preserved only through exception chaining on the raised
    # ``PortalFetchError``, not in these log lines.

    def _safe_http_status(self, exc: Exception):
        """Extract an HTTP status code from an exception's response, or None."""
        response = getattr(exc, "response", None)
        if response is not None:
            status = getattr(response, "status_code", None)
            if status is not None:
                return status
        return None

    def _safe_request_url(self, exc: Exception):
        """Return the sanitized (query-stripped) request URL, or None."""
        request = getattr(exc, "request", None)
        if request is not None:
            url = getattr(request, "url", None)
            if url is not None:
                return _sanitize_url(url)
        return None

    def _log_error(self, exc: Exception, detail: str = "") -> None:
        """Log a generic (expected) adapter failure using controlled fields only.

        Logs the portal, optional detail, and the exception CLASS name — never
        the raw exception message or a traceback, either of which could embed a
        secret-bearing URL/header. When available, a sanitized request URL and
        HTTP status are appended.
        """
        parts = [f"[{self.portal_name}] Error"]
        if detail:
            parts.append(f"— {detail}")
        parts.append(f"({_safe_cause_label(exc)})")
        status = self._safe_http_status(exc)
        if status is not None:
            parts.append(f"status={status}")
        safe_url = self._safe_request_url(exc)
        if safe_url is not None:
            parts.append(f"url={safe_url}")
        logger.error(" ".join(parts))

    def _log_http_error(self, exc: "httpx.HTTPStatusError") -> None:
        """Log an HTTP status error (4xx/5xx) from an httpx request.

        The request URL is sanitized to strip any query string (which may
        contain credentials such as ``api_key=...``), and the raw exception —
        whose ``str()`` embeds the full URL — is NOT logged. Authorization
        headers are never logged.
        """
        status = self._safe_http_status(exc)
        if status is None:
            status = "?"
        safe_url = self._safe_request_url(exc) or "?"
        logger.error(
            "[%s] HTTP error %s for URL %s",
            self.portal_name,
            status,
            safe_url,
        )

    def _log_parse_error(self, exc: Exception) -> None:
        """Log a response parse failure using controlled fields only.

        Logs only the exception CLASS name (e.g. ``JSONDecodeError``); never the
        raw message, response body, or a traceback.
        """
        logger.error(
            "[%s] Parse error (%s)",
            self.portal_name,
            _safe_cause_label(exc),
        )

    def _log_auth_error(self, exc: Exception) -> None:
        """Log an authentication failure using controlled fields only.

        Logs only the exception CLASS name; never the raw message (which may
        embed the configured email/password) or a traceback.
        """
        logger.error(
            "[%s] Authentication error (%s)",
            self.portal_name,
            _safe_cause_label(exc),
        )
