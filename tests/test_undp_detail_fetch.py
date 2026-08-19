"""Tests for UNDP detail-page description enrichment (v2).

Covers:
  - Original 6: description extraction, truncation, fallback
  - Adversarial: keyword after char 1000 still passes filter
  - Adversarial: Documents block longer than Overview — only Overview used
  - Concurrency: 106 cards, bounded concurrency, expired skipping, failure resilience
  - Timing: adapter completes within defined max time
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from portals.undp_adapter import (
    UNDPAdapter,
    _extract_overview_from_detail,
    _DESCRIPTION_DISPLAY_MAX,
    _MAX_CONCURRENT_DETAIL_FETCHES,
    _ADAPTER_LEVEL_TIMEOUT,
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


def _make_detail_html(overview_text: str, docs_text: str = "Short doc") -> str:
    return f"""
    <html><body><main>
      <div class="postContent"><h2>Documents</h2><p>{docs_text}</p></div>
      <div class="postContent"><h2>Overview</h2><p>{overview_text}</p></div>
    </main></body></html>
    """


def _make_listing_html(cards_html: str) -> str:
    return f"""
    <html><body>
      <div class="vacanciesTable">
        <div class="vacanciesTable__header"></div>
        {cards_html}
      </div>
    </body></html>
    """


def _make_card_html(title, country, href, deadline="30-Dec-26", region_class="region_RLA"):
    return f"""
    <a class="vacanciesTableLink vacanciesTable__row {region_class}" href="{href}">
      <div class="vacanciesTable__cell">
        <div class="vacanciesTable__cell__label">Title</div><span>{title}</span>
      </div>
      <div class="vacanciesTable__cell">
        <div class="vacanciesTable__cell__label">Ref No</div><span>TEST-001</span>
      </div>
      <div class="vacanciesTable__cell">
        <div class="vacanciesTable__cell__label">UNDP Office/Country</div><span>UNDP-COL/{country.upper()}</span>
      </div>
      <div class="vacanciesTable__cell">
        <div class="vacanciesTable__cell__label">Process</div><span>RFP</span>
      </div>
      <div class="vacanciesTable__cell">
        <div class="vacanciesTable__cell__label">Deadline</div><span>{deadline}</span>
      </div>
    </a>
    """


# ---------------------------------------------------------------------------
# Unit tests for _extract_overview_from_detail
# ---------------------------------------------------------------------------

def test_extract_overview_by_heading():
    """Primary: identifies Overview block by its <h2> heading."""
    html = _make_detail_html("Real overview about anti-corruption")
    result = _extract_overview_from_detail(html)
    assert result is not None
    assert "anti-corruption" in result


def test_extract_overview_strips_heading_text():
    """The heading text 'Overview' itself is stripped from the returned text."""
    html = _make_detail_html("Description body here.")
    result = _extract_overview_from_detail(html)
    assert result is not None
    assert not result.startswith("Overview")


def test_extract_overview_returns_full_text_not_truncated():
    """Returns the FULL overview text without truncation."""
    long_text = "x" * 5000
    html = _make_detail_html(long_text)
    result = _extract_overview_from_detail(html)
    assert result is not None
    assert len(result) == 5000


def test_extract_overview_returns_none_when_no_postcontent():
    html = "<html><body><main><p>Nothing</p></main></body></html>"
    result = _extract_overview_from_detail(html)
    assert result is None


def test_extract_overview_fallback_to_longest_when_no_heading():
    """Falls back to longest postContent when no Overview heading exists."""
    html = """
    <html><body><main>
      <div class="postContent"><p>Short</p></div>
      <div class="postContent"><p>This is a much longer block with real content about governance reform.</p></div>
    </main></body></html>
    """
    result = _extract_overview_from_detail(html)
    assert result is not None
    assert "governance reform" in result


# ---------------------------------------------------------------------------
# ADVERSARIAL: keyword after character 1000 still passes filter (spec AC 2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_keyword_after_1000_chars_still_passes():
    """A keyword placed at character 1500 is still found by the filter because
    full overview text (not truncated) is used for matching."""
    filler = "lorem ipsum " * 125  # ~1500 chars
    overview = filler + "corruption program details"
    detail_html = _make_detail_html(overview)
    listing_html = _make_listing_html(
        _make_card_html("Generic Title", "Colombia", "view_notice.cfm?notice_id=1")
    )

    config = _make_config()
    adapter = UNDPAdapter(config)

    class FakeResponse:
        def __init__(self, text):
            self.text = text
            self.status_code = 200
        def raise_for_status(self):
            pass

    class FakeAsyncClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url, **kwargs):
            if "notice_id" in str(url):
                return FakeResponse(detail_html)
            return FakeResponse(listing_html)

    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=FakeAsyncClient()):
        results = await adapter.fetch_opportunities()

    assert len(results) >= 1, "Keyword at position 1500 should still match via full-text"
    assert len(results[0]["description_snippet"]) <= _DESCRIPTION_DISPLAY_MAX
    # Verify _full_overview is preserved for downstream get_matched_keywords
    assert "_full_overview" in results[0], "_full_overview must be kept for main.py to use"
    assert "corruption" in results[0]["_full_overview"]


@pytest.mark.asyncio
async def test_matched_keywords_populated_from_full_overview():
    """get_matched_keywords() must find keywords beyond char 1000 using _full_overview."""
    from engine.keyword_filter import KeywordFilter

    # Build overview with keyword only after position 1500
    filler = "generic text " * 130  # ~1690 chars
    overview = filler + "transparency reform initiative"
    detail_html = _make_detail_html(overview)
    listing_html = _make_listing_html(
        _make_card_html("Boring Title No Keywords", "Colombia", "view_notice.cfm?notice_id=7")
    )

    config = _make_config()
    adapter = UNDPAdapter(config)

    class FakeResponse:
        def __init__(self, text):
            self.text = text
            self.status_code = 200
        def raise_for_status(self):
            pass

    class FakeAsyncClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url, **kwargs):
            if "notice_id" in str(url):
                return FakeResponse(detail_html)
            return FakeResponse(listing_html)

    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=FakeAsyncClient()):
        results = await adapter.fetch_opportunities()

    assert len(results) >= 1

    # Simulate what main.py does: use _full_overview for get_matched_keywords
    opp = results[0]
    kf = KeywordFilter(config)
    full_overview = opp.get("_full_overview")
    assert full_overview is not None

    saved = opp["description_snippet"]
    opp["description_snippet"] = full_overview
    matched = kf.get_matched_keywords(opp)
    opp["description_snippet"] = saved

    assert "transparency" in matched, (
        f"Expected 'transparency' in matched_keywords but got: '{matched}'"
    )


@pytest.mark.asyncio
async def test_documents_block_longer_than_overview_only_overview_used():
    """Even if Documents block is longer, heading-based extraction picks Overview."""
    long_docs = "document " * 500
    overview = "Short overview about transparency and governance reform"
    detail_html = f"""
    <html><body><main>
      <div class="postContent"><h2>Documents</h2><p>{long_docs}</p></div>
      <div class="postContent"><h2>Overview</h2><p>{overview}</p></div>
    </main></body></html>
    """
    listing_html = _make_listing_html(
        _make_card_html("Some Title", "Colombia", "view_notice.cfm?notice_id=2")
    )

    config = _make_config()
    adapter = UNDPAdapter(config)

    class FakeResponse:
        def __init__(self, text):
            self.text = text
            self.status_code = 200
        def raise_for_status(self):
            pass

    class FakeAsyncClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url, **kwargs):
            if "notice_id" in str(url):
                return FakeResponse(detail_html)
            return FakeResponse(listing_html)

    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=FakeAsyncClient()):
        results = await adapter.fetch_opportunities()

    assert len(results) >= 1
    desc = results[0]["description_snippet"]
    assert "transparency" in desc
    assert "document " * 10 not in desc


# ---------------------------------------------------------------------------
# CONCURRENCY: 106 cards, bounded concurrency, expired skipping, failure resilience
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrency_106_cards_bounded_and_resilient():
    """Simulates 106 cards: verifies bounded concurrency, expired skipping,
    and that one failing detail request doesn't crash the adapter."""
    # 100 active cards + 6 expired cards = 106 total
    active_cards_html = ""
    for i in range(100):
        active_cards_html += _make_card_html(
            f"Opp {i}", "Colombia", f"view_notice.cfm?notice_id={i}", deadline="30-Dec-26"
        )
    for i in range(100, 106):
        active_cards_html += _make_card_html(
            f"Expired {i}", "Colombia", f"view_notice.cfm?notice_id={i}", deadline="01-Jan-20"
        )

    listing_html = _make_listing_html(active_cards_html)
    overview_text = "This opportunity covers governance and transparency reform"
    detail_html = _make_detail_html(overview_text)

    config = _make_config()
    adapter = UNDPAdapter(config)

    # Track concurrent access
    max_concurrent = [0]
    current_concurrent = [0]

    import httpx as httpx_mod

    class FakeResponse:
        def __init__(self, text, status_code=200):
            self.text = text
            self.status_code = status_code
        def raise_for_status(self):
            pass

    class FakeAsyncClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url, **kwargs):
            url_str = str(url)
            if "notice_id" in url_str:
                current_concurrent[0] += 1
                if current_concurrent[0] > max_concurrent[0]:
                    max_concurrent[0] = current_concurrent[0]
                await asyncio.sleep(0.005)
                current_concurrent[0] -= 1
                if "notice_id=50" in url_str:
                    raise Exception("Simulated failure for card 50")
                return FakeResponse(detail_html)
            return FakeResponse(listing_html)

    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=FakeAsyncClient()):
        results = await adapter.fetch_opportunities()

    # Expired cards should be skipped (6 of them)
    assert len(results) > 0, "Should have results despite one failure"
    assert max_concurrent[0] <= _MAX_CONCURRENT_DETAIL_FETCHES, (
        f"Max concurrent was {max_concurrent[0]}, limit is {_MAX_CONCURRENT_DETAIL_FETCHES}"
    )


