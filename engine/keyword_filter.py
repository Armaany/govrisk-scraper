# Keyword filtering for sector and geography inclusion checks.
import unicodedata
from typing import Optional

from config import Config

# Key used by adapters to provide explicit full-text for keyword matching.
# This field is transient — it must NOT be serialized or written to Sheets.
MATCHING_TEXT_KEY = "_matching_text"


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

    @staticmethod
    def get_matching_text(parsed: dict) -> tuple[str, str]:
        """Return (title, searchable_text) for keyword matching.

        If the opportunity carries an explicit _matching_text field (set by adapters
        that fetch full detail-page content), that value is used as the searchable text.
        Otherwise, falls back to description_snippet (legacy behavior for non-UNDP adapters).

        Both passes_filter() and get_matched_keywords() MUST use this single helper
        so they always see the same text.
        """
        title = _normalize(parsed.get("opportunity_title") or "")
        explicit = parsed.get(MATCHING_TEXT_KEY)
        if explicit:
            searchable = _normalize(explicit)
        else:
            searchable = _normalize(parsed.get("description_snippet") or "")
        return title, searchable

    def passes_filter(self, parsed: dict) -> bool:
        """Return True only when both sector and geography filters pass.

        Matching is:
        - Case-insensitive
        - Accent-insensitive (anticorrupción matches anticorrupcion)
        - Partial-word (corruption matches anticorrupcion)
        """
        title, searchable = self.get_matching_text(parsed)
        country_region = _normalize(parsed.get("country_region") or "").strip()

        sector_match = any(
            keyword in title or keyword in searchable
            for keyword in self.sector_keywords
        )

        if not country_region or country_region == "global":
            geography_match = any(
                country in searchable for country in self.target_countries
            )
        else:
            geography_match = any(
                country in country_region for country in self.target_countries
            )

        return sector_match and geography_match

    def get_matched_keywords(self, parsed: dict) -> str:
        """Return comma-separated sector keywords found in title/searchable text."""
        title, searchable = self.get_matching_text(parsed)
        matched = [
            keyword
            for keyword in self.sector_keywords
            if keyword in title or keyword in searchable
        ]
        return ",".join(matched)

    def get_matched_countries(self, parsed: dict) -> str:
        """Return comma-separated countries found in country field and searchable text."""
        country_region = _normalize(parsed.get("country_region") or "")
        _, searchable = self.get_matching_text(parsed)
        matched = [
            country
            for country in self.target_countries
            if country in country_region or country in searchable
        ]
        return ",".join(matched)


def is_duplicate(opportunity_id: str, existing_ids: set) -> bool:
    """Return True when opportunity ID already exists in known IDs."""
    return opportunity_id in existing_ids
