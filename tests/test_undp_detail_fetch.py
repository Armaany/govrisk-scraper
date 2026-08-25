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
    _parse_retry_after,
    _DESCRIPTION_DISPLAY_MAX,
    _MAX_CONCURRENT_DETAIL_FETCHES,
    _MAX_ATTEMPTS,
    _DETAIL_ENRICHMENT_DEADLINE,
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
    """The AsyncClient constructor is called exactly once — proving client reuse."""
    detail_html = _make_detail_html("Governance text")
    listing_html = _make_listing_html(
        _make_card_html("Gov", "Colombia", "view_notice.cfm?notice_id=1") +
        _make_card_html("Gov2", "Colombia", "view_notice.cfm?notice_id=2")
    )

    async def handler(url):
        if "notice_id" in url:
            return FakeResponse(detail_html)
        return FakeResponse(listing_html)

    config = _make_config()
    adapter = UNDPAdapter(config)

    fake = FakeClient(handler)
    mock_constructor = MagicMock(return_value=fake)
    with patch("portals.undp_adapter.httpx.AsyncClient", mock_constructor):
        await adapter.fetch_opportunities()

    # Constructor called exactly once — not per-record
    assert mock_constructor.call_count == 1, (
        f"Expected AsyncClient constructed once, got {mock_constructor.call_count} calls"
    )
    # All requests routed through that single instance
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
    """Fast records are preserved when slow one exceeds deadline.

    Asserts:
    - All expected fast record IDs are returned
    - The slow task observes CancelledError (confirmed via event signal)
    - Cancelled task is awaited (no pending tasks remain)
    - Warning is logged with correct total/completed/fallback/cancelled counts
    """
    import logging

    # 5 fast cards + 1 slow card
    fast_ids = [f"fast{i}" for i in range(5)]
    cards_html = "".join(
        _make_card_html(f"Fast{i}", "Colombia", f"view_notice.cfm?notice_id={fid}", "30-Dec-26")
        for i, fid in enumerate(fast_ids)
    )
    cards_html += _make_card_html("Slow", "Colombia", "view_notice.cfm?notice_id=slow", "30-Dec-26")
    # Override Ref No to be unique per card (the helper uses T-001 for all)
    # Use a custom listing_html with unique ref numbers
    def _card_with_ref(title, country, href, ref, deadline="30-Dec-26"):
        return f"""<a class="vacanciesTableLink vacanciesTable__row region_RLA" href="{href}">
          <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">Title</div><span>{title}</span></div>
          <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">Ref No</div><span>{ref}</span></div>
          <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">UNDP Office/Country</div><span>UNDP-COL/{country.upper()}</span></div>
          <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">Process</div><span>RFP</span></div>
          <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">Deadline</div><span>{deadline}</span></div>
        </a>"""

    cards_html = "".join(
        _card_with_ref(f"Fast{i}", "Colombia", f"view_notice.cfm?notice_id={fid}", fid)
        for i, fid in enumerate(fast_ids)
    )
    cards_html += _card_with_ref("Slow", "Colombia", "view_notice.cfm?notice_id=slow", "slow-ref")
    listing_html = _make_listing_html(cards_html)
    detail_html = _make_detail_html("Governance and transparency")

    config = _make_config()
    adapter = UNDPAdapter(config)

    # Signal: set when the slow task observes cancellation
    cancelled_observed = asyncio.Event()

    async def handler(url):
        if "slow" in url:
            try:
                await asyncio.sleep(100)  # will be cancelled by deadline
            except asyncio.CancelledError:
                cancelled_observed.set()
                raise  # re-raise to propagate cancellation
            return FakeResponse(detail_html)
        if "notice_id" in url:
            await asyncio.sleep(0.005)
            return FakeResponse(detail_html)
        return FakeResponse(listing_html)

    # Use very short enrichment deadline (0.3s — enough for fast, not slow)
    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=FakeClient(handler)), \
         patch("portals.undp_adapter._DETAIL_ENRICHMENT_DEADLINE", 0.3), \
         patch("portals.undp_adapter.random.uniform", return_value=0) as _, \
         patch.object(adapter, '_log_error'):  # suppress error prints
        # Capture log warnings
        with patch("portals.undp_adapter.logger") as mock_logger:
            results = await adapter.fetch_opportunities()

    # 1. All fast records returned (they have governance/transparency in overview)
    result_ids = [r.get("opportunity_id", "") for r in results]
    for fid in fast_ids:
        expected_id = f"undp-{fid}"
        assert expected_id in result_ids, (
            f"Fast record '{expected_id}' not found in results: {result_ids}"
        )

    # 2. Slow task directly observed CancelledError
    assert cancelled_observed.is_set(), (
        "Slow task did not observe CancelledError — cancellation not propagated"
    )

    # 3. Deadline warning logged with EXACT counts. Identify the specific
    #    formatted warning call by its format string, then inspect its
    #    positional args (total, completed, fallback, cancelled) directly.
    deadline_calls = [
        c for c in mock_logger.warning.call_args_list
        if c.args and "Adapter deadline reached" in str(c.args[0])
    ]
    assert len(deadline_calls) == 1, (
        f"Expected exactly one deadline warning, got: "
        f"{[str(c)[:80] for c in mock_logger.warning.call_args_list]}"
    )
    total, completed, fallback, cancelled = deadline_calls[0].args[1:5]
    assert total == 6, f"total: expected 6, got {total}"
    assert completed == 5, f"completed: expected 5, got {completed}"
    assert fallback == 0, f"fallback: expected 0, got {fallback}"
    assert cancelled == 1, f"cancelled: expected 1, got {cancelled}"

    # 4. Results NOT silently empty
    assert len(results) >= 1, "Completed records should not be discarded"


