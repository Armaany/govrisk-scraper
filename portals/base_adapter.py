"""Abstract base class for all portal adapters."""
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

from config import Config

logger = logging.getLogger(__name__)


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
        """Log an HTTP status error (4xx/5xx) from an httpx request."""
        logger.error(
            "[%s] HTTP error %s for URL %s: %s",
            self.portal_name,
            exc.response.status_code,
            exc.request.url,
            exc,
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
