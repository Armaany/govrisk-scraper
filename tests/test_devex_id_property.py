"""Property-based test for Devex opportunity_id format.

Feature: multi-portal-adapter-architecture
Property 11: Devex opportunity_id matches portal-prefixed format
Validates: Requirements 10.1
"""
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hypothesis import given, settings
from hypothesis import strategies as st

from config import Config
from portals.devex_adapter import DevexAdapter
from portals.devex_auth import AuthenticationError  # noqa: F401  (import sanity)
from engine.parser import DevexParser


# The canonical portal-prefixed id pattern for Devex: "devex-" followed by the
# numeric project id derived from the URL, or the "devex-unknown" sentinel when
# no numeric id is present. Either way the value is always devex-prefixed.
DEVEX_ID_PATTERN = re.compile(r"^devex-(\d+|unknown)$")
DEVEX_NUMERIC_ID_PATTERN = re.compile(r"^devex-\d+$")


def make_config() -> Config:
    return Config(
        devex_email="test@example.com",
        devex_password="password",
        sector_keywords=["governance"],
        target_countries=["Colombia"],
        notification_email="notify@example.com",
        admin_alert_email="admin@example.com",
    )


def make_parser() -> DevexParser:
    # extract_devex_id does not touch the page; a MagicMock page is sufficient.
    return DevexParser(make_config(), MagicMock())


# Strategy for arbitrary URL "noise" that does not accidentally contain a
# "/projects/<digits>" segment. We keep the alphabet away from digits directly
# following a "/projects/" token by generating simple word/path fragments.
_url_noise = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu"),
        whitelist_characters="/-_.",
    ),
    min_size=0,
    max_size=40,
)


# ---------------------------------------------------------------------------
# Property 11: Devex opportunity_id matches portal-prefixed format
#
# Validates: Requirements 10.1
# ---------------------------------------------------------------------------
@given(
    project_id=st.integers(min_value=0, max_value=10**12),
    scheme=st.sampled_from(["https://www.devex.com", "https://devex.com", "http://devex.com"]),
    prefix=_url_noise,
    suffix=_url_noise,
)
@settings(max_examples=200)
def test_property_11_devex_id_matches_prefixed_format_for_project_urls(
    project_id, scheme, prefix, suffix
):
    """Property 11: For any URL containing a numeric /projects/<id> segment,
    the derived id equals ``devex-<id>`` and matches the devex-prefixed pattern.

    **Validates: Requirements 10.1**
    """
    parser = make_parser()
    url = f"{scheme}/{prefix}/projects/{project_id}{('/' + suffix) if suffix else ''}"

    result = parser.extract_devex_id(url)

    assert result == f"devex-{project_id}"
    assert DEVEX_NUMERIC_ID_PATTERN.match(result), result
    assert DEVEX_ID_PATTERN.match(result), result


@given(url=st.text(max_size=200))
@settings(max_examples=200)
def test_property_11_devex_id_always_prefixed_for_arbitrary_urls(url):
    """Property 11: For ANY input string, the derived id is always devex-prefixed
    (either ``devex-<digits>`` for a matched project id, or ``devex-unknown``).

    **Validates: Requirements 10.1**
    """
    parser = make_parser()

    result = parser.extract_devex_id(url)

    assert result.startswith("devex-"), result
    assert DEVEX_ID_PATTERN.match(result), result


@pytest.mark.asyncio
async def test_devex_adapter_fetch_remaps_id_and_sets_source_portal():
    """The real ``DevexAdapter.fetch_opportunities()`` remaps
    ``devex_opportunity_id`` -> ``opportunity_id``, removes
    ``devex_opportunity_id`` from the output dict, and sets
    ``source_portal="devex"``. ``DevexAuth``/``DevexSearch``/``DevexParser`` are
    mocked at their use sites in ``portals.devex_adapter`` so no browser or
    network is involved.

    **Validates: Requirements 10.1**
    """
    config = make_config()
    adapter = DevexAdapter(config)

    url = "https://www.devex.com/projects/123456"
    parsed = {
        "devex_opportunity_id": "devex-123456",
        "opportunity_title": "Some Devex opportunity",
        "opportunity_link": url,
    }

    mock_auth = AsyncMock()
    mock_auth.load_session = AsyncMock(return_value=MagicMock())
    mock_auth.close = AsyncMock()

    mock_search = MagicMock()
    mock_search.collect_opportunity_urls = AsyncMock(return_value=[url])

    mock_parser = MagicMock()
    mock_parser.parse_opportunity = AsyncMock(return_value=dict(parsed))

    with patch("portals.devex_adapter.DevexAuth", return_value=mock_auth), \
         patch("portals.devex_adapter.DevexSearch", return_value=mock_search), \
         patch("portals.devex_adapter.DevexParser", return_value=mock_parser):
        results = await adapter.fetch_opportunities()

    assert len(results) == 1
    opp = results[0]
    assert opp["opportunity_id"] == "devex-123456"
    assert "devex_opportunity_id" not in opp
    assert opp["source_portal"] == "devex"

    # Playwright resources closed in the adapter's finally block.
    mock_auth.close.assert_awaited_once()
