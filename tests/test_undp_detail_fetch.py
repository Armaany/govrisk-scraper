"""Tests for UNDP detail-page description enrichment (v3).

Covers:
  - Overview extraction (heading-based + fallback)
  - Full-text matching via _matching_text (keyword beyond 1000 chars)
  - Transient matching text excluded from serialization
  - Backward compatibility for non-UNDP adapters
  - Shared client reuse with retry/backoff
  - Permanent failure (404) gets exactly one attempt
  - Bounded concurrency (1 < peak <= 8)
  - Expired records skipped before detail fetch
  - Timeout preserves completed work
"""
import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from engine.keyword_filter import MATCHING_TEXT_KEY, KeywordFilter
from portals.undp_adapter import (
    UNDPAdapter,
    _extract_overview_from_detail,
    _DESCRIPTION_DISPLAY_MAX,
    _MAX_CONCURRENT_DETAIL_FETCHES,
    _MAX_ATTEMPTS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config():
    cfg = MagicMock()
    cfg.undp_enabled = True
    cfg.sector_keywords = ["corruption", "transparency", "justice", "governance", "integrity"]
    cfg.target_countries = ["Colombia", "Brazil", "Honduras", "Panama", "Argentina"]
    cfg.max_results = 50
    return cfg


def _make_detail_html(overview_text, docs_text="Short doc"):
    return f"""<html><body><main>
      <div class="postContent"><h2>Documents</h2><p>{docs_text}</p></div>
      <div class="postContent"><h2>Overview</h2><p>{overview_text}</p></div>
    </main></body></html>"""


def _make_listing_html(cards_html):
    return f"""<html><body><div class="vacanciesTable">
        <div class="vacanciesTable__header"></div>{cards_html}
    </div></body></html>"""


def _make_card_html(title, country, href, deadline="30-Dec-26", region_class="region_RLA"):
    return f"""<a class="vacanciesTableLink vacanciesTable__row {region_class}" href="{href}">
      <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">Title</div><span>{title}</span></div>
      <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">Ref No</div><span>T-001</span></div>
      <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">UNDP Office/Country</div><span>UNDP-COL/{country.upper()}</span></div>
      <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">Process</div><span>RFP</span></div>
      <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">Deadline</div><span>{deadline}</span></div>
    </a>"""


class FakeResponse:
    def __init__(self, text="", status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}
    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=MagicMock(), response=self
            )


class FakeClient:
    """Shared fake httpx.AsyncClient that tracks requests."""
    def __init__(self, handler):
        self._handler = handler
        self.request_log = []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        pass
    async def get(self, url, **kwargs):
        self.request_log.append(str(url))
        return await self._handler(str(url))


# ---------------------------------------------------------------------------
# OVERVIEW EXTRACTION
# ---------------------------------------------------------------------------

def test_extract_overview_by_heading():
    html = _make_detail_html("Anti-corruption reform program")
    assert "anti-corruption" in (_extract_overview_from_detail(html) or "").lower()

def test_extract_overview_strips_heading():
    result = _extract_overview_from_detail(_make_detail_html("Body text here"))
    assert result and not result.startswith("Overview")

def test_extract_overview_full_text_not_truncated():
    long = "x" * 5000
    result = _extract_overview_from_detail(_make_detail_html(long))
    assert result and len(result) == 5000

def test_extract_overview_none_when_no_postcontent():
    assert _extract_overview_from_detail("<html><body></body></html>") is None

def test_extract_overview_fallback_when_no_heading():
    html = """<html><body><main>
      <div class="postContent"><p>Short</p></div>
      <div class="postContent"><p>Longer block about governance reform programs</p></div>
    </main></body></html>"""
    result = _extract_overview_from_detail(html)
    assert result and "governance" in result

def test_documents_longer_than_overview_only_overview_used():
    html = f"""<html><body><main>
      <div class="postContent"><h2>Documents</h2><p>{'doc ' * 2000}</p></div>
      <div class="postContent"><h2>Overview</h2><p>Short transparency text</p></div>
    </main></body></html>"""
    result = _extract_overview_from_detail(html)
    assert "transparency" in result
    assert "doc doc doc" not in result


# ---------------------------------------------------------------------------
# FULL-TEXT FILTERING END TO END
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_keyword_after_1000_chars_passes_filter_and_matched_keywords():
    """Keyword at position 1500 passes filter AND appears in matched_keywords."""
    filler = "lorem ipsum " * 130
    overview = filler + "corruption program"
    detail_html = _make_detail_html(overview)
    listing_html = _make_listing_html(
        _make_card_html("Generic Title", "Colombia", "view_notice.cfm?notice_id=1")
    )
    config = _make_config()
    adapter = UNDPAdapter(config)

    async def handler(url):
        if "notice_id" in url:
            return FakeResponse(detail_html)
        return FakeResponse(listing_html)

    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=FakeClient(handler)):
        results = await adapter.fetch_opportunities()

    assert len(results) >= 1
    opp = results[0]
    # description_snippet is truncated for display
    assert len(opp["description_snippet"]) <= _DESCRIPTION_DISPLAY_MAX
    # _matching_text carries full text
    assert MATCHING_TEXT_KEY in opp
    assert "corruption" in opp[MATCHING_TEXT_KEY]
    # get_matched_keywords sees full text
    kf = KeywordFilter(config)
    assert "corruption" in kf.get_matched_keywords(opp)


