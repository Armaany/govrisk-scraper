"""Unit tests for DevexAdapter error paths and resource cleanup."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from config import Config
from portals.devex_adapter import DevexAdapter
from portals.devex_auth import AuthenticationError
from portals.errors import (
    CATEGORY_AUTHENTICATION,
    OP_AUTHENTICATION,
    PortalFetchError,
)


def make_config() -> Config:
    return Config(
        devex_email="test@example.com",
        devex_password="password",
        sector_keywords=["governance"],
        target_countries=["Colombia"],
        notification_email="notify@example.com",
        admin_alert_email="admin@example.com",
    )


# ---------------------------------------------------------------------------
# AuthenticationError path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auth_error_raises_typed_error_without_adapter_alert():
    """AuthenticationError becomes a credential-safe PortalFetchError.

    The adapter no longer sends its own audit/notifier alert (the orchestrator
    owns alerting). The typed error carries the authentication category and the
    remediation phrase, but never the configured email/password.
    """
    config = make_config()
    adapter = DevexAdapter(config)

    mock_auth = AsyncMock()
    # Sentinel-bearing raw message: the actual password must not leak into str().
    mock_auth.load_session.side_effect = AuthenticationError(
        "login failed for test@example.com / password"
    )
    mock_auth.close = AsyncMock()

    with patch("portals.devex_adapter.DevexAuth", return_value=mock_auth):
        with pytest.raises(PortalFetchError) as excinfo:
            await adapter.fetch_opportunities()

    err = excinfo.value
    assert err.portal == "Devex"
    assert err.operation == OP_AUTHENTICATION
    assert err.category == CATEGORY_AUTHENTICATION
    assert err.attempts == 1
    # Remediation preserved, secrets absent.
    assert "check DEVEX_EMAIL and DEVEX_PASSWORD" in str(err)
    assert "password" not in str(err)
    assert "test@example.com" not in str(err)
    # Original cause preserved for debugging via chaining.
    assert isinstance(err.__cause__, AuthenticationError)


@pytest.mark.asyncio
async def test_auth_error_still_closes_playwright():
    """auth.close() is called even when the typed error is raised."""
    config = make_config()
    adapter = DevexAdapter(config)

    mock_auth = AsyncMock()
    mock_auth.load_session.side_effect = AuthenticationError("login failed")
    mock_auth.close = AsyncMock()

    with patch("portals.devex_adapter.DevexAuth", return_value=mock_auth):
        with pytest.raises(PortalFetchError):
            await adapter.fetch_opportunities()

    mock_auth.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Per-URL parse failure — partial results
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_per_url_parse_failure_returns_partial_results():
    """When one URL fails to parse, the loop continues and partial results are returned."""
    config = make_config()
    adapter = DevexAdapter(config)

    good_parsed = {
        "devex_opportunity_id": "devex-111",
        "opportunity_title": "Good Opp",
        "funder_organisation": None,
        "country_region": None,
        "deadline": None,
        "contract_value": None,
        "opportunity_link": "https://devex.com/projects/111",
        "description_snippet": None,
    }

    mock_auth = AsyncMock()
    mock_auth.load_session = AsyncMock(return_value=MagicMock())
    mock_auth.close = AsyncMock()

    mock_search = AsyncMock()
    mock_search.collect_opportunity_urls = AsyncMock(
        return_value=["https://devex.com/projects/111", "https://devex.com/projects/222"]
    )

    mock_parser = AsyncMock()
    mock_parser.parse_opportunity = AsyncMock(
        side_effect=[good_parsed, Exception("parse boom")]
    )

    with (
        patch("portals.devex_adapter.DevexAuth", return_value=mock_auth),
        patch("portals.devex_adapter.DevexSearch", return_value=mock_search),
        patch("portals.devex_adapter.DevexParser", return_value=mock_parser),
    ):
        results = await adapter.fetch_opportunities()

    assert len(results) == 1
    assert results[0]["opportunity_id"] == "devex-111"
    assert results[0]["source_portal"] == "devex"
    assert results[0]["matched_keywords"] == []


# ---------------------------------------------------------------------------
# finally block — auth.close() always called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finally_close_called_on_success():
    """auth.close() is called after a successful fetch."""
    config = make_config()
    adapter = DevexAdapter(config)

    mock_auth = AsyncMock()
    mock_auth.load_session = AsyncMock(return_value=MagicMock())
    mock_auth.close = AsyncMock()

    mock_search = AsyncMock()
    mock_search.collect_opportunity_urls = AsyncMock(return_value=[])

    mock_parser = AsyncMock()

    with (
        patch("portals.devex_adapter.DevexAuth", return_value=mock_auth),
        patch("portals.devex_adapter.DevexSearch", return_value=mock_search),
        patch("portals.devex_adapter.DevexParser", return_value=mock_parser),
    ):
        result = await adapter.fetch_opportunities()

    assert result == []
    mock_auth.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_finally_close_called_when_search_raises():
    """A listing/search fetch failure raises a typed PortalFetchError and still
    closes Playwright. The raw cause message must not leak into str()."""
    config = make_config()
    adapter = DevexAdapter(config)

    mock_auth = AsyncMock()
    mock_auth.load_session = AsyncMock(return_value=MagicMock())
    mock_auth.close = AsyncMock()

    mock_search = AsyncMock()
    mock_search.collect_opportunity_urls = AsyncMock(
        side_effect=RuntimeError("network error https://devex.com/api?token=SECRET")
    )

    mock_parser = AsyncMock()

    with (
        patch("portals.devex_adapter.DevexAuth", return_value=mock_auth),
        patch("portals.devex_adapter.DevexSearch", return_value=mock_search),
        patch("portals.devex_adapter.DevexParser", return_value=mock_parser),
    ):
        with pytest.raises(PortalFetchError) as excinfo:
            await adapter.fetch_opportunities()

    err = excinfo.value
    assert err.portal == "Devex"
    assert err.operation == "listing_fetch"
    # Only the safe cause label (class name) appears; secrets do not.
    assert "RuntimeError" in str(err)
    assert "SECRET" not in str(err)
    assert "token" not in str(err)
    assert isinstance(err.__cause__, RuntimeError)
    mock_auth.close.assert_awaited_once()
