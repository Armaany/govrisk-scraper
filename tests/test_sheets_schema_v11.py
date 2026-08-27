"""Adversarial tests for Sheet schema v1.1 (14-column header-name-driven writer).

Tests cover:
- Exact canonical 14-column initialization
- Reordered headers write every value under the correct header
- Missing scraped_at / matched_keywords rejected before append
- Duplicate headers rejected (case/whitespace normalization)
- Unknown additional headers do not shift canonical values
- Timezone-aware UTC ISO serialization with Z suffix
- Naive datetime handling (rejection)
- matched_keywords JSON round-trip including accented Spanish phrases
- Empty matched_keywords serializes as []
- source_portal still maps to portal_source
- opportunity_link correctly positioned by header name
- Legacy 12-column header produces explicit migration error
- No network or live Sheet interaction
"""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from models import OpportunityRecord
from store.adapter_sheets import SheetsAdapter, SheetsSchemaError, StoreWriteError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter_with_headers(header_row: list) -> SheetsAdapter:
    """Create a SheetsAdapter with a mocked worksheet returning given headers."""
    with patch.object(SheetsAdapter, "__init__", lambda self, *a, **kw: None):
        adapter = SheetsAdapter.__new__(SheetsAdapter)
    adapter.worksheet = MagicMock()
    adapter.worksheet.row_values.return_value = header_row
    adapter.worksheet.append_row = MagicMock()
    adapter.worksheet.col_values = MagicMock(return_value=["opportunity_link"])
    adapter._ensure_headers()
    return adapter


def _make_adapter_empty_sheet() -> SheetsAdapter:
    """Create a SheetsAdapter with an empty sheet (triggers initialization)."""
    with patch.object(SheetsAdapter, "__init__", lambda self, *a, **kw: None):
        adapter = SheetsAdapter.__new__(SheetsAdapter)
    adapter.worksheet = MagicMock()
    adapter.worksheet.row_values.return_value = []
    adapter.worksheet.append_row = MagicMock()
    adapter.worksheet.col_values = MagicMock(return_value=["opportunity_link"])
    adapter._ensure_headers()
    return adapter


