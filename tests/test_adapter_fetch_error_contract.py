"""Adapter fetch-error contract + credential-safety tests.

These tests prove:
  - Devex/World Bank/Grants.gov/SAM.gov/Perplexity caught fetch failures raise
    PortalFetchError (not []); genuine empty responses still return [].
  - Perplexity whole-response parse failure raises the typed error.
  - Devex authentication raises a safe typed error retaining remediation,
    closes Playwright, and does not send its own alert.
  - Conspicuous sentinel credentials (Devex password, Perplexity bearer key,
    SAM.gov query-string api_key) constructed INTO the underlying exception's
    message / request URL / headers never reach the wrapped PortalFetchError
    string, its repr, or the orchestrator's alert text.
  - The real main.run_scraper() path sends exactly one safe component-specific
    alert, records errors=1, performs no failed-adapter LLM/write, and still
    runs a later healthy adapter.

All collaborators are mocked/faked. No real portal/Google/Anthropic/Perplexity/
SAM.gov/email requests are made.
"""
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from config import Config
from portals.devex_adapter import DevexAdapter
from portals.devex_auth import AuthenticationError
from portals.errors import (
    CATEGORY_AUTHENTICATION,
    CATEGORY_RESPONSE_PARSE,
    OP_AUTHENTICATION,
    OP_RESPONSE_PARSE,
    PortalFetchError,
)
from portals.perplexity_adapter import PerplexityAdapter
from portals.samgov_adapter import SAMGovAdapter
from portals.usaid_adapter import USAIDAdapter
from portals.worldbank_adapter import WorldBankAdapter


# Conspicuous sentinels — must NEVER appear in user-visible error text/alerts.
SENTINEL_DEVEX_PW = "SENTINEL_DEVEX_PW_9931"
SENTINEL_PPLX_KEY = "SENTINEL_PPLX_KEY_7788"
SENTINEL_SAM_KEY = "SENTINEL_SAM_KEY_5522"
SENTINEL_DEVEX_EMAIL = "sentinel-user@example.invalid"


def make_config(**kwargs) -> Config:
    defaults = dict(
        devex_email=SENTINEL_DEVEX_EMAIL,
        devex_password=SENTINEL_DEVEX_PW,
        sector_keywords=["governance", "risk"],
        target_countries=["Colombia", "Brazil"],
        max_results=25,
        notification_email="notify@example.com",
        admin_alert_email="admin@example.com",
        samgov_enabled=True,
        samgov_api_key=SENTINEL_SAM_KEY,
        perplexity_enabled=True,
        perplexity_api_key=SENTINEL_PPLX_KEY,
    )
    defaults.update(kwargs)
    return Config(**defaults)


def _assert_no_secrets(text: str) -> None:
    """Assert none of the sentinel secrets nor obvious secret markers appear."""
    for sentinel in (SENTINEL_DEVEX_PW, SENTINEL_PPLX_KEY, SENTINEL_SAM_KEY):
        assert sentinel not in text, f"sentinel leaked: {sentinel!r} in {text!r}"
    assert "Authorization" not in text
    assert "Bearer" not in text
    assert "api_key=" not in text


def _async_client(mock_response=None, get_side_effect=None, post_side_effect=None):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    if get_side_effect is not None:
        client.get = AsyncMock(side_effect=get_side_effect)
    elif mock_response is not None:
        client.get = AsyncMock(return_value=mock_response)
    if post_side_effect is not None:
        client.post = AsyncMock(side_effect=post_side_effect)
    elif mock_response is not None:
        client.post = AsyncMock(return_value=mock_response)
    return client


