# Keyword filtering for sector and geography inclusion checks.
import unicodedata
from config import Config


def _normalize(text: str) -> str:
    """Lowercase and strip accents for accent-insensitive comparison.

    'anticorrupción' → 'anticorrupcion', 'Lavado' → 'lavado'
    """
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


class KeywordFilter:
    """Applies sector and geography filters to parsed opportunity dictionaries."""

    def __init__(self, config: Config):
        """Store config and pre-normalize keyword/country lists."""
        self.config = config
        self.sector_keywords = [_normalize(kw) for kw in (config.sector_keywords or [])]
        self.target_countries = [_normalize(c) for c in (config.target_countries or [])]

    def passes_filter(self, parsed: dict) -> bool:
        """Return True only when both sector and geography filters pass.

        Matching is:
        - Case-insensitive
        - Accent-insensitive (anticorrupción matches anticorrupcion)
        - Partial-word (corruption matches anticorrupcion)
        """
        title = _normalize(parsed.get("opportunity_title") or "")
        description = _normalize(parsed.get("description_snippet") or "")
        country_region = _normalize(parsed.get("country_region") or "").strip()

        sector_match = any(
            keyword in title or keyword in description
            for keyword in self.sector_keywords
        )

        if not country_region or country_region == "global":
            geography_match = any(country in description for country in self.target_countries)
        else:
            geography_match = any(country in country_region for country in self.target_countries)

        return sector_match and geography_match

    def get_matched_keywords(self, parsed: dict) -> str:
        """Return comma-separated sector keywords found in title/description."""
        title = _normalize(parsed.get("opportunity_title") or "")
        description = _normalize(parsed.get("description_snippet") or "")
        matched = [
            keyword
            for keyword in self.sector_keywords
            if keyword in title or keyword in description
        ]
        return ",".join(matched)

    def get_matched_countries(self, parsed: dict) -> str:
        """Return comma-separated countries found in country field and description."""
        country_region = _normalize(parsed.get("country_region") or "")
        description = _normalize(parsed.get("description_snippet") or "")
        matched = [
            country
            for country in self.target_countries
            if country in country_region or country in description
        ]
        return ",".join(matched)


def is_duplicate(opportunity_id: str, existing_ids: set) -> bool:
    """Return True when opportunity ID already exists in known IDs."""
    return opportunity_id in existing_ids
