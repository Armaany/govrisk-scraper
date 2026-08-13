"""Unit and property-based tests for SAMGovAdapter."""
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

from config import Config
from portals.samgov_adapter import SAMGovAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_config(**kwargs) -> Config:
    defaults = dict(
        devex_email="test@example.com",
        devex_password="password",
        sector_keywords=["governance", "risk"],
        target_countries=["Colombia", "Brazil"],
        max_results=25,
        notification_email="notify@example.com",
        admin_alert_email="admin@example.com",
        samgov_enabled=True,
        samgov_api_key="test-api-key",
    )
    defaults.update(kwargs)
    return Config(**defaults)


def make_sam_item(notice_id: str = "ABC123", country: str = "Colombia") -> dict:
    return {
        "noticeId": notice_id,
        "title": "Test Opportunity",
        "organizationName": "USAID",
        "responseDeadLine": "2025-12-31",
        "placeOfPerformance": {
            "country": {"name": country},
            "state": {"name": ""},
        },
        "description": "A test procurement opportunity.",
        "award": {"amount": "500000"},
    }


def _make_mock_response(items: list[dict], status_code: int = 200) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = {"opportunitiesData": items}
    if status_code >= 400:
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=MagicMock(status_code=status_code),
        )
    else:
        mock_resp.raise_for_status.return_value = None
    return mock_resp


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_returns_empty_without_http():
    """When samgov_enabled=False, fetch_opportunities returns [] without any HTTP calls."""
    config = make_config(samgov_enabled=False)
    adapter = SAMGovAdapter(config)

    with patch("portals.samgov_adapter.httpx.AsyncClient") as mock_client_cls:
        result = await adapter.fetch_opportunities()

    assert result == []
    mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_http_4xx_returns_empty_and_logs_error():
    """When SAM.gov returns HTTP 4xx, fetch_opportunities returns [] and logs the error."""
    config = make_config()
    adapter = SAMGovAdapter(config)

    mock_response = _make_mock_response([], status_code=403)
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with (
        patch("portals.samgov_adapter.httpx.AsyncClient", return_value=mock_client),
        patch.object(adapter, "_log_http_error") as mock_log,
    ):
        result = await adapter.fetch_opportunities()

    assert result == []
    mock_log.assert_called_once()


@pytest.mark.asyncio
async def test_successful_fetch_maps_results():
    """A successful response maps items to Opportunity_Dict with correct fields."""
    config = make_config()
    adapter = SAMGovAdapter(config)

    item = make_sam_item(notice_id="XYZ789", country="Colombia")
    mock_response = _make_mock_response([item])
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("portals.samgov_adapter.httpx.AsyncClient", return_value=mock_client):
        results = await adapter.fetch_opportunities()

    assert len(results) == 1
    opp = results[0]
    assert opp["opportunity_id"] == "samgov-XYZ789"
    assert opp["source_portal"] == "samgov"
    assert opp["opportunity_link"] == "https://sam.gov/opp/XYZ789/view"
    assert opp["matched_keywords"] == []


@pytest.mark.asyncio
async def test_non_latam_items_filtered_out():
    """Items whose country does not match target_countries are excluded."""
    config = make_config(target_countries=["Colombia"])
    adapter = SAMGovAdapter(config)

    items = [
        make_sam_item(notice_id="COL1", country="Colombia"),
        make_sam_item(notice_id="USA1", country="United States"),
    ]
    mock_response = _make_mock_response(items)
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("portals.samgov_adapter.httpx.AsyncClient", return_value=mock_client):
        results = await adapter.fetch_opportunities()

    assert len(results) == 1
    assert results[0]["opportunity_id"] == "samgov-COL1"


# ---------------------------------------------------------------------------
# Property 9: SAM.gov query params reflect Config values
# Validates: Requirements 3.3
# ---------------------------------------------------------------------------

