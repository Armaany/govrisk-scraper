"""OECDAdapter — stub. Requires authentication on the Prospeum SRM platform.

The OECD procurement notices appear on dashboard.prospeum.com/#/login/onboarding/oecd,
which is an Angular SPA backed by api.prospeum.com. Every API endpoint returns
HTTP 401 ("Anmeldedaten fehlen" — credentials missing) without a valid auth token.

The URL https://dashboard.prospeum.com/#/login/onboarding/oecd is the OECD supplier
onboarding/login flow — not a public procurement listing. The table visible in the
screenshot is only rendered after an authenticated session is established.

Pending implementation options:
  1. Obtain OECD supplier credentials and authenticate via POST /api/auth/login/ to
     get a Bearer token, then call GET /api/project/?team_type=procurement.
  2. If credentials are available, implement token-based REST adapter here.
"""
import logging

from portals.base_adapter import BasePortalAdapter
from utils.audit import AuditLogger

logger = logging.getLogger(__name__)

PORTAL_URL = "https://dashboard.prospeum.com/#/login/onboarding/oecd"
API_BASE = "https://api.prospeum.com"


class OECDAdapter(BasePortalAdapter):
    """Stub adapter for the OECD procurement notices via Prospeum dashboard.

    Returns empty list and logs the auth block. Replace body of fetch_opportunities()
    with token-authenticated REST calls once credentials are available.
    """

    portal_name = "oecd"

    async def fetch_opportunities(self) -> list[dict]:
        msg = (
            "[OECD] Authentication required — Prospeum API returns 401 without credentials. "
            f"Portal: {PORTAL_URL} | API: {API_BASE}. "
            "Pending implementation: supply OECD Prospeum credentials to enable this adapter."
        )
        print(msg)
        audit = AuditLogger()
        audit.log(
            event_type="adapter_blocked",
            detail=msg,
        )
        return []
