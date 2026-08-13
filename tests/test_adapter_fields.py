"""Property test for adapter result field completeness.

Feature: multi-portal-adapter-architecture
Property 1: Adapter result fields are complete
Validates: Requirements 3.4, 4.4
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from config import Config
from portals.perplexity_adapter import PerplexityAdapter, _deterministic_hash
from portals.samgov_adapter import SAMGovAdapter

# Required keys every Opportunity_Dict must contain
REQUIRED_KEYS = {
    "opportunity_id",
    "opportunity_title",
    "funder_organisation",
    "country_region",
    "deadline",
    "contract_value",
    "opportunity_link",
    "description_snippet",
    "source_portal",
    "matched_keywords",
}


def make_config(**kwargs) -> Config:
    defaults = dict(
        devex_email="test@example.com",
        devex_password="password",
        sector_keywords=["governance"],
        target_countries=["Colombia"],
        max_results=10,
        notification_email="notify@example.com",
        admin_alert_email="admin@example.com",
        samgov_enabled=True,
        samgov_api_key="test-key",
        perplexity_enabled=True,
        perplexity_api_key="pplx-key",
    )
    defaults.update(kwargs)
    return Config(**defaults)


# ---------------------------------------------------------------------------
# Property 1: SAMGovAdapter result fields are complete
# Validates: Requirements 3.4
# ---------------------------------------------------------------------------

@given(
    notice_ids=st.lists(
        st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"))),
        min_size=1,
        max_size=10,
    ),
    country=st.sampled_from(["Colombia", "Brazil", "Mexico", "Peru"]),
)
@settings(max_examples=50)
def test_property_1_samgov_result_fields_complete(notice_ids, country):
    """Property 1 (SAMGov): Every Opportunity_Dict returned contains all required keys.

    Validates: Requirements 3.4
    """
    import asyncio

    config = make_config(target_countries=[country])
    adapter = SAMGovAdapter(config)

    items = [
        {
            "noticeId": nid,
            "title": f"Opp {nid}",
            "organizationName": "USAID",
            "responseDeadLine": "2025-12-31",
            "placeOfPerformance": {
                "country": {"name": country},
                "state": {"name": ""},
            },
            "description": f"Description for {nid}",
            "award": {"amount": "100000"},
        }
        for nid in notice_ids
    ]

    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"opportunitiesData": items}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("portals.samgov_adapter.httpx.AsyncClient", return_value=mock_client):
        results = asyncio.get_event_loop().run_until_complete(adapter.fetch_opportunities())

    assert len(results) == len(notice_ids)
    for opp in results:
        missing = REQUIRED_KEYS - set(opp.keys())
        assert not missing, f"Opportunity_Dict missing keys: {missing}. Got: {set(opp.keys())}"


# ---------------------------------------------------------------------------
# Property 1: PerplexityAdapter result fields are complete
# Validates: Requirements 4.4
# ---------------------------------------------------------------------------

@given(
    links=st.lists(
        st.text(min_size=5, max_size=60, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="/:.-_")).map(lambda s: f"https://{s}"),
        min_size=1,
        max_size=10,
        unique=True,
    ),
)
@settings(max_examples=50)
def test_property_1_perplexity_result_fields_complete(links):
    """Property 1 (Perplexity): Every Opportunity_Dict returned contains all required keys.

    Validates: Requirements 4.4
    """
    import asyncio

    config = make_config()
    adapter = PerplexityAdapter(config)

    items = [
        {
            "opportunity_title": f"Opp {i}",
            "funder_organisation": "World Bank",
            "country_region": "Colombia",
            "deadline": "2025-12-31",
            "opportunity_link": link,
            "description_snippet": f"Description {i}",
        }
        for i, link in enumerate(links)
    ]

    content = json.dumps(items)
    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"choices": [{"message": {"content": content}}]}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("portals.perplexity_adapter.httpx.AsyncClient", return_value=mock_client):
        results = asyncio.get_event_loop().run_until_complete(adapter.fetch_opportunities())

    assert len(results) == len(links)
    for opp in results:
        missing = REQUIRED_KEYS - set(opp.keys())
        assert not missing, f"Opportunity_Dict missing keys: {missing}. Got: {set(opp.keys())}"
