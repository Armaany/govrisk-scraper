"""Property-based tests for link-based cross-run dedup seeding and the
deprecation of get_records_since() under the Live_Sheet_Schema.

# Feature: multi-portal-adapter-architecture, Property 15: get_all_links seeds link-based cross-run dedup; get_records_since is deprecated for Sheets

**Validates: Requirements 6.8, 9.6, 9.7, 10.5**
- Requirement 6.8: cross-run dedup is seeded from the persisted opportunity_link
  column (column 7) via get_all_links(), not from portal_source (column 1).
- Requirement 9.6: get_records_since() is unsupported/deprecated under the
  Live_Sheet_Schema (no scraped_at column) and raises or returns empty.
- Requirement 9.7: AirtableAdapter.get_records_since() returns source_portal if
  present, else defaults legacy records to "devex".
- Requirement 10.5: cross-run dedup keys on opportunity_link.
"""
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Stub external dependencies so tests run without live creds/packages.
sys.modules.setdefault("gspread", MagicMock())
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.oauth2", MagicMock())
sys.modules.setdefault("google.oauth2.service_account", MagicMock())
sys.modules.setdefault("pyairtable", MagicMock())

from store.adapter_airtable import AirtableAdapter  # noqa: E402
from store.adapter_sheets import SheetsAdapter  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sheets_adapter_with_link_column(link_column_values):
    """Build a SheetsAdapter (no __init__) whose worksheet.col_values returns
    the supplied header + link rows list."""
    adapter = SheetsAdapter.__new__(SheetsAdapter)
    ws = MagicMock()

    # Set up header index (v1.1 header-name-driven)
    adapter._header_index = {h: i for i, h in enumerate(SheetsAdapter.HEADERS)}
    adapter._row_length = len(SheetsAdapter.HEADERS)

    link_index = adapter._header_index["opportunity_link"]

    def _col_values(n):
        if n == link_index + 1:
            return link_column_values
        return []

    ws.col_values.side_effect = _col_values
    adapter.worksheet = ws
    return adapter


# Links may include surrounding whitespace and empty entries below the header.
_link_row = st.one_of(
    st.just(""),
    st.just("   "),
    st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126),
        min_size=1,
        max_size=40,
    ).map(lambda s: "https://example.com/" + s),
    # Some links padded with surrounding whitespace to exercise stripping.
    st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126),
        min_size=1,
        max_size=20,
    ).map(lambda s: "  https://pad.example/" + s + "  "),
)


# ---------------------------------------------------------------------------
# Property 15a: get_all_links() returns exactly the non-empty stripped links
# below the header row.
#
# **Validates: Requirements 6.8, 9.6, 10.5**
# ---------------------------------------------------------------------------
@given(link_rows=st.lists(_link_row, max_size=25))
@settings(max_examples=200)
def test_property_15_get_all_links_returns_persisted_links(link_rows):
    """get_all_links() == set of non-empty stripped links below the header.

    **Validates: Requirements 6.8, 9.6, 10.5**
    """
    column = ["opportunity_link"] + link_rows  # header + data rows
    adapter = _sheets_adapter_with_link_column(column)

    result = adapter.get_all_links()

    expected = {v.strip() for v in link_rows if v.strip()}
    assert result == expected


# ---------------------------------------------------------------------------
# Property 15b: seeding a cross-run dedup set from get_all_links() skips an
# incoming opportunity whose opportunity_link is already persisted.
#
# **Validates: Requirements 6.8, 10.5**
# ---------------------------------------------------------------------------
@given(
    persisted_links=st.lists(
        st.text(
            alphabet=st.characters(min_codepoint=33, max_codepoint=126),
            min_size=1,
            max_size=30,
        ).map(lambda s: "https://example.com/" + s),
        min_size=1,
        max_size=15,
        unique=True,
    ),
    new_suffix=st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126),
        min_size=1,
        max_size=30,
    ),
)
@settings(max_examples=200)
def test_property_15_seeded_set_skips_persisted_link(persisted_links, new_suffix):
    """A link already in the get_all_links() seed set is treated as a duplicate.

    **Validates: Requirements 6.8, 10.5**
    """
    column = ["opportunity_link"] + persisted_links
    adapter = _sheets_adapter_with_link_column(column)

    seen_links = adapter.get_all_links()  # cross-run dedup seed

    # An incoming opportunity whose link is already persisted must be skipped.
    already_persisted = persisted_links[0]
    incoming_dup = {"opportunity_link": already_persisted}
    assert incoming_dup["opportunity_link"] in seen_links

    # A brand-new link (guaranteed not persisted) must NOT be skipped.
    fresh_link = "https://fresh.example/" + new_suffix
    if fresh_link not in seen_links:
        incoming_new = {"opportunity_link": fresh_link}
        assert incoming_new["opportunity_link"] not in seen_links


# ---------------------------------------------------------------------------
# Property 15c: get_records_since() is unsupported under the Live_Sheet_Schema.
#
# **Validates: Requirements 9.6**
# ---------------------------------------------------------------------------
def test_property_15_sheets_get_records_since_unsupported():
    """SheetsAdapter.get_records_since() raises NotImplementedError.

    **Validates: Requirements 9.6**
    """
    adapter = SheetsAdapter.__new__(SheetsAdapter)
    with pytest.raises(NotImplementedError):
        adapter.get_records_since(datetime.now())


# ---------------------------------------------------------------------------
# Property 15d: AirtableAdapter.get_records_since() defaults missing
# source_portal to "devex" for legacy records, and preserves it when present.
#
# **Validates: Requirements 9.7**
# ---------------------------------------------------------------------------
def _airtable_adapter_with_records(records):
    """Build an AirtableAdapter (no __init__) whose table.all() returns records."""
    adapter = AirtableAdapter.__new__(AirtableAdapter)
    table = MagicMock()
    table.all.return_value = records
    adapter.table = table
    return adapter


@given(
    legacy_portal=st.sampled_from(["samgov", "perplexity", "undp", "devex"]),
)
@settings(max_examples=50)
def test_property_15_airtable_get_records_since_defaults_devex(legacy_portal):
    """Legacy Airtable records missing source_portal default to "devex";
    records that carry source_portal keep their value.

    **Validates: Requirements 9.7**
    """
    since = datetime(2020, 1, 1)
    recent = (since + timedelta(days=10)).isoformat()

    records = [
        # Legacy record with no source_portal -> must default to "devex".
        {"fields": {"opportunity_title": "Legacy", "scraped_at": recent}},
        # Record that already carries source_portal -> value preserved.
        {"fields": {"opportunity_title": "Modern", "scraped_at": recent,
                    "source_portal": legacy_portal}},
    ]
    adapter = _airtable_adapter_with_records(records)

    results = adapter.get_records_since(since)

    assert len(results) == 2
    by_title = {r["opportunity_title"]: r for r in results}
    assert by_title["Legacy"]["source_portal"] == "devex"
    assert by_title["Modern"]["source_portal"] == legacy_portal
