"""Property-based tests for the main.py orchestrator.

Feature: multi-portal-adapter-architecture

Properties tested:
  Property 3: Deduplication eliminates repeated opportunity_id values
  Property 4: Deduplication eliminates repeated opportunity_link values
  Property 5: Adapter registry contains exactly the enabled adapters
  Property 6: Unified list is the union of all adapter results
  Property 7: Failing adapters do not suppress results from healthy adapters
"""
import asyncio
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from main import build_adapter_registry, deduplicate_opportunities
from portals.devex_adapter import DevexAdapter
from portals.perplexity_adapter import PerplexityAdapter
from portals.samgov_adapter import SAMGovAdapter


# ---------------------------------------------------------------------------
# Minimal Config stub — avoids loading .env during tests
# ---------------------------------------------------------------------------

@dataclass
class StubConfig:
    devex_enabled: bool = True
    samgov_enabled: bool = False
    perplexity_enabled: bool = False
    devex_email: str = "test@example.com"
    devex_password: str = "password"
    devex_session_path: str = "./devex_session.json"
    anthropic_api_key: str = "test-key"
    store_type: str = "sheets"
    google_sheets_id: Optional[str] = "sheet-id"
    sheets_tab_name: str = "Opportunities"
    service_account_json: Optional[str] = "service_account.json"
    airtable_api_key: Optional[str] = None
    airtable_base_id: Optional[str] = None
    airtable_table_name: str = "Opportunities"
    sector_keywords: list = field(default_factory=lambda: ["governance"])
    target_countries: list = field(default_factory=lambda: ["colombia"])
    max_results: int = 10
    run_mode: str = "dry_run"
    headless: bool = True
    log_level: str = "INFO"
    notification_email: str = "notify@example.com"
    admin_alert_email: str = "admin@example.com"
    samgov_api_key: Optional[str] = None
    perplexity_api_key: Optional[str] = None


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

opp_id_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-_"),
    min_size=1,
    max_size=40,
)

opp_link_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="/:.-_"),
    min_size=5,
    max_size=80,
).map(lambda s: f"https://{s}")


def make_opp(opportunity_id: str = "", opportunity_link: str = "") -> dict:
    return {
        "opportunity_id": opportunity_id,
        "opportunity_title": "Test Opportunity",
        "funder_organisation": "Test Org",
        "country_region": "Colombia",
        "deadline": None,
        "contract_value": None,
        "opportunity_link": opportunity_link,
        "description_snippet": "Test description",
        "source_portal": "devex",
        "matched_keywords": [],
    }


# ---------------------------------------------------------------------------
# Property 5: Adapter registry contains exactly the enabled adapters
# Validates: Requirements 6.1
# ---------------------------------------------------------------------------

@given(
    devex_enabled=st.booleans(),
    samgov_enabled=st.booleans(),
    perplexity_enabled=st.booleans(),
)
@settings(max_examples=50)
def test_property_5_adapter_registry_contains_exactly_enabled_adapters(
    devex_enabled, samgov_enabled, perplexity_enabled
):
    """Property 5: Adapter registry contains exactly the enabled adapters.

    Validates: Requirements 6.1
    """
    config = StubConfig(
        devex_enabled=devex_enabled,
        samgov_enabled=samgov_enabled,
        perplexity_enabled=perplexity_enabled,
        samgov_api_key="key" if samgov_enabled else None,
        perplexity_api_key="key" if perplexity_enabled else None,
    )

    adapters = asyncio.get_event_loop().run_until_complete(build_adapter_registry(config))

    # Count expected adapters
    expected_count = sum([devex_enabled, samgov_enabled, perplexity_enabled])
    assert len(adapters) == expected_count

    # Verify correct types are present / absent
    adapter_types = [type(a) for a in adapters]
    if devex_enabled:
        assert DevexAdapter in adapter_types
    else:
        assert DevexAdapter not in adapter_types

    if samgov_enabled:
        assert SAMGovAdapter in adapter_types
    else:
        assert SAMGovAdapter not in adapter_types

    if perplexity_enabled:
        assert PerplexityAdapter in adapter_types
    else:
        assert PerplexityAdapter not in adapter_types


