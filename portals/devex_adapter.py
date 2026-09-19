"""DevexAdapter — wraps DevexAuth, DevexSearch, and DevexParser."""
import logging

from portals.base_adapter import (
    CATEGORY_AUTHENTICATION,
    CATEGORY_CONNECTION,
    OP_AUTHENTICATION,
    OP_LISTING_FETCH,
    BasePortalAdapter,
    PortalFetchError,
    _safe_cause_label,
)
from portals.devex_auth import AuthenticationError, DevexAuth
from engine.parser import DevexParser
from engine.search import DevexSearch

logger = logging.getLogger(__name__)

# Controlled remediation phrase. This is the ONLY Devex-specific remediation
# currently present; it is a fixed literal that never embeds the configured
# email or password values.
_DEVEX_AUTH_REMEDIATION = "check DEVEX_EMAIL and DEVEX_PASSWORD"


class DevexAdapter(BasePortalAdapter):
    """Portal adapter for Devex using Playwright-based auth, search, and parsing."""

    portal_name = "devex"

    async def fetch_opportunities(self) -> list[dict]:
        """Fetch and return normalized opportunity dicts from Devex.

        Whole-adapter failures raise a credential-safe ``PortalFetchError``:
        - authentication failures (``AuthenticationError``) → category
          ``authentication`` with the fixed remediation phrase;
        - listing/search failures → category ``connection`` labelled by the
          cause's class name only.

        Per-URL parse failures are logged and skipped (partial results are
        intentionally supported). Alerting and error counting are owned by the
        orchestrator, so this adapter no longer sends its own notification.
        Playwright resources are always closed in ``finally``.
        """
        auth = DevexAuth(self.config)
        try:
            try:
                page = await auth.load_session()
            except AuthenticationError as exc:
                self._log_auth_error(exc)
                raise PortalFetchError(
                    "Devex",
                    OP_AUTHENTICATION,
                    CATEGORY_AUTHENTICATION,
                    attempts=1,
                    reason=_DEVEX_AUTH_REMEDIATION,
                ) from exc

            search = DevexSearch(self.config, page)
            parser = DevexParser(self.config, page)

            try:
                urls = await search.collect_opportunity_urls()
            except Exception as exc:
                self._log_error(exc, detail="listing/search fetch failed")
                raise PortalFetchError(
                    "Devex",
                    OP_LISTING_FETCH,
                    CATEGORY_CONNECTION,
                    attempts=1,
                    reason=_safe_cause_label(exc),
                ) from exc

            results: list[dict] = []
            for url in urls:
                try:
                    parsed = await parser.parse_opportunity(url)
                    parsed["opportunity_id"] = parsed.pop("devex_opportunity_id", "devex-unknown")
                    parsed["source_portal"] = "devex"
                    parsed.setdefault("matched_keywords", [])
                    results.append(parsed)
                except Exception as exc:
                    logger.warning("[devex] Failed to parse %s: %s", url, exc)

            return results

        finally:
            await auth.close()
