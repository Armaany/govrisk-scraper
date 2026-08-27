"""Property-based and adversarial tests for SheetsAdapter (Option A, schema v1.0).

Feature: multi-portal-adapter-architecture

Under the Option A schema contract the live Google Sheet keeps its frozen
12-column ``Live_Sheet_Schema`` whose first column is the external label
``portal_source``. The canonical model field is ``source_portal``; the
``SheetsAdapter`` projects it onto the external ``portal_source`` column.

This module covers:
- Property 14: source_portal is written to the portal_source column (col 1).
- Property 15: get_records_since() is unsupported (raises).
- Startup header validation (schema v1.0): initialize-if-empty, else strict
  validation rejecting missing/duplicate/reordered/unexpected headers, never
  auto-repairing a populated header, validating before any write.
- Deprecation of get_all_ids()/record_exists() (no persisted ID column).
"""
import sys
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Stub out gspread and google-auth before importing the adapter so tests run
# without those optional dependencies installed.
sys.modules.setdefault("gspread", MagicMock())
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.oauth2", MagicMock())
sys.modules.setdefault("google.oauth2.service_account", MagicMock())

from models import OpportunityRecord  # noqa: E402
from store.adapter_sheets import SheetsAdapter, SheetsSchemaError  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(source_portal: str) -> OpportunityRecord:
    """Build a minimal OpportunityRecord with the given source_portal."""
    return OpportunityRecord(
        devex_opportunity_id="test-001",
        opportunity_title="Test Opportunity",
        funder_organisation="Test Org",
        country_region="Colombia",
        deadline=None,
        contract_value=None,
        opportunity_link="https://example.com/opp/1",
        description_snippet="A test opportunity.",
        source_portal=source_portal,
    )


def _make_adapter() -> SheetsAdapter:
    """Return a SheetsAdapter with all Google Sheets I/O mocked out (no __init__)."""
    adapter = SheetsAdapter.__new__(SheetsAdapter)
    adapter.config = MagicMock()
    adapter.worksheet = MagicMock()
    return adapter


# ---------------------------------------------------------------------------
# Property 14: SheetsAdapter maps canonical source_portal onto the external
# portal_source column (column 1 of the frozen Live_Sheet_Schema).
# Validates: Requirements 9.3, 9.4
# ---------------------------------------------------------------------------

SOURCE_PORTAL_VALUES = st.one_of(
    st.just("devex"),
    st.just("undp"),
    st.just("worldbank"),
    st.just("usaid"),
    st.just("iadb"),
    st.just("oecd"),
    st.just("samgov"),
    st.just("perplexity"),
    st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-_")),
)


@given(source_portal=SOURCE_PORTAL_VALUES)
@settings(max_examples=100)
def test_property_14_write_record_maps_source_portal_to_portal_source_column(source_portal):
    """Property 14: write_record projects canonical source_portal onto the
    external portal_source column of the 14-column schema v1.1.

    **Validates: Requirements 9.3, 9.4**
    """
    adapter = _make_adapter()
    adapter._header_index = {h: i for i, h in enumerate(SheetsAdapter.HEADERS)}
    adapter._row_length = len(SheetsAdapter.HEADERS)
    record = _make_record(source_portal)

    assert "source_portal" not in SheetsAdapter.HEADERS
    assert len(SheetsAdapter.HEADERS) == 14

    written_rows = []
    adapter.worksheet.append_row.side_effect = lambda row, **kwargs: written_rows.append(row)
    adapter.worksheet.col_values.return_value = ["portal_source", "test-001"]

    adapter.write_record(record)

    assert len(written_rows) == 1, "append_row should be called exactly once"
    written_row = written_rows[0]
    assert len(written_row) == 14

    expected_index = SheetsAdapter.HEADERS.index("portal_source")
    assert expected_index == 0
    assert written_row[expected_index] == source_portal, (
        f"Expected source_portal='{source_portal}' at portal_source index "
        f"{expected_index}, got '{written_row[expected_index]}'"
    )


# ---------------------------------------------------------------------------
# Property 15: SheetsAdapter.get_records_since() is unsupported under the
# Live_Sheet_Schema (no scraped_at column) and raises NotImplementedError.
# Validates: Requirements 9.6
# ---------------------------------------------------------------------------