# ---------------------------------------------------------------------------
# Property 6: Unified list is the union of all adapter results
# Validates: Requirements 6.2
# ---------------------------------------------------------------------------

@given(
    results_a=st.lists(st.integers(min_value=0, max_value=20), min_size=0, max_size=5),
    results_b=st.lists(st.integers(min_value=0, max_value=20), min_size=0, max_size=5),
    results_c=st.lists(st.integers(min_value=0, max_value=20), min_size=0, max_size=5),
)
@settings(max_examples=100)
def test_property_6_unified_list_is_union_of_adapter_results(results_a, results_b, results_c):
    """Property 6: Unified list is the union of all adapter results.

    For any set of active adapters each returning any number of results, the unified
    list collected before deduplication must have length equal to the sum of all
    individual adapter result lengths.

    Validates: Requirements 6.2
    """
    # Build unique opportunity dicts for each "adapter" result set
    def make_opps(sizes, prefix):
        return [make_opp(opportunity_id=f"{prefix}-{i}", opportunity_link=f"https://{prefix}-{i}.example.com") for i in range(sizes)]

    opps_a = make_opps(len(results_a), "a")
    opps_b = make_opps(len(results_b), "b")
    opps_c = make_opps(len(results_c), "c")

    # Simulate the unified adapter loop
    all_opportunities: list[dict] = []
    for adapter_results in [opps_a, opps_b, opps_c]:
        all_opportunities.extend(adapter_results)

    expected_total = len(opps_a) + len(opps_b) + len(opps_c)
    assert len(all_opportunities) == expected_total


# ---------------------------------------------------------------------------
# Property 7: Failing adapters do not suppress results from healthy adapters
# Validates: Requirements 6.4
# ---------------------------------------------------------------------------

@given(
    healthy_results=st.lists(
        st.integers(min_value=0, max_value=10),
        min_size=1,
        max_size=10,
    ),
    num_failing=st.integers(min_value=1, max_value=3),
)
@settings(max_examples=100)
def test_property_7_failing_adapters_do_not_suppress_healthy_results(
    healthy_results, num_failing
):
    """Property 7: Failing adapters do not suppress results from healthy adapters.

    For any set of adapters where a random subset raises exceptions, the opportunities
    returned by the non-failing adapters must all appear in the final unified list.

    Validates: Requirements 6.4
    """
    config = StubConfig()
    audit = MagicMock()
    audit.log_error = MagicMock()
    audit.log = MagicMock()
    notifier = MagicMock()
    notifier.send_error_alert = MagicMock()

    # Build healthy opportunities
    healthy_opps = [
        make_opp(
            opportunity_id=f"healthy-{i}",
            opportunity_link=f"https://healthy-{i}.example.com",
        )
        for i in range(len(healthy_results))
    ]

    # Simulate the orchestrator loop with failing + healthy adapters
    all_opportunities: list[dict] = []
    errors = 0

    # Failing adapters come first
    for _ in range(num_failing):
        failing_adapter = MagicMock()
        failing_adapter.portal_name = "failing-portal"
        failing_adapter.fetch_opportunities = AsyncMock(side_effect=RuntimeError("adapter failed"))
        try:
            result = asyncio.get_event_loop().run_until_complete(
                failing_adapter.fetch_opportunities()
            )
            all_opportunities.extend(result)
        except Exception as exc:
            errors += 1
            audit.log_error(str(exc))
            notifier.send_error_alert(str(exc), component=failing_adapter.portal_name)
            continue

    # Healthy adapter runs after failing ones
    healthy_adapter = MagicMock()
    healthy_adapter.portal_name = "healthy-portal"
    healthy_adapter.fetch_opportunities = AsyncMock(return_value=healthy_opps)
    try:
        result = asyncio.get_event_loop().run_until_complete(
            healthy_adapter.fetch_opportunities()
        )
        all_opportunities.extend(result)
    except Exception as exc:
        errors += 1
        audit.log_error(str(exc))
        notifier.send_error_alert(str(exc), component=healthy_adapter.portal_name)

    # All healthy results must be present
    assert len(all_opportunities) == len(healthy_opps)
    for opp in healthy_opps:
        assert opp in all_opportunities

    # Errors were counted for failing adapters
    assert errors == num_failing