@given(
    keywords=st.lists(st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"))), min_size=1, max_size=5),
    max_results=st.integers(min_value=1, max_value=200),
)
@settings(max_examples=50)
def test_samgov_query_params_reflect_config(keywords: list[str], max_results: int):
    """
    **Validates: Requirements 3.3**
    Property 9: For any Config with any sector_keywords and max_results,
    the HTTP request must include q=space-joined keywords and limit=max_results.
    """
    config = make_config(sector_keywords=keywords, max_results=max_results)
    adapter = SAMGovAdapter(config)

    captured_params = {}

    async def fake_get(url, params=None, **kwargs):
        captured_params.update(params or {})
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"opportunitiesData": []}
        return mock_resp

    import asyncio

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = fake_get

    with patch("portals.samgov_adapter.httpx.AsyncClient", return_value=mock_client):
        asyncio.get_event_loop().run_until_complete(adapter.fetch_opportunities())

    assert captured_params["q"] == " ".join(keywords)
    assert captured_params["limit"] == max_results


# ---------------------------------------------------------------------------
# Property 12: SAM.gov opportunity_id matches portal-prefixed format
# Validates: Requirements 10.2
# ---------------------------------------------------------------------------

@given(notice_id=st.text(min_size=1, max_size=50))
@settings(max_examples=100)
def test_samgov_id_format(notice_id: str):
    """
    **Validates: Requirements 10.2**
    Property 12: For any SAM.gov result with any noticeId, the mapped
    opportunity_id must equal f"samgov-{noticeId}".
    """
    config = make_config()
    adapter = SAMGovAdapter(config)

    item = {
        "noticeId": notice_id,
        "title": "Test",
        "organizationName": "Org",
        "responseDeadLine": None,
        "placeOfPerformance": {},
        "description": "",
        "award": {},
    }
    result = adapter._map_result(item)
    assert result["opportunity_id"] == f"samgov-{notice_id}"


# ---------------------------------------------------------------------------
# Property 16: LATAM post-filter excludes non-target countries
# Validates: Requirements 3.4, 3.3
# ---------------------------------------------------------------------------

@given(
    target_countries=st.lists(
        st.text(min_size=2, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll"))),
        min_size=1,
        max_size=5,
    ),
    items=st.lists(
        st.fixed_dictionaries({
            "noticeId": st.text(min_size=1, max_size=20),
            "country": st.text(min_size=2, max_size=30, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Zs"))),
            "description": st.text(max_size=100),
        }),
        min_size=0,
        max_size=10,
    ),
)
@settings(max_examples=50)
def test_samgov_latam_post_filter(target_countries: list[str], items: list[dict]):
    """
    **Validates: Requirements 3.4, 3.3**
    Property 16: _is_latam_relevant() returns False for items whose country/description
    does not match any target country, and the final list contains no such items.
    """
    config = make_config(target_countries=target_countries)
    adapter = SAMGovAdapter(config)

    for raw in items:
        sam_item = {
            "noticeId": raw["noticeId"],
            "placeOfPerformance": {
                "country": {"name": raw["country"]},
                "state": {"name": ""},
            },
            "description": raw["description"],
        }
        result = adapter._is_latam_relevant(sam_item)

        # Manually compute expected result
        target_lower = [c.lower() for c in target_countries]
        country_lower = raw["country"].lower()
        desc_lower = raw["description"].lower()
        expected = any(
            t in country_lower or t in desc_lower
            for t in target_lower
        )
        assert result == expected


@given(
    target_countries=st.lists(
        st.text(min_size=2, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll"))),
        min_size=1,
        max_size=5,
    ),
)
@settings(max_examples=50)
def test_samgov_latam_filter_empty_target_passes_all(target_countries: list[str]):
    """When target_countries is empty, _is_latam_relevant returns True for any item."""
    config = make_config(target_countries=[])
    adapter = SAMGovAdapter(config)

    item = {
        "noticeId": "X",
        "placeOfPerformance": {"country": {"name": "Anywhere"}, "state": {"name": ""}},
        "description": "some text",
    }
    assert adapter._is_latam_relevant(item) is True