# ---------------------------------------------------------------------------
# Scenario 11: Devex authentication — safe typed error, remediation retained,
# Playwright closed, NO adapter-level alert. (Credential-safety test.)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_devex_auth_safe_typed_error_no_alert_and_closes():
    config = make_config()
    adapter = DevexAdapter(config)

    mock_auth = AsyncMock()
    # Underlying cause message carries BOTH sentinels (email + password).
    mock_auth.load_session.side_effect = AuthenticationError(
        f"login failed for {SENTINEL_DEVEX_EMAIL} using password {SENTINEL_DEVEX_PW}"
    )
    mock_auth.close = AsyncMock()

    notifier = MagicMock()

    with patch("portals.devex_adapter.DevexAuth", return_value=mock_auth):
        with pytest.raises(PortalFetchError) as excinfo:
            await adapter.fetch_opportunities()

    err = excinfo.value
    assert err.operation == OP_AUTHENTICATION
    assert err.category == CATEGORY_AUTHENTICATION
    # Remediation preserved; secrets absent from message and repr.
    assert "check DEVEX_EMAIL and DEVEX_PASSWORD" in str(err)
    _assert_no_secrets(str(err))
    _assert_no_secrets(repr(err))
    assert SENTINEL_DEVEX_EMAIL not in str(err)
    # Original cause preserved for debugging.
    assert isinstance(err.__cause__, AuthenticationError)
    # Playwright closed.
    mock_auth.close.assert_awaited_once()
    # Adapter did NOT send its own alert (orchestrator owns alerting).
    notifier.send_error_alert.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 12: WB / Grants.gov / SAM.gov / Perplexity caught fetch failures
# raise PortalFetchError instead of returning []. (+ credential safety)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_samgov_fetch_failure_raises_and_hides_api_key():
    config = make_config()
    adapter = SAMGovAdapter(config)

    # Build an HTTPStatusError whose request URL embeds the sentinel api_key.
    leaky_url = f"https://api.sam.gov/opportunities/v2/search?api_key={SENTINEL_SAM_KEY}&q=x"
    request = httpx.Request("GET", leaky_url)
    response = httpx.Response(403, request=request)

    def _raise_status():
        raise httpx.HTTPStatusError("403 Forbidden", request=request, response=response)

    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.raise_for_status.side_effect = _raise_status

    client = _async_client(mock_response=mock_response)

    with patch("portals.samgov_adapter.httpx.AsyncClient", return_value=client):
        with pytest.raises(PortalFetchError) as excinfo:
            await adapter.fetch_opportunities()

    err = excinfo.value
    assert err.portal == "SAM.gov"
    assert err.http_status == 403
    _assert_no_secrets(str(err))
    _assert_no_secrets(repr(err))


def test_worldbank_fetch_failure_raises_not_empty():
    import requests

    config = make_config()
    adapter = WorldBankAdapter(config)

    with patch(
        "portals.worldbank_adapter.requests.get",
        side_effect=requests.exceptions.ConnectionError(
            f"failed to connect (token={SENTINEL_SAM_KEY})"
        ),
    ):
        with pytest.raises(PortalFetchError) as excinfo:
            asyncio.run(adapter.fetch_opportunities())

    err = excinfo.value
    assert err.portal == "World Bank"
    assert err.operation == "listing_fetch"
    _assert_no_secrets(str(err))
    assert isinstance(err.__cause__, requests.exceptions.ConnectionError)


def test_grantsgov_fetch_failure_raises_not_empty():
    import requests

    config = make_config()
    adapter = USAIDAdapter(config)

    with patch(
        "portals.usaid_adapter.requests.post",
        side_effect=requests.exceptions.Timeout("read timed out"),
    ):
        with pytest.raises(PortalFetchError) as excinfo:
            asyncio.run(adapter.fetch_opportunities())

    err = excinfo.value
    assert err.portal == "Grants.gov"
    assert err.category == "timeout"


@pytest.mark.asyncio
async def test_perplexity_fetch_failure_raises_and_hides_bearer():
    config = make_config()
    adapter = PerplexityAdapter(config)

    # HTTPStatusError whose request carries the Authorization: Bearer header.
    request = httpx.Request(
        "POST",
        "https://api.perplexity.ai/chat/completions",
        headers={"Authorization": f"Bearer {SENTINEL_PPLX_KEY}"},
    )
    response = httpx.Response(401, request=request)

    def _raise_status():
        raise httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)

    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.raise_for_status.side_effect = _raise_status

    client = _async_client(mock_response=mock_response)

    with patch("portals.perplexity_adapter.httpx.AsyncClient", return_value=client):
        with pytest.raises(PortalFetchError) as excinfo:
            await adapter.fetch_opportunities()

    err = excinfo.value
    assert err.portal == "Perplexity"
    assert err.http_status == 401
    _assert_no_secrets(str(err))
    _assert_no_secrets(repr(err))


