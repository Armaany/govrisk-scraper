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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from main import build_adapter_registry, deduplicate_opportunities
from portals.devex_adapter import DevexAdapter
from portals.iadb_adapter import IADBAdapter
from portals.oecd_adapter import OECDAdapter
from portals.perplexity_adapter import PerplexityAdapter
from portals.samgov_adapter import SAMGovAdapter
from portals.undp_adapter import UNDPAdapter
from portals.usaid_adapter import USAIDAdapter
from portals.worldbank_adapter import WorldBankAdapter


# ---------------------------------------------------------------------------
# Minimal Config stub — avoids loading .env during tests
# ---------------------------------------------------------------------------

@dataclass
class StubConfig:
    devex_enabled: bool = True
    undp_enabled: bool = False
    worldbank_enabled: bool = False
    usaid_enabled: bool = False
    iadb_enabled: bool = False
    oecd_enabled: bool = False
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


FLAG_TO_ADAPTER = {
    "devex_enabled": (DevexAdapter, "devex"),
    "undp_enabled": (UNDPAdapter, "undp"),
    "worldbank_enabled": (WorldBankAdapter, "worldbank"),
    "usaid_enabled": (USAIDAdapter, "usaid"),
    "iadb_enabled": (IADBAdapter, "iadb"),
    "oecd_enabled": (OECDAdapter, "oecd"),
    "samgov_enabled": (SAMGovAdapter, "samgov"),
    "perplexity_enabled": (PerplexityAdapter, "perplexity"),
}


# ---------------------------------------------------------------------------
# Property 5: Adapter registry contains exactly the enabled adapters
# Validates: Requirements 6.1
# ---------------------------------------------------------------------------

@given(
    devex_enabled=st.booleans(),
    undp_enabled=st.booleans(),
    worldbank_enabled=st.booleans(),
    usaid_enabled=st.booleans(),
    iadb_enabled=st.booleans(),
    oecd_enabled=st.booleans(),
    samgov_enabled=st.booleans(),
    perplexity_enabled=st.booleans(),
)
@settings(max_examples=100)
def test_property_5_adapter_registry_contains_exactly_enabled_adapters(
    devex_enabled,
    undp_enabled,
    worldbank_enabled,
    usaid_enabled,
    iadb_enabled,
    oecd_enabled,
    samgov_enabled,
    perplexity_enabled,
):
    """Property 5: Adapter registry contains exactly the enabled adapters.

    # Feature: multi-portal-adapter-architecture, Property 5: Adapter registry contains exactly the enabled adapters

    For arbitrary combinations of enabled flags, build_adapter_registry returns
    adapters whose portal set matches exactly the set of enabled flags — no more,
    no fewer.

    **Validates: Requirements 6.1**
    """
    flags = {
        "devex_enabled": devex_enabled,
        "undp_enabled": undp_enabled,
        "worldbank_enabled": worldbank_enabled,
        "usaid_enabled": usaid_enabled,
        "iadb_enabled": iadb_enabled,
        "oecd_enabled": oecd_enabled,
        "samgov_enabled": samgov_enabled,
        "perplexity_enabled": perplexity_enabled,
    }
    config = StubConfig(
        **flags,
        samgov_api_key="key" if samgov_enabled else None,
        perplexity_api_key="key" if perplexity_enabled else None,
    )

    adapters = asyncio.run(build_adapter_registry(config))

    # The registry must contain exactly one adapter per enabled flag.
    expected_count = sum(flags.values())
    assert len(adapters) == expected_count

    # The set of portal names must equal exactly the set of enabled portals.
    expected_portals = {
        FLAG_TO_ADAPTER[flag][1] for flag, enabled in flags.items() if enabled
    }
    actual_portals = {a.portal_name for a in adapters}
    assert actual_portals == expected_portals

    # Each enabled flag maps to an instance of the correct adapter type; each
    # disabled flag has no corresponding instance.
    adapter_types = {type(a) for a in adapters}
    for flag, (adapter_cls, _portal) in FLAG_TO_ADAPTER.items():
        if flags[flag]:
            assert adapter_cls in adapter_types
        else:
            assert adapter_cls not in adapter_types


# ---------------------------------------------------------------------------
# Shared helper: run the REAL main.run_scraper() with external collaborators
# mocked, capturing every opportunity that reaches the production pipeline
# boundary KeywordFilter.passes_filter(). Does NOT reimplement main.py logic.
# ---------------------------------------------------------------------------

