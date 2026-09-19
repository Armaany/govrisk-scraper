"""Deterministic tests for the UNDP listing-page retry policy.

Scenarios (contract §3 / §6):
  1. First listing timeout; second attempt succeeds.
  2. Connection failure retries and succeeds.
  3. HTTP 429 respects Retry-After, capped at 10s and the remaining deadline.
  4. HTTP 5xx retries and can recover.
  5. Permanent HTTP 404 performs exactly one attempt and raises.
  6. Three transient failures raise after exactly three attempts.
  7. The 75-second deadline bounds the listing phase.
  8. Listing retries/sleeps never acquire or hold the detail semaphore.
  9. Structurally missing listing table raises response_parse.
 10. Present listing table with zero cards returns [].

Time, sleep, and jitter are patched so the suite is fast and deterministic.
All HTTP is faked; no network access occurs.
"""
import asyncio
from unittest.mock import MagicMock, patch

import httpx
import pytest

from portals.errors import (
    CATEGORY_CONNECTION,
    CATEGORY_HTTP_STATUS,
    CATEGORY_RESPONSE_PARSE,
    CATEGORY_TIMEOUT,
    OP_LISTING_FETCH,
    OP_RESPONSE_PARSE,
    PortalFetchError,
)
from portals.undp_adapter import (
    UNDPAdapter,
    _LISTING_MAX_ATTEMPTS,
    _LISTING_MAX_RETRY_SLEEP,
    _LISTING_PHASE_DEADLINE,
    _listing_status_is_retryable,
)


# ---------------------------------------------------------------------------
# Helpers (mirror the FakeClient/FakeResponse style used elsewhere)
# ---------------------------------------------------------------------------

def _make_config():
    cfg = MagicMock()
    cfg.undp_enabled = True
    cfg.sector_keywords = ["corruption", "transparency", "governance"]
    cfg.target_countries = ["Colombia", "Brazil"]
    cfg.max_results = 50
    return cfg


def _card_html(title, country="Colombia", href="view_notice.cfm?notice_id=1",
               deadline="30-Dec-26"):
    return f"""<a class="vacanciesTableLink vacanciesTable__row region_RLA" href="{href}">
      <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">Title</div><span>{title}</span></div>
      <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">Ref No</div><span>T-001</span></div>
      <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">UNDP Office/Country</div><span>UNDP-COL/{country.upper()}</span></div>
      <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">Process</div><span>RFP</span></div>
      <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">Deadline</div><span>{deadline}</span></div>
    </a>"""


def _listing_html(cards="", with_table=True):
    if not with_table:
        return "<html><body><div class='other'>no table here</div></body></html>"
    return f"""<html><body><div class="vacanciesTable">
        <div class="vacanciesTable__header"></div>{cards}
    </div></body></html>"""


def _detail_html(text="Governance and transparency reform program"):
    return f"""<html><body><main>
      <div class="postContent"><h2>Overview</h2><p>{text}</p></div>
    </main></body></html>"""


class FakeResponse:
    def __init__(self, text="", status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=MagicMock(), response=self
            )


class FakeClient:
    """Shared fake AsyncClient; routes listing vs detail via URL, tracks calls."""

    def __init__(self, listing_handler, detail_response=None):
        self._listing_handler = listing_handler
        self._detail_response = detail_response or FakeResponse(_detail_html())
        self.listing_calls = 0
        self.request_log = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url, **kwargs):
        url = str(url)
        self.request_log.append(url)
        if "notice_id" in url:
            return self._detail_response
        # listing request
        self.listing_calls += 1
        result = self._listing_handler(self.listing_calls)
        if isinstance(result, Exception):
            raise result
        return result


def _patched_run(adapter, fake_client, sleep_recorder=None):
    """Run adapter.fetch_opportunities() with deterministic sleep/jitter."""
    async def _fake_sleep(secs):
        if sleep_recorder is not None:
            sleep_recorder.append(secs)

    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=fake_client), \
         patch("portals.undp_adapter.random.uniform", return_value=0), \
         patch("portals.undp_adapter.asyncio.sleep", side_effect=_fake_sleep):
        return asyncio.run(adapter.fetch_opportunities())


# ---------------------------------------------------------------------------
# 1. First timeout, then success
# ---------------------------------------------------------------------------

def test_listing_timeout_then_success():
    def handler(call_n):
        if call_n == 1:
            return httpx.ReadTimeout("listing timed out")
        return FakeResponse(_listing_html(_card_html("Corruption reform")))

    adapter = UNDPAdapter(_make_config())
    client = FakeClient(handler)
    sleeps = []
    results = _patched_run(adapter, client, sleeps)

    assert client.listing_calls == 2
    assert len(results) >= 1
    # One backoff sleep occurred before the retry (schedule[0] == 1.0, jitter 0).
    assert sleeps == [1.0]


