"""USAIDAdapter — queries grants.gov for US government international development grants.

USAID as an independent agency was effectively shut down in 2025 (83% of programs
terminated, agency closing September 2026). Its procurement pages return 404.

This adapter uses grants.gov — the US government's central grants portal — which
lists all active US government grant opportunities including State Department and
remaining USAID-administered programs. No API key required.

API: POST https://apply07.grants.gov/grantsws/rest/opportunities/search/
Verified accessible and returning 300+ results for governance/anti-corruption keywords.
"""
import logging
import unicodedata
from datetime import date
from typing import Optional

import requests
from dateutil import parser as date_parser

from engine.keyword_filter import KeywordFilter
from portals.base_adapter import BasePortalAdapter

logger = logging.getLogger(__name__)

API_URL = "https://apply07.grants.gov/grantsws/rest/opportunities/search/"
GRANTS_BASE = "https://www.grants.gov/search-results-detail/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# Keywords to search grants.gov — broad enough to surface all GovRisk sectors
SEARCH_KEYWORDS = (
    "anti-corruption governance transparency justice trafficking "
    "AML integrity illicit laundering rule of law"
)

# LATAM country name fragments — used to post-filter by agency/title
_LATAM_TERMS = {
    "mexico", "colombia", "peru", "brazil", "ecuador", "bolivia",
    "guatemala", "honduras", "el salvador", "nicaragua", "costa rica",
    "panama", "dominican", "haiti", "jamaica", "trinidad", "guyana",
    "venezuela", "cuba", "belize", "argentina", "chile", "uruguay",
    "paraguay", "suriname", "latin america", "latam", "caribbean",
    "central america", "south america",
}


def _normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _parse_deadline(raw: Optional[str]) -> Optional[str]:
    """Parse grants.gov date format MM/DD/YYYY to YYYY-MM-DD."""
    if not raw:
        return None
    try:
        return date_parser.parse(raw).date().isoformat()
    except Exception:
        return None


def _is_latam(title: str, agency: str) -> bool:
    """Return True if the title or agency name references a LATAM country."""
    combined = _normalize(f"{title} {agency}")
    return any(term in combined for term in _LATAM_TERMS)


class USAIDAdapter(BasePortalAdapter):
    """Adapter for US government international development grants via grants.gov.

    Searches grants.gov for governance/anti-corruption/justice keywords,
    post-filters to LATAM-relevant opportunities, applies KeywordFilter,
    and returns up to config.max_results records.
    """

    portal_name = "usaid"

    def is_available(self) -> bool:
        """Return True if the grants.gov API responds successfully."""
        try:
            r = requests.post(
                API_URL,
                json={"keyword": "governance", "oppStatuses": "posted", "rows": 1},
                timeout=10,
                headers=HEADERS,
            )
            return r.status_code == 200
        except Exception as exc:
            self._log_error(exc, detail="availability check failed")
            return False

    async def fetch_opportunities(self) -> list[dict]:
        """Fetch US government grants matching GovRisk sectors, filtered to LATAM."""
        if not getattr(self.config, "usaid_enabled", True):
            print("[USAID/Grants] Adapter disabled — skipping")
            return []

        keyword_filter = KeywordFilter(self.config)

        payload = {
            "keyword": SEARCH_KEYWORDS,
            "oppStatuses": "posted",
            "rows": 100,
            "startRecordNum": 0,
        }
        print(f"[USAID/Grants] Querying grants.gov: keyword='{SEARCH_KEYWORDS[:60]}...'")

        try:
            r = requests.post(API_URL, json=payload, timeout=15, headers=HEADERS)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            self._log_error(exc, detail="grants.gov query failed")
            print(f"[USAID/Grants] ERROR: {exc}")
            return []

        hits = data.get("oppHits", [])
        total = data.get("hitCount", "?")
        print(f"[USAID/Grants] API returned {len(hits)} records (total available: {total})")

        # Post-filter to LATAM-relevant opportunities
        latam_hits = [h for h in hits if _is_latam(h.get("title", ""), h.get("agency", ""))]
        print(f"[USAID/Grants] LATAM-relevant records: {len(latam_hits)}")

        # Map to Opportunity_Dict
        mapped = [self._map_record(h) for h in latam_hits]

        # Filter out expired deadlines
        today = date.today()
        active, expired_count = [], 0
        for opp in mapped:
            dl = opp.get("deadline")
            if dl:
                try:
                    if date.fromisoformat(dl) < today:
                        expired_count += 1
                        continue
                except ValueError:
                    pass
            active.append(opp)
        if expired_count:
            print(f"[USAID/Grants] Skipped {expired_count} expired records")

        # Apply keyword filter
        filtered = [opp for opp in active if keyword_filter.passes_filter(opp)]
        print(f"[USAID/Grants] Passed keyword filter: {len(filtered)}")

        if filtered:
            print(f"[USAID/Grants] Sample match: {filtered[0].get('opportunity_title', '')[:70]}")

        return filtered[: self.config.max_results]

    def _map_record(self, hit: dict) -> dict:
        """Map a grants.gov oppHit to the standard Opportunity_Dict schema."""
        grant_id = str(hit.get("id", ""))
        number = hit.get("number", "")
        opportunity_id = f"usaid-{number}" if number else f"usaid-{grant_id}"

        link = f"{GRANTS_BASE}{grant_id}" if grant_id else ""

        title = hit.get("title", "")
        agency = hit.get("agency", "USAID / US Government")

        # Extract country from agency name e.g. "U.S. Mission to Honduras" → "Honduras"
        country_region = self._extract_country(agency, title)

        return {
            "opportunity_id": opportunity_id,
            "devex_opportunity_id": opportunity_id,
            "opportunity_title": title,
            "funder_organisation": agency,
            "country_region": country_region,
            "deadline": _parse_deadline(hit.get("closeDate")),
            "contract_value": None,
            "opportunity_link": link,
            "description_snippet": title,  # no description in list view
            "source_portal": "usaid",
            "portal_source": "USAID / Grants.gov",
            "notice_type": hit.get("docType", ""),
            "matched_keywords": [],
        }

    def _extract_country(self, agency: str, title: str) -> str:
        """Extract a country name from the agency string or title for geo-filtering.

        Examples:
          "U.S. Mission to Honduras"  → "Honduras"
          "U.S. Embassy Buenos Aires" → "Argentina"
          Falls back to "Global" if no match found.
        """
        combined = f"{agency} {title}".lower()
        # Check each LATAM term against the combined text and return the first match
        # Use the canonical capitalised form from the LATAM_TERMS set
        for term in sorted(_LATAM_TERMS, key=len, reverse=True):  # longest first
            if term in combined:
                return term.title()
        return "Global"