# ---------------------------------------------------------------------------
# Scenario 14: Perplexity whole-response parse failure raises typed error.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_perplexity_parse_failure_raises_typed():
    config = make_config()
    adapter = PerplexityAdapter(config)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "not valid json at all"}}]
    }
    client = _async_client(mock_response=mock_response)

    with patch("portals.perplexity_adapter.httpx.AsyncClient", return_value=client):
        with pytest.raises(PortalFetchError) as excinfo:
            await adapter.fetch_opportunities()

    err = excinfo.value
    assert err.operation == OP_RESPONSE_PARSE
    assert err.category == CATEGORY_RESPONSE_PARSE


# ---------------------------------------------------------------------------
# Scenario 13: genuine successful empty responses still return [].
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_samgov_genuine_empty_returns_empty_list():
    config = make_config()
    adapter = SAMGovAdapter(config)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"opportunitiesData": []}
    client = _async_client(mock_response=mock_response)

    with patch("portals.samgov_adapter.httpx.AsyncClient", return_value=client):
        result = await adapter.fetch_opportunities()

    assert result == []


@pytest.mark.asyncio
async def test_perplexity_genuine_empty_returns_empty_list():
    config = make_config()
    adapter = PerplexityAdapter(config)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"choices": [{"message": {"content": "[]"}}]}
    client = _async_client(mock_response=mock_response)

    with patch("portals.perplexity_adapter.httpx.AsyncClient", return_value=client):
        result = await adapter.fetch_opportunities()

    assert result == []


def test_worldbank_genuine_empty_returns_empty_list():
    config = make_config()
    adapter = WorldBankAdapter(config)

    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"procnotices": []}

    with patch("portals.worldbank_adapter.requests.get", return_value=mock_response):
        result = asyncio.run(adapter.fetch_opportunities())

    assert result == []


# ---------------------------------------------------------------------------
# Scenario 15 + 16: real main.run_scraper() path — exactly one safe
# component-specific alert, errors=1, no failed-adapter LLM/write, later
# healthy adapter still processed; secret-bearing exception never reaches the
# notifier alert text.
# ---------------------------------------------------------------------------

def _run_orchestrator_with_adapters(fake_adapters):
    """Invoke the real main.run_scraper() with external collaborators mocked.

    Returns (audit_mock, notifier_mock, interpreter_mock, store_mock,
    filtered_opps).
    """
    import main

    filtered_opps = []

    cfg = MagicMock()
    cfg.run_mode = "live"
    cfg.store_type = "sheets"
    cfg.max_results = 10

    store = MagicMock()
    store.test_connection.return_value = True
    store.get_all_links.return_value = set()

    def _record_and_pass(opp):
        filtered_opps.append(opp)
        return True  # let healthy opp flow to LLM + write

    kf = MagicMock()
    kf.passes_filter.side_effect = _record_and_pass
    kf.get_matched_keywords.return_value = ["governance"]

    audit = MagicMock()
    notifier = MagicMock()
    interpreter = MagicMock()
    interpreter.interpret.return_value = {
        "summary": "s", "relevance_score": "high", "relevance_reason": "r",
        "bid_recommendation": "pursue", "risk_flags": None, "llm_confidence": "high",
    }

    async def _fake_registry(config):
        return list(fake_adapters)

    with patch("main.load_config", return_value=cfg), \
         patch("main.SheetsAdapter", return_value=store), \
         patch("main.AirtableAdapter", return_value=store), \
         patch("main.AuditLogger", return_value=audit), \
         patch("main.Notifier", return_value=notifier), \
         patch("main.KeywordFilter", return_value=kf), \
         patch("main.LLMInterpreter", return_value=interpreter), \
         patch("main.build_adapter_registry", side_effect=_fake_registry):
        asyncio.run(main.run_scraper())

    return audit, notifier, interpreter, store, filtered_opps


