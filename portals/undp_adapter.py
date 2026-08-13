"""UNDPAdapter — scrapes UNDP Procurement Notices portal using requests + BeautifulSoup."""
import hashlib
import logging
import re
from datetime import date, datetime
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from engine.keyword_filter import KeywordFilter
from portals.base_adapter import BasePortalAdapter

logger = logging.getLogger(__name__)

BASE_URL = "https://procurement-notices.undp.org"

# LATAM region class applied by UNDP to each <a> card — used for client-side filtering
_LATAM_REGION_CLASS = "region_RLA"

# Maximum characters to keep from the detail page description
_DESCRIPTION_MAX_CHARS = 1000


def _parse_deadline(raw: Optional[str]) -> Optional[str]:
    """Parse a UNDP deadline string like '06-May-26' or '30-Apr-2604:00 AM' into YYYY-MM-DD."""
    if not raw:
        return None
    raw = raw.strip()
    # Extract date part: DD-Mon-YY (possibly followed immediately by time digits)
    match = re.match(r"(\d{1,2}-[A-Za-z]{3}-\d{2})", raw)
    if not match:
        return None
    date_str = match.group(1)
    try:
        parsed = datetime.strptime(date_str, "%d-%b-%y")
        return parsed.date().isoformat()
    except ValueError:
        return None


def _extract_cell(card: BeautifulSoup, label: str) -> Optional[str]:
    """Extract the value span from a vacanciesTable__cell matching the given label.

    Each cell has structure:
      <div class="vacanciesTable__cell">
        <div class="vacanciesTable__cell__label">Deadline</div>
        <span>06-May-26</span>
      </div>
    """
    label_lower = label.lower().strip()
    for cell in card.find_all("div", class_="vacanciesTable__cell"):
        label_div = cell.find("div", class_="vacanciesTable__cell__label")
        if label_div and label_div.get_text(strip=True).lower() == label_lower:
            span = cell.find("span")
            if span:
                return span.get_text(strip=True) or None
            # Fallback: get the cell text minus the label text
            full_text = cell.get_text(" ", strip=True)
            label_text = label_div.get_text(strip=True)
            value = full_text[len(label_text):].strip()
            return value or None
    return None


def _fetch_detail_description(url: str) -> Optional[str]:
    """Fetch the UNDP detail page and extract the Overview/body description text.

    The detail page contains several ``div.postContent`` sections:
    - "Link to Atlas Project"
    - "Documents"
    - "Overview"  ← this is the one with real descriptive content

    We return the text of the largest postContent block (which is always Overview)
    truncated to _DESCRIPTION_MAX_CHARS, or None if the request fails.
    """
    resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    # All postContent divs — pick the one with the most text (the Overview block)
    content_divs = soup.find_all("div", class_="postContent")
    if not content_divs:
        return None

    best = max(content_divs, key=lambda d: len(d.get_text()))
    text = best.get_text(" ", strip=True)
    return text[:_DESCRIPTION_MAX_CHARS] if text else None