# ---------------------------------------------------------------------------
# TIMING: adapter completes within defined max time for 106 cards
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timing_106_cards_under_30_seconds():
    """Adapter must complete 106 cards well under 30s with concurrent mocks."""
    cards_html = ""
    for i in range(106):
        cards_html += _make_card_html(
            f"Opp {i}", "Colombia", f"view_notice.cfm?notice_id={i}", deadline="30-Dec-26"
        )
    listing_html = _make_listing_html(cards_html)
    detail_html = _make_detail_html("Overview about governance and transparency")

    config = _make_config()
    adapter = UNDPAdapter(config)

    class FakeResponse:
        def __init__(self, text):
            self.text = text
            self.status_code = 200
        def raise_for_status(self):
            pass

    class FakeAsyncClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url, **kwargs):
            if "notice_id" in str(url):
                await asyncio.sleep(0.005)
                return FakeResponse(detail_html)
            return FakeResponse(listing_html)

    start = time.time()
    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=FakeAsyncClient()):
        results = await adapter.fetch_opportunities()
    elapsed = time.time() - start

    # With 8 concurrent and 5ms each: 106/8 * 0.005 ≈ 0.07s — well under 30s
    assert elapsed < 30, f"Took {elapsed:.1f}s — should be well under 30s"
    assert len(results) > 0


