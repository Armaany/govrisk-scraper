"""Tests for UNDP detail-page description fetching.

Covers:
  - description_snippet is populated from detail page when fetch succeeds
  - description_snippet falls back to title when detail fetch fails (HTTP error)
  - description_snippet falls back to title when detail page has no postContent divs
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bs4 import BeautifulSoup

from portals.undp_adapter import _fetch_detail_description, UNDPAdapter, _DESCRIPTION_MAX_CHARS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config():
    """Minimal config stub — avoids loading .env."""
    cfg = MagicMock()
    cfg.undp_enabled = True
    cfg.sector_keywords = ["corruption", "transparency", "justice"]
    cfg.target_countries = ["Colombia", "Brazil", "Honduras", "Panama"]
    cfg.max_results = 50
    return cfg


def _make_detail_html(overview_text: str) -> str:
    """Build a minimal UNDP detail page HTML containing postContent divs."""
    return f"""
    <html><body>
      <main>
        <div class="postContent"><h2>Link to Atlas Project</h2> Non-UNDP Project</div>
        <div class="postContent"><h2>Documents</h2> Some file link</div>
        <div class="postContent"><h2>Overview</h2><p>{overview_text}</p></div>
      </main>
    </body></html>
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


def _make_card_html(title: str, country: str, href: str, region_class: str = "region_RLA") -> str:
    return f"""
    <a class="vacanciesTableLink vacanciesTable__row {region_class}" href="{href}">
      <div class="vacanciesTable__cell">
        <div class="vacanciesTable__cell__label">Title</div>
        <span>{title}</span>
      </div>
      <div class="vacanciesTable__cell">
        <div class="vacanciesTable__cell__label">Ref No</div>
        <span>TEST-001</span>
      </div>
      <div class="vacanciesTable__cell">
        <div class="vacanciesTable__cell__label">UNDP Office/Country</div>
        <span>UNDP-COL/{country.upper()}</span>
      </div>
      <div class="vacanciesTable__cell">
        <div class="vacanciesTable__cell__label">Process</div>
        <span>RFP</span>
      </div>
      <div class="vacanciesTable__cell">
        <div class="vacanciesTable__cell__label">Deadline</div>
        <span>30-Dec-26</span>
      </div>
    </a>
    """


# ---------------------------------------------------------------------------
# Unit tests for _fetch_detail_description
# ---------------------------------------------------------------------------

def test_fetch_detail_description_returns_overview_text():
    """When the detail page has postContent divs, returns the largest one truncated."""
    html = _make_detail_html("This is the full overview text about anti-corruption programs.")
    mock_resp = MagicMock()
    mock_resp.text = html
    mock_resp.raise_for_status = MagicMock()

    with patch("portals.undp_adapter.requests.get", return_value=mock_resp):
        result = _fetch_detail_description("https://procurement-notices.undp.org/view_notice.cfm?notice_id=1")

    assert result is not None
    assert "overview" in result.lower() or "anti-corruption" in result.lower()


def test_fetch_detail_description_truncates_to_max_chars():
    """Long descriptions are truncated to _DESCRIPTION_MAX_CHARS."""
    long_text = "x" * (_DESCRIPTION_MAX_CHARS + 500)
    html = _make_detail_html(long_text)
    mock_resp = MagicMock()
    mock_resp.text = html
    mock_resp.raise_for_status = MagicMock()

    with patch("portals.undp_adapter.requests.get", return_value=mock_resp):
        result = _fetch_detail_description("https://procurement-notices.undp.org/view_notice.cfm?notice_id=1")

    assert result is not None
    assert len(result) <= _DESCRIPTION_MAX_CHARS


def test_fetch_detail_description_returns_none_when_no_postcontent():
    """Returns None when the detail page has no postContent divs."""
    html = "<html><body><main><p>No content here</p></main></body></html>"
    mock_resp = MagicMock()
    mock_resp.text = html
    mock_resp.raise_for_status = MagicMock()

    with patch("portals.undp_adapter.requests.get", return_value=mock_resp):
        result = _fetch_detail_description("https://procurement-notices.undp.org/view_notice.cfm?notice_id=1")

    assert result is None


# ---------------------------------------------------------------------------
# Integration tests for fetch_opportunities — description enrichment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_description_populated_from_detail_page():
    """description_snippet is set to detail page content, not just the title."""
    overview_text = "This program focuses on anti-corruption and justice reform in Colombia."
    detail_html = _make_detail_html(overview_text)
    listing_html = _make_listing_html(
        _make_card_html("Some Generic Title", "Colombia", "view_notice.cfm?notice_id=999")
    )

    config = _make_config()
    adapter = UNDPAdapter(config)

    def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if "notice_id" in url:
            resp.text = detail_html
        else:
            resp.text = listing_html
        resp.status_code = 200
        return resp

    with patch("portals.undp_adapter.requests.get", side_effect=fake_get):
        results = await adapter.fetch_opportunities()

    # At least one result should have description from detail page
    assert len(results) >= 1
    desc = results[0]["description_snippet"]
    assert desc != "Some Generic Title", "description_snippet should NOT equal the title"
    assert "anti-corruption" in desc.lower() or "overview" in desc.lower()


@pytest.mark.asyncio
async def test_description_falls_back_to_title_on_detail_fetch_failure():
    """When detail page fetch raises an exception, description_snippet falls back to title."""
    import requests as req_module

    # Title must pass keyword filter: "justicia" is a sector keyword, Colombia is LATAM
    card_title = "Consultoría justicia transicional Colombia"
    listing_html = _make_listing_html(
        _make_card_html(card_title, "Colombia", "view_notice.cfm?notice_id=42")
    )

    config = _make_config()
    adapter = UNDPAdapter(config)

    def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "notice_id" in url:
            raise req_module.RequestException("connection timeout")
        resp.raise_for_status = MagicMock()
        resp.text = listing_html
        return resp

    with patch("portals.undp_adapter.requests.get", side_effect=fake_get):
        results = await adapter.fetch_opportunities()

    assert len(results) >= 1, "Expected at least 1 result — title should pass filter"
    desc = results[0]["description_snippet"]
    assert desc == card_title, (
        f"Expected title as fallback but got: {desc!r}"
    )


@pytest.mark.asyncio
async def test_description_falls_back_to_title_when_no_postcontent():
    """When detail page has no postContent, description_snippet falls back to title."""
    empty_detail_html = "<html><body><main><p>Nothing here</p></main></body></html>"
    card_title = "Consultoría gobernanza y transparencia Honduras"
    listing_html = _make_listing_html(
        _make_card_html(card_title, "Honduras", "view_notice.cfm?notice_id=77")
    )

    config = _make_config()
    adapter = UNDPAdapter(config)

    def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.status_code = 200
        resp.text = empty_detail_html if "notice_id" in url else listing_html
        return resp

    with patch("portals.undp_adapter.requests.get", side_effect=fake_get):
        results = await adapter.fetch_opportunities()

    assert len(results) >= 1, "Expected at least 1 result — title should pass filter"
    desc = results[0]["description_snippet"]
    assert desc == card_title