class UNDPAdapter(BasePortalAdapter):
    """Adapter for the UNDP Procurement Notices portal.

    The portal serves all notices in a single HTML page (no server-side search).
    We fetch the page once, parse all 500+ cards, filter by LATAM region class
    and then apply KeywordFilter for sector relevance.
    """

    portal_name = "undp"

    def is_available(self) -> bool:
        """Return True if the portal responds with HTTP 200."""
        try:
            resp = requests.get(BASE_URL, timeout=10)
            return resp.status_code == 200
        except Exception as exc:
            self._log_error(exc, detail="availability check failed")
            return False

    async def fetch_opportunities(self) -> list[dict]:
        """Fetch and parse UNDP procurement notices.

        Strategy:
        1. GET the main page (all notices are server-rendered in one HTML response).
        2. Parse all <a class="vacanciesTable__row"> cards.
        3. Filter to LATAM cards using the region_RLA CSS class.
        4. Fall back to all cards if LATAM yields 0 results.
        5. Apply KeywordFilter for sector relevance.
        6. Return up to config.max_results matches.
        """
        if not getattr(self.config, "undp_enabled", True):
            print("[UNDP] Adapter disabled — skipping")
            return []

        url = f"{BASE_URL}/"
        print(f"[UNDP] Requesting: {url}")

        try:
            resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            print(f"[UNDP] HTTP status: {resp.status_code}")
            print(f"[UNDP] Response size: {len(resp.text)} chars")
            print(f"[UNDP] First 300 chars: {repr(resp.text[:300])}")
            resp.raise_for_status()
        except requests.RequestException as exc:
            self._log_error(exc, detail="HTTP request failed")
            print(f"[UNDP] ERROR: request failed — {exc}")
            return []

        soup = BeautifulSoup(resp.text, "lxml")

        # Locate the vacanciesTable container
        table = soup.find("div", class_="vacanciesTable")
        if not table:
            print("[UNDP] ERROR: div.vacanciesTable not found in page")
            return []

        all_cards = table.find_all("a", class_="vacanciesTable__row")
        print(f"[UNDP] Raw cards found (all regions): {len(all_cards)}")

        # Filter to LATAM region first
        latam_cards = [c for c in all_cards if _LATAM_REGION_CLASS in (c.get("class") or [])]
        print(f"[UNDP] LATAM cards (region_RLA): {len(latam_cards)}")

        # Use LATAM subset if available, otherwise fall back to all cards
        cards_to_parse = latam_cards if latam_cards else all_cards
        print(f"[UNDP] Cards to parse: {len(cards_to_parse)}")

        # Parse cards into opportunity dicts, fetching detail page descriptions
        raw_results: list[dict] = []
        detail_fetched = 0
        detail_fallback = 0
        for card in cards_to_parse:
            opp = self._parse_card(card)
            if not opp:
                continue

            # Fetch description from the detail page
            detail_url = opp.get("opportunity_link", "")
            if detail_url and detail_url != BASE_URL:
                try:
                    description = _fetch_detail_description(detail_url)
                    if description:
                        opp["description_snippet"] = description
                        detail_fetched += 1
                    else:
                        # Page loaded but no content found — keep title as fallback
                        detail_fallback += 1
                except Exception as exc:
                    logger.warning("[undp] Detail page fetch failed for %s: %s", detail_url, exc)
                    detail_fallback += 1
                    # description_snippet already set to title in _parse_card — leave as-is

            raw_results.append(opp)

        print(f"[UNDP] Successfully parsed: {len(raw_results)} records "
              f"(detail fetched: {detail_fetched}, fallback to title: {detail_fallback})")

        # Filter out expired deadlines
        today = date.today()
        active, expired = [], []
        for opp in raw_results:
            dl = opp.get("deadline")
            if dl:
                try:
                    if date.fromisoformat(dl) < today:
                        expired.append(opp)
                        continue
                except ValueError:
                    pass
            active.append(opp)
        if expired:
            print(f"[UNDP] Skipped {len(expired)} expired records")
        raw_results = active

        # Apply keyword filter — now accent-insensitive and Spanish-aware
        keyword_filter = KeywordFilter(self.config)
        filtered = [opp for opp in raw_results if keyword_filter.passes_filter(opp)]
        print(f"[UNDP] Passed keyword filter: {len(filtered)}")

        if filtered:
            print(f"[UNDP] Sample match: {filtered[0].get('opportunity_title', '')[:80]}")

        return filtered[: self.config.max_results]

    def _parse_card(self, card: BeautifulSoup) -> Optional[dict]:
        """Parse a single <a class='vacanciesTable__row'> into an Opportunity_Dict."""
        try:
            title = _extract_cell(card, "Title")
            ref_no = _extract_cell(card, "Ref No")
            country_raw = _extract_cell(card, "UNDP Office/Country")
            process = _extract_cell(card, "Process")
            deadline_raw = _extract_cell(card, "Deadline")
            posted_raw = _extract_cell(card, "Posted")

            # Normalise country: "UNOPS/GABON" → "Gabon", "UNDP-COL/COLOMBIA" → "Colombia"
            country_region = None
            if country_raw:
                if "/" in country_raw:
                    country_region = country_raw.split("/")[-1].strip().title()
                else:
                    country_region = country_raw.strip().title()

            # Build opportunity_id
            if ref_no:
                opportunity_id = f"undp-{ref_no}"
            else:
                hash_src = (title or "") + (country_raw or "")
                opportunity_id = "undp-" + hashlib.sha256(hash_src.encode()).hexdigest()[:12]

            # Build full link
            href = card.get("href", "")
            if href:
                link = urljoin(f"{BASE_URL}/", href)
            else:
                link = BASE_URL

            deadline = _parse_deadline(deadline_raw)

            return {
                "opportunity_id": opportunity_id,
                "opportunity_title": title,
                "devex_opportunity_id": opportunity_id,
                "funder_organisation": "UNDP",
                "country_region": country_region,
                "deadline": deadline,
                "contract_value": None,
                "opportunity_link": link,
                # description_snippet is intentionally set to title here as a safe
                # default; fetch_opportunities() will overwrite it with the real
                # description fetched from the detail page.
                "description_snippet": title,
                "source_portal": "undp",
                "portal_source": "UNDP",
                "notice_type": process,
                "publication_date": posted_raw,
                "matched_keywords": [],
            }
        except Exception as exc:
            self._log_error(exc, detail="card parse error")
            return None
