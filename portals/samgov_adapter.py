"""SAM.gov portal adapter — queries the free public REST API."""
import logging
from datetime import datetime, timedelta

import httpx

from config import Config
from portals.base_adapter import BasePortalAdapter

logger = logging.getLogger(__name__)


def _thirty_days_ago() -> str:
    """Return a date string 30 days ago in MM/dd/yyyy format."""
    from datetime import timezone
    date = datetime.now(timezone.utc) - timedelta(days=30)
    return date.strftime("%m/%d/%Y")


class SAMGovAdapter(BasePortalAdapter):
    """Adapter for the SAM.gov v2 opportunities REST API."""

    portal_name = "samgov"
    BASE_URL = "https://api.sam.gov/opportunities/v2/search"

    async def fetch_opportunities(self) -> list[dict]:
        if not self.config.samgov_enabled:
            return []

        params = {
            "api_key": self.config.samgov_api_key,
            "q": " ".join(self.config.sector_keywords),
            "limit": self.config.max_results,
            "postedFrom": _thirty_days_ago(),
        }

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(self.BASE_URL, params=params)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                self._log_http_error(exc)
                return []

        raw_items = response.json().get("opportunitiesData", [])
        latam_items = [item for item in raw_items if self._is_latam_relevant(item)]
        return [self._map_result(item) for item in latam_items]

    def _is_latam_relevant(self, item: dict) -> bool:
        """Return True when the opportunity matches a configured target country.

        Checks placeOfPerformance.country.name, placeOfPerformance.state.name,
        and description using case-insensitive substring matching.
        Returns True if target_countries is empty/None (no filter configured).
        """
        target = [c.lower() for c in (self.config.target_countries or [])]
        if not target:
            return True

        pop = item.get("placeOfPerformance", {})
        country_name = (pop.get("country", {}).get("name") or "").lower()
        state_name = (pop.get("state", {}).get("name") or "").lower()
        description = (item.get("description") or "").lower()

        for country in target:
            if country in country_name or country in state_name or country in description:
                return True
        return False

    def _map_result(self, item: dict) -> dict:
        notice_id = item.get("noticeId", "")
        pop = item.get("placeOfPerformance", {})
        country_name = (pop.get("country", {}).get("name") or "")
        return {
            "opportunity_id": f"samgov-{notice_id}",
            "opportunity_title": item.get("title"),
            "funder_organisation": item.get("organizationName"),
            "country_region": country_name,
            "deadline": item.get("responseDeadLine"),
            "contract_value": item.get("award", {}).get("amount"),
            "opportunity_link": f"https://sam.gov/opp/{notice_id}/view",
            "description_snippet": (item.get("description") or "")[:500],
            "source_portal": "samgov",
            "matched_keywords": [],
        }
