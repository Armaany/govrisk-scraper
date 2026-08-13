# BeautifulSoup parser for extracting core opportunity fields from Devex pages.
import re
from typing import Any, Optional

from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from playwright.async_api import Page

from config import Config
from models import OpportunityRecord


class DevexParser:
    """Parses Devex opportunity pages into structured dictionaries."""

    def __init__(self, config: Config, page: Page):
        """Store runtime config and Playwright page used for parsing."""
        self.config = config
        self.page = page

    async def parse_opportunity(self, url: str) -> dict:
        """Navigate to an opportunity page and extract configured core fields."""
        fields: dict[str, Any] = {
            "devex_opportunity_id": self.extract_devex_id(url),
            "opportunity_title": None,
            "funder_organisation": None,
            "country_region": None,
            "deadline": None,
            "contract_value": None,
            "opportunity_link": url,
            "description_snippet": None,
        }

        try:
            await self.page.goto(url, wait_until="domcontentloaded")
            await self.page.wait_for_load_state("networkidle")
            html = await self.page.content()
            soup = BeautifulSoup(html, "lxml")

            fields["opportunity_title"] = self._extract_title(soup)
            fields["funder_organisation"] = self._extract_labeled_value(
                soup, labels=["Posted by", "Organization", "Client"]
            )
            fields["country_region"] = self._extract_labeled_value(
                soup, labels=["Locations", "Countries", "Location"]
            )
            raw_deadline = self._extract_labeled_value(soup, labels=["Deadline", "Closing date"])
            fields["deadline"] = self._normalize_deadline(raw_deadline)
            fields["contract_value"] = self._extract_labeled_value(
                soup, labels=["Budget", "Estimated value", "Contract value"]
            )
            fields["description_snippet"] = self._extract_description_snippet(soup)
        except Exception:
            # Missing fields or page-level parse issues should not break extraction.
            pass

        print(f"Parsed opportunity: {fields.get('opportunity_title')} | {url}")
        return fields

    def extract_devex_id(self, url: str) -> str:
        """Extract numeric project ID from URL and format as devex-XXXXXX."""
        match = re.search(r"/projects/(\d+)", url)
        if not match:
            return "devex-unknown"
        return f"devex-{match.group(1)}"

    def _extract_title(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract title from H1 first, then fallback to og:title meta."""
        try:
            h1 = soup.select_one("h1")
            if h1:
                title = h1.get_text(" ", strip=True)
                if title:
                    return title
            og_title = soup.select_one('meta[property="og:title"]')
            if og_title and og_title.get("content"):
                return str(og_title["content"]).strip() or None
        except Exception:
            return None
        return None

    def _extract_labeled_value(self, soup: BeautifulSoup, labels: list[str]) -> Optional[str]:
        """Find value near an element that contains any provided label text."""
        normalized_labels = [label.lower() for label in labels]

        try:
            for text_node in soup.find_all(string=True):
                text = text_node.strip()
                if not text:
                    continue
                lowered = text.lower()
                if not any(label in lowered for label in normalized_labels):
                    continue

                container = text_node.parent
                if container is None:
                    continue

                combined = container.get_text(" ", strip=True)
                for label in labels:
                    combined = re.sub(
                        rf"(?i)\b{re.escape(label)}\b\s*[:\-]?\s*",
                        "",
                        combined,
                    )
                combined = combined.strip()
                if combined and combined.lower() not in normalized_labels:
                    return combined

                sibling = container.find_next_sibling()
                if sibling:
                    sibling_text = sibling.get_text(" ", strip=True)
                    if sibling_text:
                        return sibling_text
        except Exception:
            return None
        return None

    def _normalize_deadline(self, raw_deadline: Optional[str]) -> Optional[str]:
        """Convert raw deadline text into YYYY-MM-DD, or None if invalid."""
        if not raw_deadline:
            return None
        try:
            parsed = date_parser.parse(raw_deadline, fuzzy=True)
            return parsed.date().isoformat()
        except Exception:
            return None

    def _extract_description_snippet(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract first 500 chars of listing body text with HTML removed."""
        try:
            candidates = [
                "main",
                "article",
                '[data-testid="job-description"]',
                ".job-description",
                ".opportunity-description",
                ".description",
            ]
            for selector in candidates:
                node = soup.select_one(selector)
                if node:
                    text = node.get_text(" ", strip=True)
                    if text:
                        return text[:500]

            body = soup.select_one("body")
            if body:
                text = body.get_text(" ", strip=True)
                if text:
                    return text[:500]
        except Exception:
            return None
        return None