def _run_scraper_capturing_filter(fake_adapters):
    """Invoke the real `main.run_scraper()` with only external collaborators
    mocked (config, store, audit, notifier, LLM, KeywordFilter, and
    build_adapter_registry). Returns (filtered_opps, audit_mock, notifier_mock).

    `filtered_opps` is the ordered list of opportunity dicts passed to
    `KeywordFilter.passes_filter()` — the real collection + dedup + pipeline
    path in `main.py`. `passes_filter` returns False so the run stops at the
    filter boundary (no LLM/store needed to prove the opportunities arrived).
    """
    import main

    filtered_opps = []

    cfg = MagicMock()
    cfg.run_mode = "dry_run"
    cfg.store_type = "sheets"
    cfg.max_results = 10

    store = MagicMock()
    store.test_connection.return_value = True
    store.get_all_ids.return_value = set()

    def _record_and_reject(opp):
        filtered_opps.append(opp)
        return False

    kf = MagicMock()
    kf.passes_filter.side_effect = _record_and_reject

    audit = MagicMock()
    notifier = MagicMock()

    async def _fake_registry(config):
        return list(fake_adapters)

    with patch("main.load_config", return_value=cfg), \
         patch("main.SheetsAdapter", return_value=store), \
         patch("main.AirtableAdapter", return_value=store), \
         patch("main.AuditLogger", return_value=audit), \
         patch("main.Notifier", return_value=notifier), \
         patch("main.KeywordFilter", return_value=kf), \
         patch("main.LLMInterpreter", return_value=MagicMock()), \
         patch("main.build_adapter_registry", side_effect=_fake_registry):
        asyncio.run(main.run_scraper())

    return filtered_opps, audit, notifier


# ---------------------------------------------------------------------------
# Property 6: Unified list is the union of all adapter results (real pipeline)
# Validates: Requirements 6.2
# ---------------------------------------------------------------------------

@given(result_sizes=st.lists(st.integers(min_value=0, max_value=6), min_size=0, max_size=8))
@settings(max_examples=30)
def test_property_6_unified_list_is_union_of_adapter_results(result_sizes):
    """Property 6: every opportunity from every healthy adapter reaches the real
    production pipeline boundary (KeywordFilter.passes_filter), in the exact
    collected order — exercised through the real `main.run_scraper()` collection
    loop (no test-local `list.extend`).

    # Feature: multi-portal-adapter-architecture, Property 6: Unified list is the union of all adapter results

    **Validates: Requirements 6.2**
    """
    per_adapter_results = [
        [
            make_opp(
                opportunity_id=f"p{a_idx}-{i}",
                opportunity_link=f"https://p{a_idx}-{i}.example.com",
            )
            for i in range(size)
        ]
        for a_idx, size in enumerate(result_sizes)
    ]
    fake_adapters = []
    for a_idx, opps in enumerate(per_adapter_results):
        ad = MagicMock()
        ad.portal_name = f"portal-{a_idx}"
        ad.fetch_opportunities = AsyncMock(return_value=opps)
        fake_adapters.append(ad)

    filtered, _audit, _notifier = _run_scraper_capturing_filter(fake_adapters)

    # Every collected opportunity reached passes_filter in the correct order.
    # (Fails if main.py stops collecting any adapter's results.)
    expected = [opp for opps in per_adapter_results for opp in opps]
    assert filtered == expected

    # Each adapter was awaited exactly once by the real orchestrator.
    for ad in fake_adapters:
        assert ad.fetch_opportunities.await_count == 1


# ---------------------------------------------------------------------------
# Property 7: Failing adapters do not suppress healthy results (real pipeline)
# Validates: Requirements 6.4
# ---------------------------------------------------------------------------