# ---------------------------------------------------------------------------
# 2. Connection failure, then success
# ---------------------------------------------------------------------------

def test_listing_connection_failure_then_success():
    def handler(call_n):
        if call_n == 1:
            return httpx.ConnectError("cannot connect")
        return FakeResponse(_listing_html(_card_html("Transparency initiative")))

    adapter = UNDPAdapter(_make_config())
    client = FakeClient(handler)
    results = _patched_run(adapter, client, [])

    assert client.listing_calls == 2
    assert len(results) >= 1


# ---------------------------------------------------------------------------
# 3. HTTP 429 respects Retry-After, capped at 10s and remaining deadline
# ---------------------------------------------------------------------------

def test_listing_429_respects_retry_after_capped():
    def handler(call_n):
        if call_n == 1:
            # Retry-After far larger than the 10s cap.
            return FakeResponse("", status_code=429, headers={"Retry-After": "600"})
        return FakeResponse(_listing_html(_card_html("Governance reform")))

    adapter = UNDPAdapter(_make_config())
    client = FakeClient(handler)
    sleeps = []
    results = _patched_run(adapter, client, sleeps)

    assert client.listing_calls == 2
    assert len(results) >= 1
    # Retry-After 600 is capped at the 10s max retry sleep.
    assert sleeps == [_LISTING_MAX_RETRY_SLEEP]


# ---------------------------------------------------------------------------
# 4. HTTP 5xx retries and recovers
# ---------------------------------------------------------------------------

def test_listing_5xx_retries_and_recovers():
    def handler(call_n):
        if call_n == 1:
            return FakeResponse("", status_code=503)
        return FakeResponse(_listing_html(_card_html("Anti-corruption program")))

    adapter = UNDPAdapter(_make_config())
    client = FakeClient(handler)
    results = _patched_run(adapter, client, [])

    assert client.listing_calls == 2
    assert len(results) >= 1


@pytest.mark.parametrize("adversarial_status", [501, 599])
def test_listing_uncommon_5xx_retries_and_recovers(adversarial_status):
    """Adversarial: a 5xx code OUTSIDE the old {500,502,503,504} subset (e.g.
    501, 599) must still be retried. Fails once, then succeeds → 2 attempts."""
    def handler(call_n):
        if call_n == 1:
            return FakeResponse("", status_code=adversarial_status)
        return FakeResponse(_listing_html(_card_html("Governance reform")))

    adapter = UNDPAdapter(_make_config())
    client = FakeClient(handler)
    results = _patched_run(adapter, client, [])

    assert client.listing_calls == 2, (
        f"HTTP {adversarial_status} should be retryable (2 attempts), "
        f"got {client.listing_calls}"
    )
    assert len(results) >= 1


def test_listing_status_retryable_predicate():
    """Every 5xx plus 429 is retryable; other 4xx are not."""
    assert _listing_status_is_retryable(429)
    for status in (500, 501, 502, 503, 504, 505, 550, 599):
        assert _listing_status_is_retryable(status), status
    for status in (400, 401, 403, 404, 405, 410, 418, 451):
        assert not _listing_status_is_retryable(status), status
    # 2xx/3xx are handled as success, not "retryable" failures.
    assert not _listing_status_is_retryable(200)
    assert not _listing_status_is_retryable(302)


# ---------------------------------------------------------------------------
# 5. Permanent 404 — exactly one attempt, raises
# ---------------------------------------------------------------------------

def test_listing_404_single_attempt_raises():
    def handler(call_n):
        return FakeResponse("", status_code=404)

    adapter = UNDPAdapter(_make_config())
    client = FakeClient(handler)

    with pytest.raises(PortalFetchError) as excinfo:
        _patched_run(adapter, client, [])

    err = excinfo.value
    assert client.listing_calls == 1  # no retry for non-retryable 4xx
    assert err.operation == OP_LISTING_FETCH
    assert err.category == CATEGORY_HTTP_STATUS
    assert err.http_status == 404
    assert err.attempts == 1
    assert "non-retryable HTTP 404" in str(err)


# ---------------------------------------------------------------------------
# 6. Three transient failures raise after exactly three attempts
# ---------------------------------------------------------------------------

