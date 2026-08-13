"""Property-based tests for SheetsAdapter source_portal handling.

Feature: multi-portal-adapter-architecture
"""
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

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
from store.adapter_sheets import SheetsAdapter  # noqa: E402


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
    """Return a SheetsAdapter with all Google Sheets I/O mocked out."""
    with patch("store.adapter_sheets.Credentials"), \
         patch("store.adapter_sheets.gspread") as mock_gspread:
        mock_ws = MagicMock()
        mock_ws.row_values.return_value = SheetsAdapter.HEADERS  # headers already present
        mock_ws.col_values.return_value = ["devex_opportunity_id"]
        mock_gspread.authorize.return_value.open_by_key.return_value.worksheet.return_value = mock_ws

        cfg = MagicMock()
        cfg.service_account_json = "service_account.json"
        cfg.google_sheets_id = "fake-sheet-id"
        cfg.sheets_tab_name = "Sheet1"

        adapter = SheetsAdapter.__new__(SheetsAdapter)
        adapter.config = cfg
        adapter.worksheet = mock_ws
        return adapter


# ---------------------------------------------------------------------------
# Property 14: SheetsAdapter writes source_portal at correct column position
# Validates: Requirements 9.3, 9.4
# ---------------------------------------------------------------------------

SOURCE_PORTAL_VALUES = st.one_of(
    st.just("devex"),
    st.just("samgov"),
    st.just("perplexity"),
    st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-_")),
)


@given(source_portal=SOURCE_PORTAL_VALUES)
@settings(max_examples=100)
def test_property_14_write_record_source_portal_at_correct_column(source_portal):
    """Property 14: SheetsAdapter writes source_portal at correct column position.

    For any OpportunityRecord with any source_portal value, the row written by
    write_record() must contain that value at the index corresponding to
    "source_portal" in HEADERS.

    **Validates: Requirements 9.3, 9.4**
    """
    adapter = _make_adapter()
    record = _make_record(source_portal)

    # Capture the row passed to append_row
    written_rows = []
    adapter.worksheet.append_row.side_effect = lambda row, **kwargs: written_rows.append(row)
    adapter.worksheet.col_values.return_value = ["devex_opportunity_id", "test-001"]

    adapter.write_record(record)

    assert len(written_rows) == 1, "append_row should be called exactly once"
    written_row = written_rows[0]

    expected_index = SheetsAdapter.HEADERS.index("source_portal")
    assert written_row[expected_index] == source_portal, (
        f"Expected source_portal='{source_portal}' at index {expected_index}, "
        f"got '{written_row[expected_index]}'"
    )


# ---------------------------------------------------------------------------
# Property 15: Store get_records_since returns "devex" default for legacy rows
# Validates: Requirements 9.6
# ---------------------------------------------------------------------------

def _make_legacy_row(scraped_at: datetime, extra_fields: dict) -> dict:
    """Build a row dict that lacks source_portal (simulating a legacy row)."""
    row = {
        "devex_opportunity_id": "legacy-001",
        "opportunity_title": "Legacy Opportunity",
        "scraped_at": scraped_at.isoformat(),
    }
    row.update(extra_fields)
    # Explicitly ensure source_portal is absent
    row.pop("source_portal", None)
    return row


EXTRA_FIELDS_STRATEGY = st.fixed_dictionaries({
    "opportunity_title": st.text(min_size=0, max_size=50),
    "funder_organisation": st.text(min_size=0, max_size=50),
})


@given(extra_fields=EXTRA_FIELDS_STRATEGY)
@settings(max_examples=100)
def test_property_15_get_records_since_defaults_source_portal_for_legacy_rows(extra_fields):
    """Property 15: get_records_since returns "devex" default for legacy rows.

    For any stored row that lacks a source_portal field, get_records_since()
    must return "devex" as the source_portal value for that row.

    **Validates: Requirements 9.6**
    """
    adapter = _make_adapter()
    since = datetime(2020, 1, 1)
    scraped_at = datetime(2024, 6, 1, 12, 0, 0)

    legacy_row = _make_legacy_row(scraped_at, extra_fields)
    assert "source_portal" not in legacy_row, "Test setup error: legacy row must not have source_portal"

    adapter.worksheet.get_all_records.return_value = [legacy_row]

    results = adapter.get_records_since(since)

    assert len(results) == 1, "Expected exactly one result row"
    assert results[0]["source_portal"] == "devex", (
        f"Expected source_portal='devex' for legacy row, got '{results[0].get('source_portal')}'"
    )