def test_property_15_get_records_since_unsupported_for_sheets():
    """Property 15: get_records_since() is deprecated for the Live_Sheet_Schema
    and raises NotImplementedError (there is no scraped_at column to filter on).

    **Validates: Requirements 9.6**
    """
    adapter = _make_adapter()
    with pytest.raises(NotImplementedError):
        adapter.get_records_since(datetime(2020, 1, 1))


# ---------------------------------------------------------------------------
# Startup header validation (schema v1.0, Option A)
# Validates: Requirements 9.8
# ---------------------------------------------------------------------------

def test_valid_header_passes_validation():
    """The exact canonical 12-column header validates without raising."""
    adapter = _make_adapter()
    adapter._validate_headers(list(SheetsAdapter.HEADERS))  # must not raise


def test_empty_sheet_initializes_canonical_header():
    """An empty sheet is initialized by writing the canonical 12-column header."""
    adapter = _make_adapter()
    adapter.worksheet.row_values.return_value = []  # empty row 1
    appended = []
    adapter.worksheet.append_row.side_effect = lambda row, **kwargs: appended.append(row)

    adapter._ensure_headers()

    assert appended == [SheetsAdapter.HEADERS]


def test_missing_required_header_rejected():
    """A header row missing a required column is rejected."""
    adapter = _make_adapter()
    bad = list(SheetsAdapter.HEADERS)
    bad.pop()  # drop 'review_status'
    with pytest.raises(SheetsSchemaError):
        adapter._validate_headers(bad)


def test_duplicate_exact_header_rejected():
    """A header row with an exact duplicate column is rejected."""
    adapter = _make_adapter()
    bad = list(SheetsAdapter.HEADERS)
    bad[1] = "portal_source"  # duplicate of column 0
    with pytest.raises(SheetsSchemaError):
        adapter._validate_headers(bad)


def test_duplicate_after_trim_and_case_rejected():
    """Duplicates distinguishable only by whitespace/case are rejected."""
    adapter = _make_adapter()
    bad = list(SheetsAdapter.HEADERS)
    bad[1] = "  PORTAL_SOURCE  "  # normalizes to 'portal_source' -> duplicate
    with pytest.raises(SheetsSchemaError):
        adapter._validate_headers(bad)


def test_reordered_header_accepted():
    """A reordered header (same set, different order) is accepted under v1.1."""
    adapter = _make_adapter()
    reordered = list(SheetsAdapter.HEADERS)
    reordered[0], reordered[1] = reordered[1], reordered[0]
    adapter._validate_headers(reordered)  # must not raise


def test_unexpected_extra_header_preserved():
    """Extra unknown columns are allowed under v1.1 (blanks written under them)."""
    adapter = _make_adapter()
    extended = list(SheetsAdapter.HEADERS) + ["extra_column"]
    adapter._validate_headers(extended)  # must not raise


def test_validation_runs_before_any_write_on_populated_bad_header():
    """When row 1 is populated but has a missing required column, _ensure_headers
    raises and never attempts to write/repair the header."""
    adapter = _make_adapter()
    bad = list(SheetsAdapter.HEADERS)
    bad.remove("scraped_at")  # missing required column
    adapter.worksheet.row_values.return_value = bad

    with pytest.raises(SheetsSchemaError):
        adapter._ensure_headers()

    adapter.worksheet.append_row.assert_not_called()  # no auto-repair / write


# ---------------------------------------------------------------------------
# Deprecated ID-based methods (no persisted opportunity-ID column)
# Validates: Requirements 9.9
# ---------------------------------------------------------------------------

def test_get_all_ids_raises_before_worksheet_calls():
    """get_all_ids() raises NotImplementedError without touching the worksheet."""
    adapter = _make_adapter()
    with pytest.raises(NotImplementedError):
        adapter.get_all_ids()
    adapter.worksheet.col_values.assert_not_called()
    adapter.worksheet.get_all_values.assert_not_called()


def test_record_exists_raises_before_worksheet_calls():
    """record_exists() raises NotImplementedError without touching the worksheet."""
    adapter = _make_adapter()
    with pytest.raises(NotImplementedError):
        adapter.record_exists("devex-123")
    adapter.worksheet.col_values.assert_not_called()
    adapter.worksheet.get_all_values.assert_not_called()