@given(
    adapter_specs=st.lists(
        st.tuples(st.booleans(), st.integers(min_value=0, max_value=5)),
        min_size=1,
        max_size=8,
    ),
)
@settings(max_examples=30)
def test_property_7_failing_adapters_do_not_suppress_healthy_results(adapter_specs):
    """Property 7: with arbitrarily interleaved healthy/failing adapters, the
    real `main.run_scraper()` isolates failures — every adapter is awaited once,
    every healthy opportunity reaches the pipeline (including adapters after an
    earlier failure), each failing adapter yields an audit error + notifier
    alert, and the completion summary reports the exact failure count. Does not
    reproduce main.py's try/except loop.

    # Feature: multi-portal-adapter-architecture, Property 7: Failing adapters do not suppress results from healthy adapters

    **Validates: Requirements 6.4**
    """
    fake_adapters = []
    expected_healthy = []
    failing_portals = []
    for a_idx, (is_healthy, size) in enumerate(adapter_specs):
        ad = MagicMock()
        ad.portal_name = f"portal-{a_idx}"
        if is_healthy:
            opps = [
                make_opp(
                    opportunity_id=f"h-{a_idx}-{i}",
                    opportunity_link=f"https://h-{a_idx}-{i}.example.com",
                )
                for i in range(size)
            ]
            expected_healthy.extend(opps)
            ad.fetch_opportunities = AsyncMock(return_value=opps)
        else:
            failing_portals.append(f"portal-{a_idx}")
            ad.fetch_opportunities = AsyncMock(side_effect=RuntimeError(f"boom-{a_idx}"))
        fake_adapters.append(ad)

    expected_failures = len(failing_portals)

    filtered, audit, notifier = _run_scraper_capturing_filter(fake_adapters)

    # Every adapter awaited exactly once (later adapters ran despite failures).
    for ad in fake_adapters:
        assert ad.fetch_opportunities.await_count == 1

    # Every healthy opportunity reached the pipeline, in collected order.
    assert filtered == expected_healthy

    # Each failing adapter produced an audit error and a notifier alert naming it.
    assert audit.log_error.call_count == expected_failures
    assert notifier.send_error_alert.call_count == expected_failures
    alerted = [c.kwargs.get("component") for c in notifier.send_error_alert.call_args_list]
    for portal in failing_portals:
        assert portal in alerted

    # The completion summary reports the exact failure count.
    summary_calls = notifier.send_completion_summary.call_args_list
    assert len(summary_calls) == 1
    assert summary_calls[0].kwargs.get("errors") == expected_failures


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
    deduplicated, skipped = deduplicate_opportunities(all_opps, existing_ids=set())

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
    deduplicated, skipped = deduplicate_opportunities(all_opps, existing_ids=set())

    # No two entries share the same opportunity_link
    seen_links = set()
    for opp in deduplicated:
        link = opp.get("opportunity_link", "")
        assert link not in seen_links, f"Duplicate opportunity_link found: {link}"
        seen_links.add(link)

    # Duplicates were counted
    assert skipped == duplicate_count


# ---------------------------------------------------------------------------
# Additional: deduplication against existing_ids from store
# ---------------------------------------------------------------------------

@given(
    ids=st.lists(opp_id_strategy, min_size=1, max_size=10, unique=True),
)
@settings(max_examples=50)
def test_deduplication_skips_ids_already_in_store(ids):
    """Opportunities whose IDs are already in the store are skipped."""
    existing_ids = set(ids)
    opps = [
        make_opp(
            opportunity_id=oid,
            opportunity_link=f"https://link-{i}.example.com",
        )
        for i, oid in enumerate(ids)
    ]

    deduplicated, skipped = deduplicate_opportunities(opps, existing_ids=existing_ids)

    assert len(deduplicated) == 0
    assert skipped == len(ids)

# ---------------------------------------------------------------------------
# Property 7 (deterministic regression): a failing adapter FOLLOWED BY a healthy
# adapter. Hypothesis Property 7 cannot guarantee this exact ordering appears in
# a generated example, so this permanently proves the failure-before-success
# path (later healthy adapter still runs and its opportunity reaches the
# pipeline).
# Validates: Requirements 6.4
# ---------------------------------------------------------------------------

def test_property_7_failure_before_success_healthy_adapter_still_runs():
    """Deterministic: failing adapter, then healthy adapter with one unique opp.

    # Feature: multi-portal-adapter-architecture, Property 7 (deterministic): failing adapter followed by healthy adapter

    **Validates: Requirements 6.4**
    """
    failing = MagicMock()
    failing.portal_name = "failing-portal"
    failing.fetch_opportunities = AsyncMock(side_effect=RuntimeError("boom"))

    healthy_opp = make_opp(
        opportunity_id="healthy-after-failure-1",
        opportunity_link="https://healthy-after-failure-1.example.com",
    )
    healthy = MagicMock()
    healthy.portal_name = "healthy-portal"
    healthy.fetch_opportunities = AsyncMock(return_value=[healthy_opp])

    fake_adapters = [failing, healthy]  # failure BEFORE success

    filtered, audit, notifier = _run_scraper_capturing_filter(fake_adapters)

    # Both adapters awaited exactly once (healthy ran after the earlier failure).
    assert failing.fetch_opportunities.await_count == 1
    assert healthy.fetch_opportunities.await_count == 1

    # The later healthy opportunity reached KeywordFilter.passes_filter().
    assert filtered == [healthy_opp]

    # Exactly one audit error and one notifier alert naming the failing portal.
    assert audit.log_error.call_count == 1
    assert notifier.send_error_alert.call_count == 1
    assert notifier.send_error_alert.call_args_list[0].kwargs.get("component") == "failing-portal"

    # Completion summary reports errors=1.
    summary_calls = notifier.send_completion_summary.call_args_list
    assert len(summary_calls) == 1
    assert summary_calls[0].kwargs.get("errors") == 1