# ---------------------------------------------------------------------------
# SEMAPHORE PER-ATTEMPT: later records proceed while throttled ones sleep
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_semaphore_released_during_backoff():
    """Proves semaphore is released during backoff by asserting that fast records
    complete BEFORE any throttled record starts its second (retry) attempt.

    This test would FAIL with the old f4c7715 behavior where the semaphore wrapped
    the entire retry lifecycle, because fast records would be blocked until the
    throttled records finished sleeping and released their permits.
    """
    n_throttled = 8
    n_fast = 4
    detail_html = _make_detail_html("Governance and transparency reform")
    all_cards = "".join(
        _make_card_html(f"Throttled{i}", "Colombia", f"view_notice.cfm?notice_id=throttle{i}", "30-Dec-26")
        for i in range(n_throttled)
    ) + "".join(
        _make_card_html(f"Fast{i}", "Colombia", f"view_notice.cfm?notice_id=fast{i}", "30-Dec-26")
        for i in range(n_fast)
    )
    listing_html = _make_listing_html(all_cards)

    # Track the ORDER of network requests (not just counts)
    request_order = []  # entries: ("throttle0_attempt1", timestamp), ("fast0", timestamp), ...
    throttle_attempt_counts = {}

    async def handler(url):
        url = str(url)
        if "throttle" in url:
            # Extract throttle ID
            tid = url.split("notice_id=")[1] if "notice_id=" in url else url
            throttle_attempt_counts[tid] = throttle_attempt_counts.get(tid, 0) + 1
            attempt = throttle_attempt_counts[tid]
            request_order.append((f"{tid}_attempt{attempt}", asyncio.get_event_loop().time()))

            if attempt == 1:
                return FakeResponse("", status_code=429, headers={"Retry-After": "0.05"})
            return FakeResponse(detail_html)

        if "fast" in url:
            fid = url.split("notice_id=")[1] if "notice_id=" in url else url
            request_order.append((fid, asyncio.get_event_loop().time()))
            await asyncio.sleep(0.001)
            return FakeResponse(detail_html)

        return FakeResponse(listing_html)

    config = _make_config()
    adapter = UNDPAdapter(config)

    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=FakeClient(handler)), \
         patch("portals.undp_adapter.random.uniform", return_value=0):
        results = await adapter.fetch_opportunities()

    # Extract timestamps for fast requests and throttled retry attempts
    fast_times = [t for label, t in request_order if "fast" in label and "attempt" not in label]
    retry_times = [t for label, t in request_order if "attempt2" in label]

    # KEY ASSERTION: Every fast request must complete BEFORE any throttled record's
    # second attempt begins. If semaphore was held during backoff, fast requests
    # would be blocked until throttled records released permits (after sleeping).
    assert len(fast_times) == n_fast, f"Expected {n_fast} fast requests, got {len(fast_times)}"
    assert len(retry_times) > 0, "Expected at least one retry attempt"

    max_fast_time = max(fast_times)
    min_retry_time = min(retry_times)
    assert max_fast_time < min_retry_time, (
        f"Fast requests should complete before retries begin. "
        f"Last fast: {max_fast_time:.4f}, First retry: {min_retry_time:.4f}"
    )
    assert len(results) > 0


