"""UNDPAdapter — scrapes UNDP Procurement Notices portal.

v2: Bounded concurrency detail-page fetching, heading-based Overview extraction,
full-text keyword matching (not truncated), adapter-level timeout.
"""
import asyncio
import hashlib
import logging
import re
from datetime import date, datetime
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from engine.keyword_filter import KeywordFilter
from portals.base_adapter import BasePortalAdapter

logger = logging.getLogger(__name__)

BASE_URL = "https://procurement-notices.undp.org"

# LATAM region class applied by UNDP to each <a> card
_LATAM_REGION_CLASS = "region_RLA"

# Concurrency settings
_MAX_CONCURRENT_DETAIL_FETCHES = 8
_DETAIL_REQUEST_TIMEOUT = 12  # seconds per detail page
_ADAPTER_LEVEL_TIMEOUT = 120  # seconds — entire adapter run must complete within this

# Display/storage truncation (NOT used for keyword matching)
_DESCRIPTION_DISPLAY_MAX = 1000


def _parse_deadline(raw: Optional[str]) -> Optional[str]:
    """Parse '06-May-26' or '30-Apr-2604:00 AM' into YYYY-MM-DD."""
    if not raw:
        return None
    raw = raw.strip()
    match = re.match(r"(\d{1,2}-[A-Za-z]{3}-\d{2})", raw)
    if not match:
        return None
    try:
        parsed = datetime.strptime(match.group(1), "%d-%b-%y")
        return parsed.date().isoformat()
    except ValueError:
        return None


def _extract_cell(card, label: str) -> Optional[str]:
    """Extract span value from a vacanciesTable__cell matching the given label."""
    label_lower = label.lower().strip()
    for cell in card.find_all("div", class_="vacanciesTable__cell"):
        label_div = cell.find("div", class_="vacanciesTable__cell__label")
        if label_div and label_div.get_text(strip=True).lower() == label_lower:
            span = cell.find("span")
            if span:
                return span.get_text(strip=True) or None
            full_text = cell.get_text(" ", strip=True)
            label_text = label_div.get_text(strip=True)
            return full_text[len(label_text):].strip() or None
    return None


def _extract_overview_from_detail(html: str) -> Optional[str]:
    """Extract the Overview section from a UNDP detail page.

    Strategy (per spec AC 3):
      1. Look for a postContent div whose <h2> heading contains "Overview"
      2. If not found, fall back to the longest postContent block and log
      3. Return the FULL text (no truncation) for keyword matching
    """
    soup = BeautifulSoup(html, "lxml")
    content_divs = soup.find_all("div", class_="postContent")
    if not content_divs:
        return None

    # Primary: heading-based identification
    for div in content_divs:
        h2 = div.find("h2")
        if h2 and "overview" in h2.get_text(strip=True).lower():
            text = div.get_text(" ", strip=True)
            # Strip the heading text itself from the start
            heading_text = h2.get_text(strip=True)
            if text.startswith(heading_text):
                text = text[len(heading_text):].strip()
            return text if text else None

    # Fallback: longest block heuristic
    logger.info("[undp] Overview heading not found — using longest postContent block (fallback)")
    best = max(content_divs, key=lambda d: len(d.get_text()))
    text = best.get_text(" ", strip=True)
    return text if text else None