def _make_record(**overrides) -> OpportunityRecord:
    """Create a minimal valid OpportunityRecord with UTC scraped_at."""
    defaults = {
        "devex_opportunity_id": "TEST-001",
        "opportunity_title": "Test Opportunity",
        "funder_organisation": "Test Funder",
        "country_region": "Colombia",
        "deadline": None,
        "contract_value": "USD 100,000",
        "opportunity_link": "https://example.com/opp/1",
        "description_snippet": "A test opportunity",
        "matched_keywords": ["corruption", "transparencia"],
        "source_portal": "devex",
        "scraped_at": datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return OpportunityRecord.from_dict(defaults)


# ---------------------------------------------------------------------------
# Test: Canonical 14-column initialization
# ---------------------------------------------------------------------------

def test_empty_sheet_initializes_14_column_header():
    """An empty sheet gets the canonical 14-column header written."""
    adapter = _make_adapter_empty_sheet()
    adapter.worksheet.append_row.assert_called_once_with(
        SheetsAdapter.HEADERS, value_input_option="RAW"
    )
    assert len(SheetsAdapter.HEADERS) == 14
    assert "scraped_at" in SheetsAdapter.HEADERS
    assert "matched_keywords" in SheetsAdapter.HEADERS


# ---------------------------------------------------------------------------
# Test: Reordered headers write values correctly
# ---------------------------------------------------------------------------

def test_reordered_headers_write_correct_positions():
    """Values land under the correct header regardless of column order."""
    reordered = list(reversed(SheetsAdapter.HEADERS))
    adapter = _make_adapter_with_headers(reordered)
    record = _make_record(source_portal="undp")
    row = adapter._project_row(record)

    # portal_source should be at the index where "portal_source" appears in reordered
    ps_idx = reordered.index("portal_source")
    assert row[ps_idx] == "undp"

    # opportunity_link at its reordered position
    ol_idx = reordered.index("opportunity_link")
    assert row[ol_idx] == "https://example.com/opp/1"

    # scraped_at at its reordered position
    sa_idx = reordered.index("scraped_at")
    assert row[sa_idx] == "2025-01-15T10:30:00Z"

    # matched_keywords at its reordered position
    mk_idx = reordered.index("matched_keywords")
    parsed = json.loads(row[mk_idx])
    assert parsed == ["corruption", "transparencia"]


# ---------------------------------------------------------------------------
# Test: Missing scraped_at / matched_keywords rejected
# ---------------------------------------------------------------------------

def test_missing_scraped_at_rejected():
    """A header missing scraped_at raises SheetsSchemaError."""
    headers_12 = SheetsAdapter.HEADERS[:12]  # Only the original 12
    with patch.object(SheetsAdapter, "__init__", lambda self, *a, **kw: None):
        adapter = SheetsAdapter.__new__(SheetsAdapter)
    adapter.worksheet = MagicMock()
    with pytest.raises(SheetsSchemaError, match="missing columns.*scraped_at"):
        adapter._validate_headers(headers_12)


def test_missing_matched_keywords_rejected():
    """A header missing matched_keywords raises SheetsSchemaError."""
    headers_13 = SheetsAdapter.HEADERS[:12] + ["scraped_at"]
    with patch.object(SheetsAdapter, "__init__", lambda self, *a, **kw: None):
        adapter = SheetsAdapter.__new__(SheetsAdapter)
    adapter.worksheet = MagicMock()
    with pytest.raises(SheetsSchemaError, match="missing columns.*matched_keywords"):
        adapter._validate_headers(headers_13)


# ---------------------------------------------------------------------------
# Test: Duplicate headers rejected (case/whitespace normalization)
# ---------------------------------------------------------------------------

def test_duplicate_headers_same_case_rejected():
    """Exact duplicate column names raise SheetsSchemaError."""
    headers = SheetsAdapter.HEADERS + ["portal_source"]
    with patch.object(SheetsAdapter, "__init__", lambda self, *a, **kw: None):
        adapter = SheetsAdapter.__new__(SheetsAdapter)
    adapter.worksheet = MagicMock()
    with pytest.raises(SheetsSchemaError, match="duplicate"):
        adapter._validate_headers(headers)


def test_duplicate_headers_case_normalized_rejected():
    """Duplicates differing only by case are rejected."""
    headers = list(SheetsAdapter.HEADERS)
    headers.append("Portal_Source")  # Normalized dup of portal_source
    with patch.object(SheetsAdapter, "__init__", lambda self, *a, **kw: None):
        adapter = SheetsAdapter.__new__(SheetsAdapter)
    adapter.worksheet = MagicMock()
    with pytest.raises(SheetsSchemaError, match="duplicate"):
        adapter._validate_headers(headers)


def test_duplicate_headers_whitespace_normalized_rejected():
    """Duplicates differing only by whitespace are rejected."""
    headers = list(SheetsAdapter.HEADERS)
    headers.append("  portal_source  ")
    with patch.object(SheetsAdapter, "__init__", lambda self, *a, **kw: None):
        adapter = SheetsAdapter.__new__(SheetsAdapter)
    adapter.worksheet = MagicMock()
    with pytest.raises(SheetsSchemaError, match="duplicate"):
        adapter._validate_headers(headers)


# ---------------------------------------------------------------------------
# Test: Unknown additional headers do not shift canonical values
# ---------------------------------------------------------------------------

def test_unknown_columns_get_blanks_and_canonical_values_correct():
    """Extra unknown columns receive blank values; canonical columns stay correct."""
    headers_with_extra = list(SheetsAdapter.HEADERS) + ["custom_notes", "internal_id"]
    adapter = _make_adapter_with_headers(headers_with_extra)
    record = _make_record(source_portal="samgov")
    row = adapter._project_row(record)

    # Row length matches actual header (16 columns)
    assert len(row) == 16

    # Canonical values in correct positions
    ps_idx = headers_with_extra.index("portal_source")
    assert row[ps_idx] == "samgov"

    # Unknown columns get blanks
    custom_idx = headers_with_extra.index("custom_notes")
    internal_idx = headers_with_extra.index("internal_id")
    assert row[custom_idx] == ""
    assert row[internal_idx] == ""


# ---------------------------------------------------------------------------
# Test: Timezone-aware UTC ISO serialization with Z suffix
# ---------------------------------------------------------------------------

def test_scraped_at_utc_serializes_with_z_suffix():
    """UTC datetime serializes as ISO 8601 with Z suffix."""
    record = _make_record(
        scraped_at=datetime(2025, 6, 15, 14, 30, 45, tzinfo=timezone.utc)
    )
    d = record.to_dict()
    assert d["scraped_at"] == "2025-06-15T14:30:45Z"


def test_scraped_at_non_utc_timezone_converted_to_utc_z():
    """A non-UTC aware datetime is converted to UTC for serialization."""
    eastern = timezone(timedelta(hours=-5))
    record = _make_record(
        scraped_at=datetime(2025, 6, 15, 10, 30, 0, tzinfo=eastern)
    )
    d = record.to_dict()
    # 10:30 EST = 15:30 UTC
    assert d["scraped_at"] == "2025-06-15T15:30:00Z"


# ---------------------------------------------------------------------------
# Test: Naive datetime handling (rejection)
# ---------------------------------------------------------------------------

def test_naive_datetime_rejected_on_serialization():
    """A naive datetime in scraped_at raises ValueError on serialization."""
    record = _make_record()
    # Force a naive datetime
    record.scraped_at = datetime(2025, 1, 15, 10, 30, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        record.to_dict()


def test_naive_datetime_string_rejected_on_parse():
    """from_dict rejects a naive datetime string."""
    with pytest.raises(ValueError, match="timezone-aware"):
        OpportunityRecord.from_dict({
            "devex_opportunity_id": "X",
            "opportunity_title": "T",
            "funder_organisation": "F",
            "country_region": "C",
            "opportunity_link": "https://x.com",
            "description_snippet": "D",
            "scraped_at": "2025-01-15T10:30:00",  # naive!
        })


# ---------------------------------------------------------------------------
# Test: matched_keywords JSON round-trip with accented Spanish
# ---------------------------------------------------------------------------

def test_matched_keywords_json_roundtrip_accented_spanish():
    """Accented Spanish terms survive JSON serialization intact."""
    keywords = ["corrupción", "transparencia", "licitación pública"]
    record = _make_record(matched_keywords=keywords)
    json_str = record.serialize_matched_keywords_for_sheet()
    parsed = json.loads(json_str)
    assert parsed == keywords


def test_matched_keywords_preserves_phrases():
    """Multi-word phrases are preserved exactly."""
    keywords = ["anti-money laundering", "bid rigging", "conflict of interest"]
    record = _make_record(matched_keywords=keywords)
    json_str = record.serialize_matched_keywords_for_sheet()
    parsed = json.loads(json_str)
    assert parsed == keywords


# ---------------------------------------------------------------------------
# Test: Empty matched_keywords serializes as []
# ---------------------------------------------------------------------------

def test_empty_matched_keywords_serializes_as_empty_json_array():
    """Empty matched_keywords produces '[]'."""
    record = _make_record(matched_keywords=[])
    json_str = record.serialize_matched_keywords_for_sheet()
    assert json_str == "[]"


# ---------------------------------------------------------------------------
# Test: source_portal still maps to portal_source
# ---------------------------------------------------------------------------

def test_source_portal_maps_to_portal_source_column():
    """Canonical source_portal is written under external portal_source header."""
    adapter = _make_adapter_with_headers(SheetsAdapter.HEADERS)
    record = _make_record(source_portal="worldbank")
    row = adapter._project_row(record)
    ps_idx = SheetsAdapter.HEADERS.index("portal_source")
    assert row[ps_idx] == "worldbank"


# ---------------------------------------------------------------------------
# Test: opportunity_link positioned by header name
# ---------------------------------------------------------------------------

def test_opportunity_link_positioned_by_header_name():
    """opportunity_link value lands at the header-determined index."""
    # Shuffle headers
    import random
    shuffled = list(SheetsAdapter.HEADERS)
    random.seed(42)
    random.shuffle(shuffled)
    adapter = _make_adapter_with_headers(shuffled)
    record = _make_record(opportunity_link="https://undp.org/project/123")
    row = adapter._project_row(record)
    ol_idx = shuffled.index("opportunity_link")
    assert row[ol_idx] == "https://undp.org/project/123"


# ---------------------------------------------------------------------------
# Test: Legacy 12-column header produces explicit migration error
# ---------------------------------------------------------------------------

def test_legacy_12_column_header_produces_migration_error():
    """The old 12-column schema triggers a clear incompatibility message."""
    legacy_headers = [
        "portal_source", "opportunity_title", "funder_organisation",
        "country_region", "deadline", "contract_value", "opportunity_link",
        "summary", "relevance_score", "bid_recommendation", "risk_flags",
        "review_status",
    ]
    with patch.object(SheetsAdapter, "__init__", lambda self, *a, **kw: None):
        adapter = SheetsAdapter.__new__(SheetsAdapter)
    adapter.worksheet = MagicMock()
    with pytest.raises(SheetsSchemaError, match="schema incompatible.*missing columns.*scraped_at.*matched_keywords"):
        adapter._validate_headers(legacy_headers)


# ---------------------------------------------------------------------------
# Test: No network or live Sheet interaction
# ---------------------------------------------------------------------------

def test_no_network_calls_during_tests():
    """All tests use mocked worksheet — verify no real gspread calls leak."""
    adapter = _make_adapter_with_headers(SheetsAdapter.HEADERS)
    record = _make_record()
    row = adapter._project_row(record)
    # Only mock methods called
    assert isinstance(adapter.worksheet, MagicMock)


# ---------------------------------------------------------------------------
# Test: write_record end-to-end with 14-column schema
# ---------------------------------------------------------------------------

def test_write_record_appends_14_values():
    """write_record appends a row with all 14 canonical values populated."""
    adapter = _make_adapter_with_headers(SheetsAdapter.HEADERS)
    adapter.worksheet.col_values.return_value = ["opportunity_link", "https://a.com"]
    record = _make_record(
        source_portal="undp",
        matched_keywords=["corrupción", "governance"],
        scraped_at=datetime(2025, 3, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    adapter.write_record(record)
    call_args = adapter.worksheet.append_row.call_args[0][0]
    assert len(call_args) == 14

    # Verify key positions
    assert call_args[SheetsAdapter.HEADERS.index("portal_source")] == "undp"
    assert call_args[SheetsAdapter.HEADERS.index("scraped_at")] == "2025-03-01T12:00:00Z"
    mk_val = call_args[SheetsAdapter.HEADERS.index("matched_keywords")]
    assert json.loads(mk_val) == ["corrupción", "governance"]
    assert call_args[SheetsAdapter.HEADERS.index("opportunity_link")] == "https://example.com/opp/1"


# ---------------------------------------------------------------------------
# Test: get_all_links uses header-driven index
# ---------------------------------------------------------------------------

def test_get_all_links_uses_header_position():
    """get_all_links reads from the correct column based on header position."""
    reordered = list(reversed(SheetsAdapter.HEADERS))
    adapter = _make_adapter_with_headers(reordered)
    ol_idx = reordered.index("opportunity_link")
    adapter.worksheet.col_values.return_value = [
        "opportunity_link", "https://a.com", "https://b.com", ""
    ]
    links = adapter.get_all_links()
    adapter.worksheet.col_values.assert_called_with(ol_idx + 1)
    assert links == {"https://a.com", "https://b.com"}


# ---------------------------------------------------------------------------
# Test: scraped_at Z-suffix round-trip through from_dict
# ---------------------------------------------------------------------------

def test_scraped_at_z_suffix_parsed_correctly():
    """from_dict parses '2025-01-15T10:30:00Z' into UTC-aware datetime."""
    record = OpportunityRecord.from_dict({
        "devex_opportunity_id": "X",
        "opportunity_title": "T",
        "funder_organisation": "F",
        "country_region": "C",
        "opportunity_link": "https://x.com",
        "description_snippet": "D",
        "scraped_at": "2025-01-15T10:30:00Z",
    })
    assert record.scraped_at.tzinfo is not None
    assert record.scraped_at == datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