def test_orchestrator_one_safe_alert_errors_one_and_continues():
    # Failing adapter raises a PortalFetchError whose CAUSE carries a sentinel.
    failing = MagicMock()
    failing.portal_name = "undp"

    async def _boom():
        try:
            raise TimeoutError(
                f"read timeout https://x/y?api_key={SENTINEL_SAM_KEY} "
                f"Authorization: Bearer {SENTINEL_PPLX_KEY}"
            )
        except TimeoutError as exc:
            from portals.errors import (
                CATEGORY_TIMEOUT,
                OP_LISTING_FETCH,
                PortalFetchError,
            )
            raise PortalFetchError(
                "UNDP", OP_LISTING_FETCH, CATEGORY_TIMEOUT, attempts=3,
                reason=type(exc).__name__,
            ) from exc

    failing.fetch_opportunities = AsyncMock(side_effect=_boom)

    healthy_opp = {
        "opportunity_id": "healthy-1",
        "opportunity_title": "Healthy governance opp",
        "opportunity_link": "https://healthy-1.example.com",
        "source_portal": "samgov",
        "matched_keywords": [],
        "deadline": None,
        "contract_value": None,
        "country_region": "Colombia",
        "description_snippet": "desc",
    }
    healthy = MagicMock()
    healthy.portal_name = "samgov"
    healthy.fetch_opportunities = AsyncMock(return_value=[healthy_opp])

    audit, notifier, interpreter, store, filtered = _run_orchestrator_with_adapters(
        [failing, healthy]  # failure BEFORE healthy
    )

    # Both adapters awaited once; later healthy adapter ran.
    assert failing.fetch_opportunities.await_count == 1
    assert healthy.fetch_opportunities.await_count == 1

    # Exactly one alert, component-specific to the failing adapter.
    assert notifier.send_error_alert.call_count == 1
    call = notifier.send_error_alert.call_args_list[0]
    assert call.kwargs.get("component") == "undp"
    alert_text = call.args[0]
    # Alert text includes portal, operation, category/reason, attempts.
    assert "UNDP" in alert_text
    assert "listing_fetch" in alert_text
    assert "timeout" in alert_text
    assert "3 attempts" in alert_text
    # And NO secret material.
    _assert_no_secrets(alert_text)

    # errors == 1 in the completion summary.
    summary_calls = notifier.send_completion_summary.call_args_list
    assert len(summary_calls) == 1
    assert summary_calls[0].kwargs.get("errors") == 1

    # No failed-adapter LLM/write: only the ONE healthy opportunity is processed.
    assert interpreter.interpret.call_count == 1
    assert store.write_record.call_count == 1
    assert filtered == [healthy_opp]


# ---------------------------------------------------------------------------
# Blocker-2 regression: LOG credential safety. The prior _log_error /
# _log_auth_error / _log_parse_error interpolated the raw exception and used
# exc_info=exc, leaking secret-bearing URLs/messages into logs. These tests
# assert no sentinel appears anywhere in the captured log text, while the
# expected typed error still propagates.
#
# pytest's `caplog` captures records from the root logger by propagation; we
# raise the capture level to DEBUG so nothing is filtered out.
# ---------------------------------------------------------------------------

def _assert_no_secrets_in_logs(caplog) -> None:
    # Full formatted text (message only) ...
    text = caplog.text
    _assert_no_secrets(text)
    assert SENTINEL_DEVEX_EMAIL not in text
    # ... and every record's rendered message + any attached exception text.
    for record in caplog.records:
        rendered = record.getMessage()
        for sentinel in (SENTINEL_DEVEX_PW, SENTINEL_PPLX_KEY, SENTINEL_SAM_KEY,
                         SENTINEL_DEVEX_EMAIL):
            assert sentinel not in rendered, f"sentinel leaked in log message: {sentinel!r}"
        # No secret-bearing traceback should be attached (exc_info must be None).
        assert record.exc_info is None, (
            "expected-failure logs must not attach exc_info (secret-bearing traceback)"
        )


@pytest.mark.asyncio
async def test_devex_auth_failure_logs_carry_no_secrets(caplog):
    """Devex auth failure: the sentinel email/password in the raw
    AuthenticationError message must never reach the logs."""
    config = make_config()
    adapter = DevexAdapter(config)

    mock_auth = AsyncMock()
    mock_auth.load_session.side_effect = AuthenticationError(
        f"login failed for {SENTINEL_DEVEX_EMAIL} using password {SENTINEL_DEVEX_PW}"
    )
    mock_auth.close = AsyncMock()

    with patch("portals.devex_adapter.DevexAuth", return_value=mock_auth):
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(PortalFetchError):
                await adapter.fetch_opportunities()

    _assert_no_secrets_in_logs(caplog)