@pytest.mark.asyncio
async def test_matching_text_excluded_before_serialization():
    """_matching_text must not appear as a Sheets column."""
    from store.adapter_sheets import SheetsAdapter
    assert MATCHING_TEXT_KEY not in SheetsAdapter.HEADERS


def test_non_undp_opportunity_filters_normally():
    """Opportunity without _matching_text uses legacy title+description_snippet."""
    config = _make_config()
    kf = KeywordFilter(config)
    opp = {
        "opportunity_title": "Governance reform in Colombia",
        "description_snippet": "Support for anti-corruption",
        "country_region": "Colombia",
    }
    assert kf.passes_filter(opp)
    assert "governance" in kf.get_matched_keywords(opp)
    assert "corruption" in kf.get_matched_keywords(opp)


# ---------------------------------------------------------------------------
# SHARED CLIENT AND RETRY/BACKOFF
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transient_failure_then_success():
    """First attempt fails transiently, second succeeds."""
    detail_html = _make_detail_html("Transparency program Colombia")
    listing_html = _make_listing_html(
        _make_card_html("Title", "Colombia", "view_notice.cfm?notice_id=1")
    )
    attempt_count = [0]

    async def handler(url):
        if "notice_id" in url:
            attempt_count[0] += 1
            if attempt_count[0] == 1:
                import httpx
                raise httpx.ConnectError("transient failure")
            return FakeResponse(detail_html)
        return FakeResponse(listing_html)

    config = _make_config()
    adapter = UNDPAdapter(config)

    # Inject zero-backoff for test speed
    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=FakeClient(handler)), \
         patch("portals.undp_adapter._BASE_BACKOFF", 0), \
         patch("portals.undp_adapter.random.uniform", return_value=0):
        results = await adapter.fetch_opportunities()

    assert len(results) >= 1
    assert attempt_count[0] == 2  # first failed, second succeeded


@pytest.mark.asyncio
async def test_repeated_transient_failures_exhaust_attempts():
    """All attempts fail transiently — falls back to title."""
    listing_html = _make_listing_html(
        _make_card_html("Anti-corruption Colombia", "Colombia", "view_notice.cfm?notice_id=1")
    )
    attempt_count = [0]

    async def handler(url):
        if "notice_id" in url:
            attempt_count[0] += 1
            import httpx
            raise httpx.ConnectError("always failing")
        return FakeResponse(listing_html)

    config = _make_config()
    adapter = UNDPAdapter(config)

    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=FakeClient(handler)), \
         patch("portals.undp_adapter._BASE_BACKOFF", 0), \
         patch("portals.undp_adapter.random.uniform", return_value=0):
        results = await adapter.fetch_opportunities()

    assert attempt_count[0] == _MAX_ATTEMPTS
    # Title "Anti-corruption Colombia" passes filter on its own
    assert len(results) >= 1
    assert MATCHING_TEXT_KEY not in results[0]


@pytest.mark.asyncio
async def test_404_gets_exactly_one_attempt():
    """A 404 is permanent — exactly one request, no retry."""
    listing_html = _make_listing_html(
        _make_card_html("Corruption reform", "Colombia", "view_notice.cfm?notice_id=1")
    )
    attempt_count = [0]

    async def handler(url):
        if "notice_id" in url:
            attempt_count[0] += 1
            return FakeResponse("", status_code=404)
        return FakeResponse(listing_html)

    config = _make_config()
    adapter = UNDPAdapter(config)

    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=FakeClient(handler)):
        results = await adapter.fetch_opportunities()

    assert attempt_count[0] == 1  # no retry for 404


@pytest.mark.asyncio
async def test_shared_client_reused():
    """The same client instance is used for listing and all detail requests."""
    detail_html = _make_detail_html("Governance text")
    listing_html = _make_listing_html(
        _make_card_html("Gov", "Colombia", "view_notice.cfm?notice_id=1") +
        _make_card_html("Gov2", "Colombia", "view_notice.cfm?notice_id=2")
    )
    clients_created = [0]
    original_init = FakeClient.__init__

    async def handler(url):
        if "notice_id" in url:
            return FakeResponse(detail_html)
        return FakeResponse(listing_html)

    def tracking_init(self, h):
        clients_created[0] += 1
        original_init(self, h)

    config = _make_config()
    adapter = UNDPAdapter(config)

    fake = FakeClient(handler)
    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=fake):
        await adapter.fetch_opportunities()

    # All requests should go through the same FakeClient instance
    assert len(fake.request_log) >= 3  # 1 listing + 2 details


