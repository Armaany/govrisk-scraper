"""IADBAdapter — stub. Blocked by Cloudflare; Playwright implementation pending.

The IDB procurement notices portal (www.iadb.org and projectprocurement.iadb.org)
is protected by Cloudflare bot detection that cannot be bypassed with requests or
httpx. Every attempt returns HTTP 403 with a JS challenge page.

Pending implementation: use Playwright (already available for DevexAdapter) to
launch a real Chromium browser, solve the Cloudflare challenge, wait for the
procurement table to load via XHR, then extract results.
"""
import logging

from portals.base_adapter import BasePortalAdapter
from utils.audit import AuditLogger

logger = logging.getLogger(__name__)

PORTAL_URL = "https://www.iadb.org/en/how-we-can-work-together/procurement/procurement-projects/procurement-notices"


class IADBAdapter(BasePortalAdapter):
    """Stub adapter for the IDB/IADB Procurement Notices portal.

    Returns empty list and logs the block. Replace body of fetch_opportunities()
    with Playwright-based scraping once that implementation is ready.
    """

    portal_name = "iadb"

    async def fetch_opportunities(self) -> list[dict]:
        msg = (
            "[IADB] Blocked by Cloudflare — Playwright implementation pending. "
            f"Portal URL: {PORTAL_URL}"
        )
        print(msg)
        audit = AuditLogger()
        audit.log(
            event_type="adapter_blocked",
            detail=msg,
        )
        return []
