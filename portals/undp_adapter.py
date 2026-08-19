"""UNDPAdapter — scrapes UNDP Procurement Notices portal.

v3: Unified _matching_text, shared httpx client, retry/backoff for transient errors,
bounded concurrency, explicit task management for timeout preservation.
"""
import asyncio
import hashlib
import logging
import random
import re
from datetime import date, datetime
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from engine.keyword_filter import MATCHING_TEXT_KEY, KeywordFilter
from portals.base_adapter import BasePortalAdapter

logger = logging.getLogger(__name__)

BASE_URL = "https://procurement-notices.undp.org"

_LATAM_REGION_CLASS = "region_RLA"

# Concurrency and timeout settings
_MAX_CONCURRENT_DETAIL_FETCHES = 8
_DETAIL_REQUEST_TIMEOUT = 12  # seconds per individual request attempt
_ADAPTER_LEVEL_TIMEOUT = 120  # seconds — entire adapter must complete within this

# Retry settings — only for transient failures
_MAX_ATTEMPTS = 3
_BASE_BACKOFF = 0.5  # seconds — multiplied by 2^attempt with jitter
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_PERMANENT_FAIL_STATUS_CODES = {400, 401, 403, 404}

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
    logger.warning("[undp] Overview heading not found — using longest postContent block (fallback)")
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
        """Fetch UNDP notices with bounded concurrency and deadline-preserving timeout."""
        if not getattr(self.config, "undp_enabled", True):
            print("[UNDP] Adapter disabled — skipping")
            return []
        return await self._run()

    async def _run(self) -> list[dict]:
        """Core logic with explicit task management for timeout preservation."""
        url = f"{BASE_URL}/"
        print(f"[UNDP] Requesting listing: {url}")

        # One shared client for the entire adapter run
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_DETAIL_REQUEST_TIMEOUT, connect=10),
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            # Fetch listing page
            try:
                resp = await client.get(url)
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
            print(f"[UNDP] Total: {len(all_cards)} | LATAM: {len(latam_cards)} | Parsing: {len(cards_to_parse)}")

            # Parse basic card info
            parsed_cards = [self._parse_card(c) for c in cards_to_parse]
            parsed_cards = [o for o in parsed_cards if o]

            # Skip expired BEFORE detail fetches
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
                print(f"[UNDP] Skipped {expired_count} expired before detail fetch")
            print(f"[UNDP] Active cards to enrich: {len(active_cards)}")

            # Bounded-concurrency detail enrichment with explicit deadline
            semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DETAIL_FETCHES)
            deadline = asyncio.get_event_loop().time() + _ADAPTER_LEVEL_TIMEOUT

            async def enrich_one(opp: dict) -> bool:
                """Enrich one opportunity. Returns True if detail fetched successfully."""
                detail_url = opp.get("opportunity_link", "")
                if not detail_url or detail_url == BASE_URL:
                    return False
                async with semaphore:
                    overview = await self._fetch_detail_with_retry(client, detail_url)
                    if overview:
                        opp[MATCHING_TEXT_KEY] = overview
                        opp["description_snippet"] = overview[:_DESCRIPTION_DISPLAY_MAX]
                        return True
                    return False

            # Create tasks and wait with timeout
            tasks = [asyncio.create_task(enrich_one(opp)) for opp in active_cards]

            # Wait for all tasks but respect the adapter deadline
            remaining_time = max(0, deadline - asyncio.get_event_loop().time())
            done, pending = await asyncio.wait(tasks, timeout=remaining_time)

            # Cancel and await pending tasks cleanly
            cancelled_count = 0
            for task in pending:
                task.cancel()
                cancelled_count += 1
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            # Count results
            detail_fetched = sum(1 for t in done if not t.cancelled() and t.result())
            detail_fallback = len(active_cards) - detail_fetched

            if cancelled_count:
                logger.warning(
                    "[undp] Adapter deadline reached: total=%d, completed=%d, "
                    "fallback=%d, cancelled=%d — run incomplete",
                    len(active_cards), detail_fetched,
                    detail_fallback - cancelled_count, cancelled_count,
                )
                print(f"[UNDP] WARN: deadline reached — {cancelled_count} tasks cancelled, "
                      f"{detail_fetched} completed")
            else:
                print(f"[UNDP] Detail pages: fetched={detail_fetched}, fallback={detail_fallback}")

        # Apply keyword filter — uses _matching_text when present (no mutation needed)
        keyword_filter = KeywordFilter(self.config)
        filtered: list[dict] = []
        for opp in active_cards:
            if keyword_filter.passes_filter(opp):
                filtered.append(opp)

        print(f"[UNDP] Passed keyword filter: {len(filtered)}")
        if filtered:
            print(f"[UNDP] Sample: {filtered[0].get('opportunity_title', '')[:70]}")

        return filtered[: self.config.max_results]

    async def _fetch_detail_with_retry(
        self, client: httpx.AsyncClient, url: str
    ) -> Optional[str]:
        """Fetch a detail page with bounded retry for transient failures.

        Returns the extracted Overview text or None on permanent failure.
        """
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = await client.get(url)

                # Permanent failure — do not retry
                if resp.status_code in _PERMANENT_FAIL_STATUS_CODES:
                    logger.warning(
                        "[undp] Permanent %d for %s — no retry", resp.status_code, url
                    )
                    return None

                # Retryable status
                if resp.status_code in _RETRYABLE_STATUS_CODES:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and attempt < _MAX_ATTEMPTS:
                        try:
                            wait = min(float(retry_after), _ADAPTER_LEVEL_TIMEOUT / 4)
                        except ValueError:
                            wait = _BASE_BACKOFF * (2 ** (attempt - 1))
                        await asyncio.sleep(wait)
                        continue
                    if attempt < _MAX_ATTEMPTS:
                        backoff = _BASE_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
                        await asyncio.sleep(backoff)
                        continue
                    logger.warning(
                        "[undp] Exhausted %d attempts for %s (last status=%d)",
                        _MAX_ATTEMPTS, url, resp.status_code,
                    )
                    return None

                resp.raise_for_status()
                return _extract_overview_from_detail(resp.text)

            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                if attempt < _MAX_ATTEMPTS:
                    backoff = _BASE_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
                    logger.info(
                        "[undp] Transient error for %s (attempt %d/%d): %s — retrying in %.1fs",
                        url, attempt, _MAX_ATTEMPTS, exc, backoff,
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.warning(
                        "[undp] Exhausted %d attempts for %s: %s",
                        _MAX_ATTEMPTS, url, exc,
                    )
                    return None
            except httpx.HTTPStatusError as exc:
                # Unexpected status error — treat as non-retryable
                logger.warning("[undp] HTTP error for %s: %s", url, exc)
                return None
            except Exception as exc:
                logger.warning("[undp] Unexpected error for %s: %s", url, exc)
                return None

        return None

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