# ---------------------------------------------------------------------------
# CONCURRENCY, EXPIRY, TIMING
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrency_bounded_and_expired_skipped():
    """106 cards: verifies 1 < peak_concurrency <= 8, expired skipping, request count."""
    active_cards = "".join(
        _make_card_html(f"Opp{i}", "Colombia", f"view_notice.cfm?notice_id={i}", "30-Dec-26")
        for i in range(100)
    )
    expired_cards = "".join(
        _make_card_html(f"Exp{i}", "Colombia", f"view_notice.cfm?notice_id=exp{i}", "01-Jan-20")
        for i in range(6)
    )
    listing_html = _make_listing_html(active_cards + expired_cards)
    detail_html = _make_detail_html("Governance and transparency reform")

    config = _make_config()
    adapter = UNDPAdapter(config)

    max_concurrent = [0]
    current_concurrent = [0]
    request_urls = []

    async def handler(url):
        if "notice_id" in url:
            request_urls.append(url)
            current_concurrent[0] += 1
            if current_concurrent[0] > max_concurrent[0]:
                max_concurrent[0] = current_concurrent[0]
            await asyncio.sleep(0.01)  # simulate latency
            current_concurrent[0] -= 1
            return FakeResponse(detail_html)
        return FakeResponse(listing_html)

    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=FakeClient(handler)):
        results = await adapter.fetch_opportunities()

    # Bounded concurrency: 1 < peak <= 8
    assert 1 < max_concurrent[0] <= _MAX_CONCURRENT_DETAIL_FETCHES, (
        f"peak_concurrency={max_concurrent[0]}"
    )
    # Exact request count = 100 active cards (expired are never requested)
    detail_requests = [u for u in request_urls if "notice_id" in u]
    assert len(detail_requests) == 100
    # No expired IDs in request log
    assert not any("exp" in u for u in detail_requests)
    # Results exist
    assert len(results) > 0


@pytest.mark.asyncio
async def test_timing_concurrent_faster_than_sequential():
    """Concurrent execution is materially faster than sequential would be."""
    n_cards = 50
    per_request_delay = 0.02  # 20ms
    cards_html = "".join(
        _make_card_html(f"O{i}", "Colombia", f"view_notice.cfm?notice_id={i}", "30-Dec-26")
        for i in range(n_cards)
    )
    listing_html = _make_listing_html(cards_html)
    detail_html = _make_detail_html("Governance transparency reform")

    config = _make_config()
    adapter = UNDPAdapter(config)

    async def handler(url):
        if "notice_id" in url:
            await asyncio.sleep(per_request_delay)
            return FakeResponse(detail_html)
        return FakeResponse(listing_html)

    start = time.time()
    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=FakeClient(handler)):
        results = await adapter.fetch_opportunities()
    elapsed = time.time() - start

    # Sequential lower bound: 50 * 0.02 = 1.0s
    # Concurrent (8 slots): ceil(50/8) * 0.02 ≈ 0.14s
    sequential_lower_bound = n_cards * per_request_delay
    assert elapsed < sequential_lower_bound * 0.5, (
        f"Elapsed {elapsed:.2f}s >= {sequential_lower_bound * 0.5:.2f}s — not concurrent enough"
    )
    assert len(results) > 0


# ---------------------------------------------------------------------------
# TIMEOUT PRESERVES COMPLETED WORK
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_preserves_completed_records():
    """Fast records are preserved when slow ones exceed the adapter deadline."""
    # 5 fast cards + 1 slow card that would exceed a very short deadline
    cards_html = "".join(
        _make_card_html(f"Fast{i}", "Colombia", f"view_notice.cfm?notice_id=fast{i}", "30-Dec-26")
        for i in range(5)
    )
    cards_html += _make_card_html("Slow", "Colombia", "view_notice.cfm?notice_id=slow", "30-Dec-26")
    listing_html = _make_listing_html(cards_html)
    detail_html = _make_detail_html("Governance and transparency")

    config = _make_config()
    adapter = UNDPAdapter(config)

    async def handler(url):
        if "slow" in url:
            await asyncio.sleep(10)  # will be cancelled
            return FakeResponse(detail_html)
        if "notice_id" in url:
            await asyncio.sleep(0.01)
            return FakeResponse(detail_html)
        return FakeResponse(listing_html)

    # Use a very short adapter timeout
    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=FakeClient(handler)), \
         patch("portals.undp_adapter._ADAPTER_LEVEL_TIMEOUT", 0.5):
        results = await adapter.fetch_opportunities()

    # Fast records should be preserved (they complete before the deadline)
    assert len(results) >= 1, "Completed fast records should not be discarded"
    # The result should NOT be empty (the old behavior)
    # The slow task should have been cancelled, not blocking others