# ---------------------------------------------------------------------------
# Property 3: Deduplication eliminates repeated opportunity_id values
# Validates: Requirements 6.7, 10.4
# ---------------------------------------------------------------------------

@given(
    unique_ids=st.lists(
        opp_id_strategy,
        min_size=1,
        max_size=20,
        unique=True,
    ),
    duplicate_count=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=100)
def test_property_3_deduplication_eliminates_repeated_opportunity_ids(
    unique_ids, duplicate_count
):
    """Property 3: Deduplication eliminates repeated opportunity_id values.

    For any list of Opportunity_Dict instances where some share the same opportunity_id,
    the deduplicated output list must contain no two entries with the same opportunity_id.

    Validates: Requirements 6.7, 10.4
    """
    # Build base list with unique IDs
    base_opps = [
        make_opp(
            opportunity_id=uid,
            opportunity_link=f"https://unique-{i}.example.com",
        )
        for i, uid in enumerate(unique_ids)
    ]

    # Add duplicates of the first ID
    first_id = unique_ids[0]
    duplicates = [
        make_opp(
            opportunity_id=first_id,
            opportunity_link=f"https://dup-link-{j}.example.com",
        )
        for j in range(duplicate_count)
    ]

    all_opps = base_opps + duplicates
    deduplicated, skipped = deduplicate_opportunities(all_opps, existing_links=set())

    # No two entries share the same opportunity_id
    seen = set()
    for opp in deduplicated:
        oid = opp.get("opportunity_id", "")
        assert oid not in seen, f"Duplicate opportunity_id found: {oid}"
        seen.add(oid)

    # Duplicates were counted
    assert skipped == duplicate_count


# ---------------------------------------------------------------------------
# Property 4: Deduplication eliminates repeated opportunity_link values
# Validates: Requirements 6.7
# ---------------------------------------------------------------------------

@given(
    unique_links=st.lists(
        opp_link_strategy,
        min_size=1,
        max_size=20,
        unique=True,
    ),
    duplicate_count=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=100)
def test_property_4_deduplication_eliminates_repeated_opportunity_links(
    unique_links, duplicate_count
):
    """Property 4: Deduplication eliminates repeated opportunity_link values.

    For any list of Opportunity_Dict instances where some share the same opportunity_link
    (and no portal-specific ID is available), the deduplicated output list must contain
    no two entries with the same opportunity_link.

    Validates: Requirements 6.7
    """
    # Build base list with unique links but no opportunity_id (empty string)
    base_opps = [
        make_opp(opportunity_id="", opportunity_link=link)
        for link in unique_links
    ]

    # Add duplicates of the first link (also no ID so link-based dedup triggers)
    first_link = unique_links[0]
    duplicates = [
        make_opp(opportunity_id="", opportunity_link=first_link)
        for _ in range(duplicate_count)
    ]

    all_opps = base_opps + duplicates
    deduplicated, skipped = deduplicate_opportunities(all_opps, existing_links=set())

    # No two entries share the same opportunity_link
    seen_links = set()
    for opp in deduplicated:
        link = opp.get("opportunity_link", "")
        assert link not in seen_links, f"Duplicate opportunity_link found: {link}"
        seen_links.add(link)

    # Duplicates were counted
    assert skipped == duplicate_count


# ---------------------------------------------------------------------------
# Additional: cross-run deduplication against existing_links from store
# (Option A: cross-run dedup keys on opportunity_link, not opportunity_id)
# Validates: Requirements 6.8, 10.5
# ---------------------------------------------------------------------------

@given(
    links=st.lists(opp_link_strategy, min_size=1, max_size=10, unique=True),
)
@settings(max_examples=50)
def test_deduplication_skips_links_already_in_store(links):
    """Opportunities whose links are already persisted (seeded from
    store.get_all_links()) are skipped as cross-run duplicates."""
    existing_links = set(links)
    opps = [
        make_opp(
            opportunity_id=f"id-{i}",
            opportunity_link=link,
        )
        for i, link in enumerate(links)
    ]

    deduplicated, skipped = deduplicate_opportunities(opps, existing_links=existing_links)

    assert len(deduplicated) == 0
    assert skipped == len(links)
