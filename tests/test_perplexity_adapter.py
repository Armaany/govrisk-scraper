"""Unit and property-based tests for PerplexityAdapter."""
import json
import re
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

from config import Config
from portals.perplexity_adapter import PerplexityAdapter, _deterministic_hash


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
        perplexity_enabled=True,
        perplexity_api_key="test-perplexity-key",
    )
    defaults.update(kwargs)
    return Config(**defaults)


def _make_api_response(content: str, status_code: int = 200) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": content}}]
    }
    if status_code >= 400:
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=MagicMock(status_code=status_code),
        )
    else:
        mock_resp.raise_for_status.return_value = None
    return mock_resp


def _make_async_client(mock_response: MagicMock) -> MagicMock:
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)
    return mock_client


SAMPLE_ITEMS = [
    {
        "opportunity_title": "Governance Reform Grant",
        "funder_organisation": "World Bank",
        "country_region": "Colombia",
        "deadline": "2025-12-31",
        "opportunity_link": "https://example.com/opp/1",
        "description_snippet": "A grant for governance reform.",
    }
]


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_returns_empty_without_http():
    """When perplexity_enabled=False, fetch_opportunities returns [] without HTTP calls."""
    config = make_config(perplexity_enabled=False)
    adapter = PerplexityAdapter(config)

    with patch("portals.perplexity_adapter.httpx.AsyncClient") as mock_client_cls:
        result = await adapter.fetch_opportunities()

    assert result == []
    mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_http_error_returns_empty_and_logs():
    """When Perplexity returns HTTP 4xx, fetch_opportunities returns [] and logs the error."""
    config = make_config()
    adapter = PerplexityAdapter(config)

    mock_response = _make_api_response("", status_code=401)
    mock_client = _make_async_client(mock_response)

    with (
        patch("portals.perplexity_adapter.httpx.AsyncClient", return_value=mock_client),
        patch.object(adapter, "_log_http_error") as mock_log,
    ):
        result = await adapter.fetch_opportunities()

    assert result == []
    mock_log.assert_called_once()


@pytest.mark.asyncio
async def test_unparseable_json_returns_empty_and_logs():
    """When the response content is not valid JSON, returns [] and logs parse error."""
    config = make_config()
    adapter = PerplexityAdapter(config)

    mock_response = _make_api_response("this is not json at all")
    mock_client = _make_async_client(mock_response)

    with (
        patch("portals.perplexity_adapter.httpx.AsyncClient", return_value=mock_client),
        patch.object(adapter, "_log_parse_error") as mock_log,
    ):
        result = await adapter.fetch_opportunities()

    assert result == []
    mock_log.assert_called_once()


@pytest.mark.asyncio
async def test_valid_json_maps_to_opportunity_dict():
    """A valid JSON response is correctly mapped to Opportunity_Dict fields."""
    config = make_config()
    adapter = PerplexityAdapter(config)

    content = json.dumps(SAMPLE_ITEMS)
    mock_response = _make_api_response(content)
    mock_client = _make_async_client(mock_response)

    with patch("portals.perplexity_adapter.httpx.AsyncClient", return_value=mock_client):
        results = await adapter.fetch_opportunities()

    assert len(results) == 1
    opp = results[0]
    link = SAMPLE_ITEMS[0]["opportunity_link"]
    assert opp["opportunity_id"] == f"perplexity-{_deterministic_hash(link)}"
    assert opp["source_portal"] == "perplexity"
    assert opp["matched_keywords"] == []
    assert opp["opportunity_title"] == "Governance Reform Grant"
    assert opp["funder_organisation"] == "World Bank"
    assert opp["country_region"] == "Colombia"
    assert opp["deadline"] == "2025-12-31"
    assert opp["opportunity_link"] == link
    assert opp["description_snippet"] == "A grant for governance reform."


@pytest.mark.asyncio
async def test_markdown_code_fences_stripped():
    """Markdown code fences (```json ... ```) are stripped before JSON parsing."""
    config = make_config()
    adapter = PerplexityAdapter(config)

    raw_json = json.dumps(SAMPLE_ITEMS)
    fenced_content = f"```json\n{raw_json}\n```"
    mock_response = _make_api_response(fenced_content)
    mock_client = _make_async_client(mock_response)

    with patch("portals.perplexity_adapter.httpx.AsyncClient", return_value=mock_client):
        results = await adapter.fetch_opportunities()

    assert len(results) == 1
    assert results[0]["opportunity_title"] == "Governance Reform Grant"


# ---------------------------------------------------------------------------
# Property 10: Perplexity prompt contains all configured keywords and countries
# Validates: Requirements 4.3
# ---------------------------------------------------------------------------

@given(
    keywords=st.lists(
        st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"))),
        min_size=1,
        max_size=5,
    ),
    countries=st.lists(
        st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll"))),
        min_size=1,
        max_size=5,
    ),
)
@settings(max_examples=100)
def test_prompt_contains_all_keywords_and_countries(keywords: list[str], countries: list[str]):
    """
    **Validates: Requirements 4.3**
    Property 10: For any Config with any sector_keywords and target_countries,
    the prompt built by _build_prompt() must contain every keyword and every country.
    """
    config = make_config(sector_keywords=keywords, target_countries=countries)
    adapter = PerplexityAdapter(config)

    prompt = adapter._build_prompt()

    for keyword in keywords:
        assert keyword in prompt, f"Keyword '{keyword}' not found in prompt"
    for country in countries:
        assert country in prompt, f"Country '{country}' not found in prompt"


# ---------------------------------------------------------------------------
# Property 13: Perplexity opportunity_id is deterministic
# Validates: Requirements 10.3
# ---------------------------------------------------------------------------

@given(link=st.text(min_size=0, max_size=200))
@settings(max_examples=100)
def test_perplexity_id_is_deterministic(link: str):
    """
    **Validates: Requirements 10.3**
    Property 13: For any opportunity_link, calling the ID generation function twice
    must produce the same opportunity_id, and it must match perplexity-[a-f0-9]{12}.
    """
    id1 = f"perplexity-{_deterministic_hash(link)}"
    id2 = f"perplexity-{_deterministic_hash(link)}"

    assert id1 == id2
    assert re.match(r"^perplexity-[a-f0-9]{12}$", id1), (
        f"ID '{id1}' does not match expected pattern perplexity-[a-f0-9]{{12}}"
    )