def test_worldbank_connection_failure_logs_carry_no_secrets(caplog):
    """World Bank connection error whose message embeds a secret-bearing URL
    must not leak the sentinel into the logs."""
    import requests

    config = make_config()
    adapter = WorldBankAdapter(config)

    with patch(
        "portals.worldbank_adapter.requests.get",
        side_effect=requests.exceptions.ConnectionError(
            f"failed to connect to https://wb/api?api_key={SENTINEL_SAM_KEY}"
        ),
    ):
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(PortalFetchError):
                asyncio.run(adapter.fetch_opportunities())

    _assert_no_secrets_in_logs(caplog)


@pytest.mark.asyncio
async def test_samgov_timeout_logs_carry_no_secrets(caplog):
    """SAM.gov timeout whose underlying request URL carries the api_key must not
    leak the sentinel into the logs (the api_key lives in the query string)."""
    config = make_config()
    adapter = SAMGovAdapter(config)

    leaky_url = f"https://api.sam.gov/opportunities/v2/search?api_key={SENTINEL_SAM_KEY}&q=x"
    request = httpx.Request("GET", leaky_url)

    client = _async_client(
        get_side_effect=httpx.ReadTimeout(
            f"timed out for {leaky_url}", request=request
        )
    )

    with patch("portals.samgov_adapter.httpx.AsyncClient", return_value=client):
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(PortalFetchError):
                await adapter.fetch_opportunities()

    _assert_no_secrets_in_logs(caplog)


@pytest.mark.asyncio
async def test_samgov_connection_failure_logs_carry_no_secrets(caplog):
    """SAM.gov connection error carrying the secret-bearing URL must not leak."""
    config = make_config()
    adapter = SAMGovAdapter(config)

    leaky_url = f"https://api.sam.gov/opportunities/v2/search?api_key={SENTINEL_SAM_KEY}&q=x"
    request = httpx.Request("GET", leaky_url)

    client = _async_client(
        get_side_effect=httpx.ConnectError(
            f"cannot connect: {leaky_url}", request=request
        )
    )

    with patch("portals.samgov_adapter.httpx.AsyncClient", return_value=client):
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(PortalFetchError):
                await adapter.fetch_opportunities()

    _assert_no_secrets_in_logs(caplog)


@pytest.mark.asyncio
async def test_perplexity_timeout_logs_carry_no_secrets(caplog):
    """Perplexity timeout whose request carries the Authorization: Bearer header
    must not leak the bearer token into the logs."""
    config = make_config()
    adapter = PerplexityAdapter(config)

    request = httpx.Request(
        "POST",
        "https://api.perplexity.ai/chat/completions",
        headers={"Authorization": f"Bearer {SENTINEL_PPLX_KEY}"},
    )

    client = _async_client(
        post_side_effect=httpx.ReadTimeout(
            f"timed out; auth Bearer {SENTINEL_PPLX_KEY}", request=request
        )
    )

    with patch("portals.perplexity_adapter.httpx.AsyncClient", return_value=client):
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(PortalFetchError):
                await adapter.fetch_opportunities()

    _assert_no_secrets_in_logs(caplog)


# ---------------------------------------------------------------------------
# Blocker-3 regression: PerplexityAdapter.response.json() failure is wrapped in
# typed response_parse handling (chained, credential-safe).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_perplexity_response_json_raises_is_typed_and_safe(caplog):
    config = make_config()
    adapter = PerplexityAdapter(config)

    # A 200 response whose .json() itself raises (invalid JSON body). The raw
    # decode error message carries the bearer sentinel to prove non-leakage.
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status.return_value = None
    json_error = ValueError(
        f"Expecting value: line 1 (body leaked Bearer {SENTINEL_PPLX_KEY})"
    )
    mock_response.json.side_effect = json_error

    client = _async_client(mock_response=mock_response)

    with patch("portals.perplexity_adapter.httpx.AsyncClient", return_value=client):
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(PortalFetchError) as excinfo:
                await adapter.fetch_opportunities()

    err = excinfo.value
    assert err.portal == "Perplexity"
    assert err.operation == OP_RESPONSE_PARSE
    assert err.category == CATEGORY_RESPONSE_PARSE
    # Safe string; secret absent.
    _assert_no_secrets(str(err))
    # Cause preserved via chaining.
    assert err.__cause__ is json_error
    # And nothing leaked into the logs.
    _assert_no_secrets_in_logs(caplog)