class UNDPAdapter(BasePortalAdapter):
    """Adapter for UNDP Procurement Notices.

    Fetches the listing page, filters to LATAM, skips expired records,
    then fetches detail pages concurrently (bounded semaphore) to extract
    real Overview descriptions for keyword matching.
    """

    portal_name = "undp"

    def is_available(self) -> bool:
        try:
            import requests
            resp = requests.get(BASE_URL, timeout=10)
            return resp.status_code == 200
        except Exception as exc:
            self._log_error(exc, detail="availability check failed")
            return False

    async def fetch_opportunities(self) -> list[dict]:
        """Fetch UNDP notices with bounded-concurrency detail enrichment."""
        if not getattr(self.config, "undp_enabled", True):
            print("[UNDP] Adapter disabled — skipping")
            return []

        try:
            return await asyncio.wait_for(
                self._run(), timeout=_ADAPTER_LEVEL_TIMEOUT
            )
        except asyncio.TimeoutError:
            print(f"[UNDP] Adapter timed out after {_ADAPTER_LEVEL_TIMEOUT}s")
            logger.error("[undp] Adapter-level timeout reached (%ds)", _ADAPTER_LEVEL_TIMEOUT)
            return []

    async def _run(self) -> list[dict]:
        """Core logic — separated so wait_for can wrap it."""
        url = f"{BASE_URL}/"
        print(f"[UNDP] Requesting listing: {url}")

        # Fetch listing page
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                self._log_error(exc, detail="listing page fetch failed")
                print(f"[UNDP] ERROR: listing fetch failed — {exc}")
                return []

        print(f"[UNDP] Listing size: {len(resp.text)} chars")
        soup = BeautifulSoup(resp.text, "lxml")

        table = soup.find("div", class_="vacanciesTable")
        if not table:
            print("[UNDP] ERROR: div.vacanciesTable not found")
            return []

        all_cards = table.find_all("a", class_="vacanciesTable__row")
        latam_cards = [c for c in all_cards if _LATAM_REGION_CLASS in (c.get("class") or [])]
        cards_to_parse = latam_cards if latam_cards else all_cards
        print(f"[UNDP] Total cards: {len(all_cards)} | LATAM: {len(latam_cards)} | Parsing: {len(cards_to_parse)}")

        # Parse basic card info
        parsed_cards: list[dict] = []
        for card in cards_to_parse:
            opp = self._parse_card(card)
            if opp:
                parsed_cards.append(opp)

        # Skip expired records BEFORE fetching detail pages (spec AC 1)
        today = date.today()
        active_cards: list[dict] = []
        expired_count = 0
        for opp in parsed_cards:
            dl = opp.get("deadline")
            if dl:
                try:
                    if date.fromisoformat(dl) < today:
                        expired_count += 1
                        continue
                except ValueError:
                    pass
            active_cards.append(opp)

        if expired_count:
            print(f"[UNDP] Skipped {expired_count} expired records before detail fetch")
        print(f"[UNDP] Active cards to enrich: {len(active_cards)}")

        # Fetch detail pages with bounded concurrency
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DETAIL_FETCHES)
        detail_fetched = 0
        detail_fallback = 0

        async def enrich_one(opp: dict) -> None:
            nonlocal detail_fetched, detail_fallback
            detail_url = opp.get("opportunity_link", "")
            if not detail_url or detail_url == BASE_URL:
                detail_fallback += 1
                return
            async with semaphore:
                try:
                    async with httpx.AsyncClient(timeout=_DETAIL_REQUEST_TIMEOUT) as client:
                        dr = await client.get(detail_url, headers={"User-Agent": "Mozilla/5.0"})
                        dr.raise_for_status()
                    overview = _extract_overview_from_detail(dr.text)
                    if overview:
                        # Full text for keyword matching (spec AC 2)
                        opp["_full_overview"] = overview
                        # Truncated version for display/sheet
                        opp["description_snippet"] = overview[:_DESCRIPTION_DISPLAY_MAX]
                        detail_fetched += 1
                    else:
                        detail_fallback += 1
                except Exception as exc:
                    logger.warning("[undp] Detail fetch failed for %s: %s", detail_url, exc)
                    detail_fallback += 1

        await asyncio.gather(*[enrich_one(opp) for opp in active_cards])
        print(f"[UNDP] Detail pages: fetched={detail_fetched}, fallback={detail_fallback}")

        # Apply keyword filter using FULL overview text (spec AC 2)
        keyword_filter = KeywordFilter(self.config)
        filtered: list[dict] = []
        for opp in active_cards:
            # Use full overview for matching if available
            full_overview = opp.get("_full_overview")
            original_snippet = opp.get("description_snippet", "")

            if full_overview:
                # Temporarily set full text for filter check
                opp["description_snippet"] = full_overview

            if keyword_filter.passes_filter(opp):
                # Keep _full_overview in the dict for downstream get_matched_keywords()
                # Store truncated version as description_snippet for display/sheet
                opp["description_snippet"] = full_overview[:_DESCRIPTION_DISPLAY_MAX] if full_overview else original_snippet
                filtered.append(opp)
            else:
                # Not matched — restore original and discard
                opp["description_snippet"] = original_snippet

        print(f"[UNDP] Passed keyword filter: {len(filtered)}")
        if filtered:
            print(f"[UNDP] Sample: {filtered[0].get('opportunity_title', '')[:70]}")

        return filtered[: self.config.max_results]

    def _parse_card(self, card) -> Optional[dict]:
        """Parse a single listing-page card into a basic Opportunity_Dict."""
        try:
            title = _extract_cell(card, "Title")
            ref_no = _extract_cell(card, "Ref No")
            country_raw = _extract_cell(card, "UNDP Office/Country")
            process = _extract_cell(card, "Process")
            deadline_raw = _extract_cell(card, "Deadline")

            country_region = None
            if country_raw:
                if "/" in country_raw:
                    country_region = country_raw.split("/")[-1].strip().title()
                else:
                    country_region = country_raw.strip().title()

            if ref_no:
                opportunity_id = f"undp-{ref_no}"
            else:
                hash_src = (title or "") + (country_raw or "")
                opportunity_id = "undp-" + hashlib.sha256(hash_src.encode()).hexdigest()[:12]

            href = card.get("href", "")
            link = urljoin(f"{BASE_URL}/", href) if href else BASE_URL

            return {
                "opportunity_id": opportunity_id,
                "opportunity_title": title,
                "devex_opportunity_id": opportunity_id,
                "funder_organisation": "UNDP",
                "country_region": country_region,
                "deadline": _parse_deadline(deadline_raw),
                "contract_value": None,
                "opportunity_link": link,
                "description_snippet": title,  # default fallback; enriched later
                "source_portal": "undp",
                "portal_source": "UNDP",
                "notice_type": process,
                "matched_keywords": [],
            }
        except Exception as exc:
            self._log_error(exc, detail="card parse error")
            return None
