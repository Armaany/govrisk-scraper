# Devex search URL construction and paginated opportunity URL discovery.
import re
from urllib.parse import urlencode

from playwright.async_api import Page

from config import Config


class DevexSearch:
    """Handles Devex search query generation and result pagination."""

    def __init__(self, config: Config, page: Page):
        """Store runtime config and authenticated Playwright page."""
        self.config = config
        self.page = page
        self.base_url = "https://www.devex.com/jobs/search"

    def build_search_url(self, page_number: int = 1) -> str:
        """Build a complete Devex search URL from configured keywords and countries."""
        terms = [*self.config.sector_keywords, *self.config.target_countries]
        query_text = " ".join(term.strip() for term in terms if term.strip())
        params = {
            "q": query_text,
            "status": "open",
            "sorting": "date_descending",
            "page": page_number,
        }
        return f"{self.base_url}?{urlencode(params)}"

    async def collect_opportunity_urls(self) -> list[str]:
        """Iterate search pages, collect unique opportunity URLs, and stop at max_results."""
        collected: list[str] = []
        seen: set[str] = set()
        page_number = 1

        while len(collected) < self.config.max_results:
            search_url = self.build_search_url(page_number=page_number)
            await self.page.goto(search_url, wait_until="domcontentloaded")
            await self.page.wait_for_load_state("networkidle")

            link_selectors = [
                'a[href*="/jobs/"]',
                'a[href*="/funding/"]',
                'a[href*="/opportunities/"]',
            ]
            urls_on_page: list[str] = []
            for selector in link_selectors:
                urls_on_page.extend(await self.page.eval_on_selector_all(selector, "els => els.map(e => e.href)"))

            for url in urls_on_page:
                if not url or "/login" in url:
                    continue
                if url not in seen:
                    seen.add(url)
                    collected.append(url)
                    if len(collected) >= self.config.max_results:
                        break

            if len(collected) >= self.config.max_results:
                break

            next_selector = (
                'a[rel="next"]:not([aria-disabled="true"]), '
                'button[aria-label*="Next"]:not([disabled]), '
                'a:has-text("Next"):not([aria-disabled="true"])'
            )
            next_count = await self.page.locator(next_selector).count()
            if next_count == 0:
                break

            page_number += 1

        print(f"Total opportunities found: {len(collected)}")
        return collected

    async def get_total_results_count(self) -> int:
        """Parse and return total results count from search page UI, or 0 if unavailable."""
        selectors = [
            '[data-testid="search-results-count"]',
            ".search-results-count",
            "text=/[0-9,]+\\s+results?/i",
        ]
        for selector in selectors:
            locator = self.page.locator(selector).first
            if await locator.count() == 0:
                continue
            text = await locator.inner_text()
            match = re.search(r"([0-9][0-9,]*)", text)
            if match:
                return int(match.group(1).replace(",", ""))
        return 0
