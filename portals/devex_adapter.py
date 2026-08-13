"""DevexAdapter — wraps DevexAuth, DevexSearch, and DevexParser."""
import logging

from config import Config
from engine.parser import DevexParser
from engine.search import DevexSearch
from portals.base_adapter import BasePortalAdapter
from portals.devex_auth import AuthenticationError, DevexAuth
from utils.audit import AuditLogger
from utils.notifier import Notifier

logger = logging.getLogger(__name__)


class DevexAdapter(BasePortalAdapter):
    """Portal adapter for Devex using Playwright-based auth, search, and parsing."""

    portal_name = "devex"

    async def fetch_opportunities(self) -> list[dict]:
        """Fetch and return normalized opportunity dicts from Devex."""
        auth = DevexAuth(self.config)
        try:
            page = await auth.load_session()
            search = DevexSearch(self.config, page)
            parser = DevexParser(self.config, page)
            urls = await search.collect_opportunity_urls()

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

        except AuthenticationError as exc:
            audit = AuditLogger()
            audit.log_error(str(exc))
            notifier = Notifier(self.config)
            notifier.send_error_alert(str(exc), component="devex")
            return []

        finally:
            await auth.close()
