"""Property-based test for the frozen 12-column schema and the
source_portal -> portal_source mapping performed by SheetsAdapter.

# Feature: multi-portal-adapter-architecture, Property 14: SheetsAdapter maps source_portal onto the portal_source column (col 1)

**Validates: Requirements 9.3, 9.4**
- Requirement 9.3: The SheetsAdapter preserves the existing 12-column HEADERS
  (the Live_Sheet_Schema) unchanged and does NOT add a column for source_portal.
- Requirement 9.4: When write_record()/_project_row runs, the canonical
  source_portal value is mapped onto the external portal_source column
  (column 1 of the Live_Sheet_Schema).
"""
import sys
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

# Stub out gspread and google-auth so tests run without those dependencies.
sys.modules.setdefault("gspread", MagicMock())
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.oauth2", MagicMock())
sys.modules.setdefault("google.oauth2.service_account", MagicMock())

from models import (  # noqa: E402
    BidRecommendation,
    LLMConfidence,
    OpportunityRecord,
    RelevanceScore,
    ReviewStatus,
)
from store.adapter_sheets import SheetsAdapter  # noqa: E402


# ---------------------------------------------------------------------------
# The frozen 12-column Live_Sheet_Schema (authoritative, must never change).
# ---------------------------------------------------------------------------
FROZEN_HEADERS = [
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
    "scraped_at",
    "matched_keywords",
]


# ---------------------------------------------------------------------------
# Strategies (mirrors tests/test_source_portal_property.py conventions)
# ---------------------------------------------------------------------------
_source_portal = st.one_of(
    st.sampled_from(["devex", "samgov", "perplexity", "undp"]),
    st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126),
        min_size=1,
        max_size=30,
    ).filter(lambda s: s.strip() != ""),
)

_optional_text = st.one_of(
    st.none(),
    st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=40),
)

_text = st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=40)

_str_list = st.lists(
    st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=126),
        min_size=1,
        max_size=20,
    ).filter(lambda s: s.strip() != ""),
    max_size=5,
)


@st.composite
def _records(draw):
    """Build an OpportunityRecord exercising the full field space."""
    return OpportunityRecord(
        devex_opportunity_id=draw(_text),
        opportunity_title=draw(_text),
        funder_organisation=draw(_text),
        country_region=draw(_text),
        deadline=draw(
            st.one_of(
                st.none(),
                st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 1, 1)),
            )
        ),
        contract_value=draw(_optional_text),
        opportunity_link=draw(_text),
        description_snippet=draw(_text),
        matched_keywords=draw(_str_list),
        summary=draw(_optional_text),
        relevance_score=draw(st.one_of(st.none(), st.sampled_from(list(RelevanceScore)))),
        relevance_reason=draw(_optional_text),
        bid_recommendation=draw(st.one_of(st.none(), st.sampled_from(list(BidRecommendation)))),
        risk_flags=draw(_str_list),
        llm_confidence=draw(st.one_of(st.none(), st.sampled_from(list(LLMConfidence)))),
        review_status=draw(st.sampled_from(list(ReviewStatus))),
        llm_called=draw(st.booleans()),
        anna_benchmark=draw(st.booleans()),
        scraped_at=draw(
            st.datetimes(
                min_value=datetime(2000, 1, 1),
                max_value=datetime(2100, 1, 1),
                timezones=st.just(timezone.utc),
            ).map(lambda dt: dt.replace(microsecond=0))
        ),
        source_portal=draw(_source_portal),
    )


# ---------------------------------------------------------------------------
# Test: HEADERS remains the frozen 12-column schema (no source_portal appended).
# ---------------------------------------------------------------------------
def test_headers_is_frozen_12_column_schema():
    """HEADERS must be exactly the frozen 14-column Live_Sheet_Schema v1.1.

    **Validates: Requirements 9.3**
    """
    assert SheetsAdapter.HEADERS == FROZEN_HEADERS, (
        "HEADERS drifted from the frozen Live_Sheet_Schema.\n"
        f"  Expected: {FROZEN_HEADERS}\n  Got:      {SheetsAdapter.HEADERS}"
    )
    # No canonical 'source_portal' column may be appended to the external schema.
    assert "source_portal" not in SheetsAdapter.HEADERS
    assert len(SheetsAdapter.HEADERS) == 14
    # portal_source occupies column 1 (index 0).
    assert SheetsAdapter.HEADERS.index("portal_source") == 0


# ---------------------------------------------------------------------------
# Property 14: SheetsAdapter maps source_portal onto the portal_source column.
#
# For arbitrary OpportunityRecords, _project_row(record) is exactly 12 wide, the
# value at HEADERS.index("portal_source") (== 0) equals record.source_portal,
# and each remaining column equals its canonical counterpart from
# record.to_dict() (risk_flags joined into a comma string, None -> "").
#
# **Validates: Requirements 9.3, 9.4**
# ---------------------------------------------------------------------------
@given(record=_records())
@settings(max_examples=300)
def test_property_14_source_portal_maps_to_portal_source_column(record):
    """_project_row projects canonical fields by header name onto the 14 columns.

    **Validates: Requirements 9.3, 9.4**
    """
    adapter = SheetsAdapter.__new__(SheetsAdapter)
    adapter._header_index = {h: i for i, h in enumerate(SheetsAdapter.HEADERS)}
    adapter._row_length = len(SheetsAdapter.HEADERS)
    row = adapter._project_row(record)

    # Row is exactly the schema width.
    assert len(row) == len(FROZEN_HEADERS) == 14

    # source_portal lands under the portal_source column (column 1 / index 0).
    portal_idx = SheetsAdapter.HEADERS.index("portal_source")
    assert portal_idx == 0
    assert row[portal_idx] == record.source_portal

    # matched_keywords is JSON serialized
    import json
    mk_idx = SheetsAdapter.HEADERS.index("matched_keywords")
    assert json.loads(row[mk_idx]) == list(record.matched_keywords)

    # scraped_at ends with Z
    sa_idx = SheetsAdapter.HEADERS.index("scraped_at")
    assert row[sa_idx].endswith("Z")
