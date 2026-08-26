"""Tests for SheetsAdapter with the current 12-column schema.

Feature: multi-portal-adapter-architecture
"""
import sys
from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

# Stub out gspread and google-auth so tests run without those dependencies.
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
        mock_ws.row_values.return_value = SheetsAdapter.HEADERS
        mock_ws.col_values.return_value = ["portal_source"]
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
# Test: HEADERS has exactly 12 columns in the correct order
# ---------------------------------------------------------------------------

def test_headers_has_exactly_12_columns():
    """HEADERS must contain exactly the 12 expected columns."""
    expected = [
        "portal_source",
        "opportunity_title",
        "funder_organisation",
        "country_region",
        "deadline",
        "contract_value",
        "opportunity_link",
        "summary",
        "relevance_score",
        "bid_recommendation",
        "risk_flags",
        "review_status",
    ]
    assert SheetsAdapter.HEADERS == expected, (
        f"HEADERS mismatch.\n  Expected: {expected}\n  Got:      {SheetsAdapter.HEADERS}"
    )


# ---------------------------------------------------------------------------
# Property: write_record places portal_source at column index 0
# (portal_source is the first column in the new 12-column schema)
# ---------------------------------------------------------------------------

SOURCE_PORTAL_VALUES = st.one_of(
    st.just("devex"),
    st.just("undp"),
    st.just("worldbank"),
    st.just("usaid"),
    st.text(
        min_size=1, max_size=30,
        alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-_"),
    ),
)


@given(source_portal=SOURCE_PORTAL_VALUES)
@settings(max_examples=100)
def test_write_record_portal_source_at_column_0(source_portal):
    """For any source_portal value, write_record() places it at index 0 (portal_source column)."""
    adapter = _make_adapter()
    record = _make_record(source_portal)

    written_rows = []
    adapter.worksheet.append_row.side_effect = lambda row, **kwargs: written_rows.append(row)
    adapter.worksheet.col_values.return_value = ["portal_source", "test-001"]

    adapter.write_record(record)

    assert len(written_rows) == 1, "append_row should be called exactly once"
    written_row = written_rows[0]

    assert len(written_row) == len(SheetsAdapter.HEADERS), (
        f"Row length {len(written_row)} != HEADERS length {len(SheetsAdapter.HEADERS)}"
    )
    # portal_source is the first column (index 0)
    assert written_row[0] == source_portal, (
        f"Expected portal_source='{source_portal}' at index 0, got '{written_row[0]}'"
    )


# ---------------------------------------------------------------------------
# Test: to_dict() returns all 12 expected keys
# ---------------------------------------------------------------------------

def test_to_dict_returns_all_12_header_keys():
    """OpportunityRecord.to_dict() must return all keys that appear in HEADERS."""
    record = _make_record("undp")
    d = record.to_dict()
    for col in SheetsAdapter.HEADERS:
        assert col in d, f"to_dict() is missing column '{col}'"
