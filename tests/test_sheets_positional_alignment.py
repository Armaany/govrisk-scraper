"""Positional-alignment compatibility test for SheetsAdapter.write_record().

Feature: multi-portal-adapter-architecture (Task 8.3)

Verifies that writing a record against the existing 12-column header row keeps
every value under its intended header — no column misalignment.

**Validates: Requirements 9.3, 9.4**
"""
import sys
from datetime import date
from unittest.mock import MagicMock

# Stub out gspread and google-auth so tests run without those dependencies.
sys.modules.setdefault("gspread", MagicMock())
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.oauth2", MagicMock())
sys.modules.setdefault("google.oauth2.service_account", MagicMock())

from models import OpportunityRecord, RelevanceScore  # noqa: E402
from store.adapter_sheets import SheetsAdapter  # noqa: E402


def _adapter_capturing_appended_row():
    """Return (adapter, captured_rows) with append_row capturing written rows."""
    adapter = SheetsAdapter.__new__(SheetsAdapter)
    ws = MagicMock()

    captured_rows = []
    ws.append_row.side_effect = lambda row, **kwargs: captured_rows.append(row)
    # write_record returns str(len(col_values(1))) after appending.
    ws.col_values.return_value = ["portal_source", "row1"]

    adapter.worksheet = ws
    return adapter, captured_rows


def test_write_record_positional_alignment():
    """Every field lands under its intended header with the frozen 12-col schema.

    **Validates: Requirements 9.3, 9.4**
    """
    record = OpportunityRecord(
        devex_opportunity_id="devex-123",  # NOT projected onto the sheet
        opportunity_title="Water Systems Tender",
        funder_organisation="World Bank",
        country_region="Colombia",
        deadline=date(2026, 3, 1),
        contract_value="USD 1,000,000",
        opportunity_link="https://example.com/opp/123",
        description_snippet="snippet text",  # NOT projected
        matched_keywords=["water", "sanitation"],  # NOT projected
        summary="A summary of the opportunity.",
        relevance_score=RelevanceScore.HIGH,
        relevance_reason="strong keyword match",  # NOT projected
        bid_recommendation=None,
        risk_flags=["fraud", "delay"],
        review_status=None,
        source_portal="samgov",
    )

    adapter, captured_rows = _adapter_capturing_appended_row()
    adapter.write_record(record)

    assert len(captured_rows) == 1, "append_row must be called exactly once"
    row = captured_rows[0]

    # Row width matches the frozen schema.
    assert len(row) == len(SheetsAdapter.HEADERS) == 12

    idx = {h: i for i, h in enumerate(SheetsAdapter.HEADERS)}

    # source_portal lands under portal_source at column 1 (index 0).
    assert idx["portal_source"] == 0
    assert row[idx["portal_source"]] == "samgov"

    # opportunity_link lands under column 7 (index 6).
    assert idx["opportunity_link"] == 6
    assert row[idx["opportunity_link"]] == "https://example.com/opp/123"

    # Every other value sits under its intended header.
    assert row[idx["opportunity_title"]] == "Water Systems Tender"
    assert row[idx["funder_organisation"]] == "World Bank"
    assert row[idx["country_region"]] == "Colombia"
    assert row[idx["deadline"]] == "2026-03-01"
    assert row[idx["contract_value"]] == "USD 1,000,000"
    assert row[idx["summary"]] == "A summary of the opportunity."
    assert row[idx["relevance_score"]] == "high"
    # bid_recommendation is None on the record -> canonical "" -> "" in row.
    assert row[idx["bid_recommendation"]] == ""
    # risk_flags list joined into a comma-separated string.
    assert row[idx["risk_flags"]] == "fraud, delay"
    # review_status None -> canonical to_dict emits "pending_review".
    assert row[idx["review_status"]] == "pending_review"
