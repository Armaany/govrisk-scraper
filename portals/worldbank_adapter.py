"""WorldBankAdapter — queries the World Bank public procurement notices API.

No API key required. Endpoint:
  https://search.worldbank.org/api/v2/procnotices

Strategy (keyword-first, verified against live API):
  - Single global keyword query sorted by deadline desc → most recent active notices
  - Post-filter to LATAM countries using COUNTRY_MAP
  - Expire-filter to drop past deadlines
  - KeywordFilter for final sector relevance check

API notes (verified against live response):
  - procnotices is a LIST of record dicts
  - deadline field: submission_deadline_date (ISO 8601 e.g. "2025-07-25T00:00:00Z")
  - funder field:   contact_organization
  - description:    bid_description
  - notice type:    notice_type
  - link:           contact_web_url (may be empty; fallback to WB project page)
"""
import hashlib
import logging
import unicodedata
from datetime import date
from typing import Optional
from urllib.parse import quote

import requests
from dateutil import parser as date_parser

from engine.keyword_filter import KeywordFilter
from portals.base_adapter import BasePortalAdapter

logger = logging.getLogger(__name__)

API_BASE = "https://search.worldbank.org/api/v2/procnotices"
WB_PROJECT_BASE = "https://projects.worldbank.org/en/projects-operations/project-detail"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Keyword query terms for the global WB search — broad enough to catch all GovRisk sectors
WB_QUERY_TERMS = (
    "anti-corruption OR AML OR transparency OR justice OR trafficking "
    "OR governance OR integrity OR illicit OR corruption OR laundering"
)

# Set of normalised WB country names that are in LATAM — used for post-filtering
# Maps config.target_countries values → exact World Bank API country name spellings.
# None values are skipped (regional/non-country terms that the API doesn't support).
COUNTRY_MAP: dict[str, str | None] = {
    "Mexico":             "Mexico",
    "Colombia":           "Colombia",
    "Peru":               "Peru",
    "Brazil":             "Brazil",
    "Ecuador":            "Ecuador",
    "Bolivia":            "Bolivia",
    "Guatemala":          "Guatemala",
    "Honduras":           "Honduras",
    "El Salvador":        "El Salvador",
    "Nicaragua":          "Nicaragua",
    "Costa Rica":         "Costa Rica",
    "Panama":             "Panama",
    "Dominican Republic": "Dominican Republic",
    "Haiti":              "Haiti",
    "Jamaica":            "Jamaica",
    "Trinidad":           "Trinidad and Tobago",
    "Guyana":             "Guyana",
    "Venezuela":          "Venezuela, Republica Bolivariana de",
    "Cuba":               "Cuba",
    "Belize":             "Belize",
    "Argentina":          "Argentina",
    "Chile":              "Chile",
    "Uruguay":            "Uruguay",
    "Paraguay":           "Paraguay",
    "Suriname":           "Suriname",
    # Regional/non-country terms — not valid WB country names
    "LATAM":              None,
    "Latin America":      None,
    "Caribbean":          None,
    "Regional":           None,
}

# Normalised set of WB country names that count as LATAM for post-filtering
_LATAM_WB_NAMES: set[str] = {
    v.lower() for v in COUNTRY_MAP.values() if v is not None
}


def _normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _parse_deadline(raw: Optional[str]) -> Optional[str]:
    """Parse ISO or freeform deadline string to YYYY-MM-DD, or None."""
    if not raw:
        return None
    try:
        return date_parser.parse(raw).date().isoformat()
    except Exception:
        return None


def _format_contract_value(raw) -> Optional[str]:
    """Format a numeric or string contract value as a USD string."""
    if raw is None or raw == "":
        return None
    try:
        amount = float(str(raw).replace(",", ""))
        return f"USD {amount:,.0f}"
    except (ValueError, TypeError):
        return str(raw) if raw else None