def test_listing_three_transient_failures_exhaust():
    def handler(call_n):
        return httpx.ReadTimeout("always timing out")

    adapter = UNDPAdapter(_make_config())
    client = FakeClient(handler)
    sleeps = []

    with pytest.raises(PortalFetchError) as excinfo:
        _patched_run(adapter, client, sleeps)

    err = excinfo.value
    assert client.listing_calls == _LISTING_MAX_ATTEMPTS == 3
    assert err.category == CATEGORY_TIMEOUT
    assert err.attempts == 3
    # Two backoffs between three attempts: schedule 1.0 then 2.0.
    assert sleeps == [1.0, 2.0]


# ---------------------------------------------------------------------------
# 7. The 75s deadline bounds the listing phase
# ---------------------------------------------------------------------------

def test_listing_deadline_bounds_phase():
    """With a monotonic clock advanced past the 75s deadline on the second
    tick, the phase stops and raises rather than making all 3 attempts."""
    def handler(call_n):
        return httpx.ConnectError("down")

    adapter = UNDPAdapter(_make_config())
    client = FakeClient(handler)

    # Deterministic fake clock: jumps beyond the deadline after the first attempt.
    ticks = iter([0.0, 0.0, _LISTING_PHASE_DEADLINE + 1, _LISTING_PHASE_DEADLINE + 1,
                  _LISTING_PHASE_DEADLINE + 1, _LISTING_PHASE_DEADLINE + 1])

    class _Loop:
        def time(self):
            try:
                return next(ticks)
            except StopIteration:
                return _LISTING_PHASE_DEADLINE + 1

    async def _fake_sleep(secs):
        pass

    loop = asyncio.new_event_loop()
    try:
        with patch("portals.undp_adapter.httpx.AsyncClient", return_value=client), \
             patch("portals.undp_adapter.random.uniform", return_value=0), \
             patch("portals.undp_adapter.asyncio.sleep", side_effect=_fake_sleep), \
             patch("portals.undp_adapter.asyncio.get_event_loop", return_value=_Loop()):
            with pytest.raises(PortalFetchError) as excinfo:
                loop.run_until_complete(adapter.fetch_opportunities())
    finally:
        loop.close()

    err = excinfo.value
    assert err.operation == OP_LISTING_FETCH
    # Deadline curtailed the phase before exhausting all 3 attempts.
    assert client.listing_calls < _LISTING_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# 8. Listing retries/sleeps never acquire or hold the detail semaphore
# ---------------------------------------------------------------------------

def test_listing_retry_never_touches_detail_semaphore():
    """A tracking Semaphore records acquisitions. During a listing-only failure
    (which retries and then raises), the detail semaphore is NEVER acquired,
    because the adapter raises before reaching detail enrichment."""
    acquisitions = {"count": 0}
    real_semaphore_cls = asyncio.Semaphore

    class TrackingSemaphore(real_semaphore_cls):
        async def acquire(self):
            acquisitions["count"] += 1
            return await super().acquire()

    def handler(call_n):
        return httpx.ReadTimeout("listing down")

    adapter = UNDPAdapter(_make_config())
    client = FakeClient(handler)

    async def _fake_sleep(secs):
        pass

    with patch("portals.undp_adapter.httpx.AsyncClient", return_value=client), \
         patch("portals.undp_adapter.random.uniform", return_value=0), \
         patch("portals.undp_adapter.asyncio.sleep", side_effect=_fake_sleep), \
         patch("portals.undp_adapter.asyncio.Semaphore", TrackingSemaphore):
        with pytest.raises(PortalFetchError):
            asyncio.run(adapter.fetch_opportunities())

    assert client.listing_calls == _LISTING_MAX_ATTEMPTS
    # The detail semaphore was never acquired during the listing-phase failure.
    assert acquisitions["count"] == 0


# ---------------------------------------------------------------------------
# 9. Structurally missing listing table raises response_parse
# ---------------------------------------------------------------------------

def test_listing_missing_table_raises_response_parse():
    def handler(call_n):
        return FakeResponse(_listing_html(with_table=False))

    adapter = UNDPAdapter(_make_config())
    client = FakeClient(handler)

    with pytest.raises(PortalFetchError) as excinfo:
        _patched_run(adapter, client, [])

    err = excinfo.value
    assert client.listing_calls == 1  # HTTP succeeded; no retry
    assert err.operation == OP_RESPONSE_PARSE
    assert err.category == CATEGORY_RESPONSE_PARSE


# ---------------------------------------------------------------------------
# 10. Present table with zero cards returns []
# ---------------------------------------------------------------------------

def test_listing_present_table_zero_cards_returns_empty():
    def handler(call_n):
        return FakeResponse(_listing_html(cards=""))  # table present, no cards

    adapter = UNDPAdapter(_make_config())
    client = FakeClient(handler)
    results = _patched_run(adapter, client, [])

    assert client.listing_calls == 1
    assert results == []
