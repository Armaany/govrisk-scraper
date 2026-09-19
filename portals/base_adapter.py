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

    def _log_error(self, exc: Exception, detail: str = "") -> None:
        """Log a generic error using the standard logging module."""
        msg = f"[{self.portal_name}] Error"
        if detail:
            msg += f" — {detail}"
        msg += f": {exc}"
        logger.error(msg, exc_info=exc)

    def _log_http_error(self, exc: "httpx.HTTPStatusError") -> None:
        """Log an HTTP status error (4xx/5xx) from an httpx request.

        The request URL is sanitized to strip any query string (which may
        contain credentials such as ``api_key=...``), and the raw exception —
        whose ``str()`` embeds the full URL — is NOT logged. Authorization
        headers are never logged.
        """
        try:
            status = exc.response.status_code
        except Exception:
            status = "?"
        safe_url = "?"
        try:
            safe_url = _sanitize_url(exc.request.url)
        except Exception:
            pass
        logger.error(
            "[%s] HTTP error %s for URL %s",
            self.portal_name,
            status,
            safe_url,
        )

    def _log_parse_error(self, exc: Exception) -> None:
        """Log a response parse failure (e.g. invalid JSON or unexpected schema)."""
        logger.error(
            "[%s] Parse error: %s",
            self.portal_name,
            exc,
            exc_info=exc,
        )

    def _log_auth_error(self, exc: Exception) -> None:
        """Log an authentication failure."""
        logger.error(
            "[%s] Authentication error: %s",
            self.portal_name,
            exc,
            exc_info=exc,
        )