class WorldBankAdapter(BasePortalAdapter):
    """Adapter for the World Bank public procurement notices API.

    Single keyword-based global query → LATAM post-filter → expiry filter → KeywordFilter.
    No per-country loops, no 500 errors from flaky country endpoints.
    """

    portal_name = "worldbank"

    def is_available(self) -> bool:
        """Return True if the API endpoint responds with HTTP 200."""
        try:
            r = requests.get(
                f"{API_BASE}?format=json&rows=1",
                timeout=10,
                headers=HEADERS,
            )
            return r.status_code == 200
        except Exception as exc:
            self._log_error(exc, detail="availability check failed")
            return False

    async def fetch_opportunities(self) -> list[dict]:
        """Fetch World Bank procurement notices via keyword search, then LATAM-filter."""
        if not getattr(self.config, "worldbank_enabled", True):
            print("[WorldBank] Adapter disabled — skipping")
            return []

        keyword_filter = KeywordFilter(self.config)

        # Single global keyword query — sorted by deadline desc to get most recent first
        url = (
            f"{API_BASE}?format=json&rows=100&os=0&apilang=en"
            f"&qterm={quote(WB_QUERY_TERMS)}"
            f"&srt=submission_deadline_date&ord=desc"
        )
        print(f"[WorldBank] Querying: {url[:120]}...")

        try:
            r = requests.get(url, timeout=15, headers=HEADERS)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            self._log_error(exc, detail="keyword query failed")
            print(f"[WorldBank] ERROR: {exc}")
            return []

        notices = data.get("procnotices", [])
        if isinstance(notices, dict):
            notices = list(notices.values())
        total_available = data.get("total", "?")
        print(f"[WorldBank] API returned {len(notices)} records (total available: {total_available})")

        # Post-filter to LATAM countries
        latam_records = []
        for rec in notices:
            country = _normalize(rec.get("project_ctry_name", ""))
            if any(country == wb for wb in _LATAM_WB_NAMES):
                latam_records.append(rec)
        print(f"[WorldBank] LATAM records: {len(latam_records)}")

        # Map to Opportunity_Dict
        mapped = [self._map_record(r) for r in latam_records]

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
            print(f"[WorldBank] Skipped {expired_count} expired records")
        print(f"[WorldBank] Active records: {len(active)}")

        # Apply keyword filter
        filtered = [opp for opp in active if keyword_filter.passes_filter(opp)]
        print(f"[WorldBank] Passed keyword filter: {len(filtered)}")

        if filtered:
            print(f"[WorldBank] Sample match: {filtered[0].get('opportunity_title', '')[:70]}")

        return filtered[: self.config.max_results]

    def _map_record(self, rec: dict) -> dict:
        """Map a raw World Bank API record to the standard Opportunity_Dict schema."""
        notice_id = rec.get("id", "")
        project_id = rec.get("project_id", "")

        opportunity_id = f"wb-{notice_id}" if notice_id else (
            "wb-" + hashlib.sha256(str(rec).encode()).hexdigest()[:12]
        )

        # Build link: prefer contact_web_url, fall back to WB project page
        link = (rec.get("contact_web_url") or "").strip()
        if not link and project_id:
            link = f"{WB_PROJECT_BASE}/{project_id}"

        # Description: prefer bid_description, fall back to project_name
        description = (rec.get("bid_description") or "").strip()
        if not description:
            description = rec.get("project_name", "")
        description = description[:500]

        return {
            "opportunity_id": opportunity_id,
            "devex_opportunity_id": opportunity_id,
            "opportunity_title": rec.get("project_name", ""),
            "funder_organisation": rec.get("contact_organization") or "World Bank",
            "country_region": rec.get("project_ctry_name", ""),
            "deadline": _parse_deadline(rec.get("submission_deadline_date")),
            "contract_value": _format_contract_value(rec.get("project_totalamt")),
            "opportunity_link": link,
            "description_snippet": description,
            "source_portal": "worldbank",
            "portal_source": "World Bank",
            "notice_type": rec.get("notice_type", ""),
            "matched_keywords": [],
        }