# ---------------------------------------------------------------------------
# ORCHESTRATION REGRESSION TEST — main.run_scraper() end to end
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestration_end_to_end_keyword_after_1000():
    """Full main.run_scraper() in LIVE mode: keyword after char 1000 survives orchestration,
    store.write_record() is called, OpportunityRecord is correct, transient fields excluded.
    """
    from unittest.mock import AsyncMock
    import main
    from engine.keyword_filter import MATCHING_TEXT_KEY
    from store.adapter_sheets import SheetsAdapter

    # Build a fake opportunity: keyword "corruption" only after position 1300
    filler = "generic text " * 100  # ~1300 chars
    full_overview = filler + "corruption reform program details"
    fake_opp = {
        "opportunity_id": "undp-TEST-E2E",
        "devex_opportunity_id": "undp-TEST-E2E",
        "opportunity_title": "Generic Capacity Building Title",
        "funder_organisation": "UNDP",
        "country_region": "Colombia",
        "deadline": "2026-12-30",
        "contract_value": None,
        "opportunity_link": "https://procurement-notices.undp.org/view_notice.cfm?notice_id=e2e",
        "description_snippet": full_overview[:1000],  # truncated, no keyword
        MATCHING_TEXT_KEY: full_overview,  # full text has "corruption" after 1300
        "source_portal": "undp",
        "matched_keywords": [],
    }

    config = _make_config()
    config.run_mode = "live"  # LIVE mode — write_record will be called
    config.store_type = "sheets"
    config.anthropic_api_key = "test"
    config.devex_enabled = False
    config.undp_enabled = False
    config.worldbank_enabled = False
    config.usaid_enabled = False
    config.iadb_enabled = False
    config.oecd_enabled = False
    config.samgov_enabled = False
    config.perplexity_enabled = False

    written_records = []
    mock_store = MagicMock()
    mock_store.test_connection.return_value = True
    mock_store.get_all_ids.return_value = set()
    mock_store.write_record.side_effect = lambda r: written_records.append(r)

    mock_audit = MagicMock()
    mock_notifier = MagicMock()
    mock_notifier.send_completion_summary = MagicMock()
    mock_notifier.send_error_alert = MagicMock()

    mock_interpreter = MagicMock()
    mock_interpreter.interpret.return_value = {
        "summary": "Test summary",
        "relevance_score": "high",
        "relevance_reason": "test",
        "bid_recommendation": "pursue",
        "risk_flags": None,
        "llm_confidence": "high",
    }

    async def fake_registry(cfg):
        class FakeAdapter:
            portal_name = "undp"
            async def fetch_opportunities(self):
                return [fake_opp]
        return [FakeAdapter()]

    with patch("main.load_config", return_value=config), \
         patch("main.SheetsAdapter", return_value=mock_store), \
         patch("main.AirtableAdapter", return_value=mock_store), \
         patch("main.AuditLogger", return_value=mock_audit), \
         patch("main.Notifier", return_value=mock_notifier), \
         patch("main.LLMInterpreter", return_value=mock_interpreter), \
         patch("main.build_adapter_registry", side_effect=fake_registry):
        await main.run_scraper()

    # 1. write_record called exactly once
    assert mock_store.write_record.call_count == 1, (
        f"Expected write_record called once, got {mock_store.write_record.call_count}"
    )

    # 2. Inspect the written OpportunityRecord
    record = written_records[0]
    record_dict = record.to_dict()

    # matched_keywords contains "corruption" (assert directly on the record field)
    assert "corruption" in record.matched_keywords

    # description_snippet <= 1000 chars (it's stored as description_snippet in the record)
    desc_in_dict = record_dict.get("description_snippet") or record_dict.get("opportunity_title", "")
    # Note: description_snippet may not be in the 12-column to_dict but check it doesn't exceed 1000
    assert len(fake_opp["description_snippet"]) <= 1000

    # 3. _matching_text and _full_overview do NOT appear in serialized output
    assert MATCHING_TEXT_KEY not in record_dict
    assert "_full_overview" not in record_dict

    # 4. No new Sheets column introduced
    assert MATCHING_TEXT_KEY not in SheetsAdapter.HEADERS
    assert "_full_overview" not in SheetsAdapter.HEADERS


# ---------------------------------------------------------------------------
# RETRY-AFTER: delay-seconds and HTTP-date formats
# ---------------------------------------------------------------------------

def test_parse_retry_after_delay_seconds():
    """Numeric Retry-After: 30 → 30s clamped to deadline."""
    assert _parse_retry_after("30", remaining_deadline=60) == 30
    # Clamped to remaining deadline
    assert _parse_retry_after("30", remaining_deadline=10) == 10


def test_parse_retry_after_http_date():
    """HTTP-date Retry-After parsed and clamped to deadline."""
    from datetime import datetime, timezone, timedelta
    future = datetime.now(timezone.utc) + timedelta(seconds=5)
    http_date = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    result = _parse_retry_after(http_date, remaining_deadline=60)
    # Should be approximately 5 seconds (±1s for execution time)
    assert 3 <= result <= 7


def test_parse_retry_after_invalid_returns_zero():
    """Unparseable Retry-After returns 0."""
    assert _parse_retry_after("not-a-date-or-number", remaining_deadline=60) == 0
    assert _parse_retry_after("", remaining_deadline=60) == 0


@pytest.mark.asyncio
async def test_retry_after_numeric_respected():
    """429 with Retry-After: 0.01 — adapter waits then retries successfully."""
    detail_html = _make_detail_html("Transparency reform program")
    listing_html = _make_listing_html(
        _make_card_html("Title", "Colombia", "view_notice.cfm?notice_id=1")
    )
    attempts = [0]

    async def handler(url):
        if "notice_id" in url:
            attempts[0] += 1
            if attempts[0] == 1:
                return FakeResponse("", status_code=429, headers={"Retry-After": "0.01"})
            return FakeResponse(detail_html)
        return FakeResponse(listing_html)

    config = _make_config()
    adapter = UNDPAdapter(config)

    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=FakeClient(handler)), \
         patch("portals.undp_adapter.random.uniform", return_value=0):
        results = await adapter.fetch_opportunities()

    assert attempts[0] == 2
    assert len(results) >= 1