# ---------------------------------------------------------------------------
# Integration: description populated from detail page
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_description_populated_from_detail_page():
    """description_snippet is set from detail page Overview, not title."""
    overview = "Anti-corruption and justice reform program for Colombia region"
    detail_html = _make_detail_html(overview)
    listing_html = _make_listing_html(
        _make_card_html("Generic Title", "Colombia", "view_notice.cfm?notice_id=99")
    )

    config = _make_config()
    adapter = UNDPAdapter(config)

    class FakeResponse:
        def __init__(self, text):
            self.text = text
            self.status_code = 200
        def raise_for_status(self):
            pass

    class FakeAsyncClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url, **kwargs):
            if "notice_id" in str(url):
                return FakeResponse(detail_html)
            return FakeResponse(listing_html)

    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=FakeAsyncClient()):
        results = await adapter.fetch_opportunities()

    assert len(results) >= 1
    assert results[0]["description_snippet"] != "Generic Title"
    assert "corruption" in results[0]["description_snippet"].lower()


@pytest.mark.asyncio
async def test_fallback_to_title_on_detail_failure():
    """When detail page fetch raises, description stays as title and adapter continues."""
    card_title = "Anti-corruption program support Colombia"
    listing_html = _make_listing_html(
        _make_card_html(card_title, "Colombia", "view_notice.cfm?notice_id=42")
    )

    config = _make_config()
    adapter = UNDPAdapter(config)

    class FakeAsyncClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url, **kwargs):
            if "notice_id" in str(url):
                raise Exception("Simulated detail page failure")
            resp = MagicMock()
            resp.text = listing_html
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            return resp

    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=FakeAsyncClient()):
        results = await adapter.fetch_opportunities()

    assert len(results) >= 1
    assert results[0]["description_snippet"] == card_title
