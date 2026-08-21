"""Focused test for AirtableAdapter.write_record() payload (Option A).

Feature: multi-portal-adapter-architecture

Asserts the ACTUAL argument passed to ``table.create()`` (not merely
``OpportunityRecord.to_dict()``): it must carry exactly the independently
declared canonical key set (so a serializer regression that adds/drops a key is
caught), with the adapter's documented ``None`` -> "" normalization, must include
the internal ``source_portal`` and ``devex_opportunity_id`` keys, and must NOT
include the external Sheet label ``portal_source``.
"""
import sys
from unittest.mock import MagicMock, patch

# Stub external deps so the adapter imports without credentials/packages.
sys.modules.setdefault("pyairtable", MagicMock())
sys.modules.setdefault("gspread", MagicMock())
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.oauth2", MagicMock())
sys.modules.setdefault("google.oauth2.service_account", MagicMock())

from models import OpportunityRecord, RelevanceScore  # noqa: E402
from store.adapter_airtable import AirtableAdapter  # noqa: E402


# Independently declared canonical key set (NOT derived from to_dict()), so that
# a serializer change which adds or removes a key fails this test.
EXPECTED_CANONICAL_KEYS = {
    "devex_opportunity_id",
    "opportunity_title",
    "funder_organisation",
    "country_region",
    "deadline",
    "contract_value",
    "opportunity_link",
    "description_snippet",
    "matched_keywords",
    "summary",
    "relevance_score",
    "relevance_reason",
    "bid_recommendation",
    "risk_flags",
    "llm_confidence",
    "review_status",
    "llm_called",
    "anna_benchmark",
    "scraped_at",
    "source_portal",
}


def _make_adapter():
    """Build an AirtableAdapter without running __init__ (no live API)."""
    adapter = AirtableAdapter.__new__(AirtableAdapter)
    adapter.table = MagicMock()
    adapter._existing_ids = set()
    return adapter


def test_write_record_payload_is_canonical_and_cached():
    """table.create receives exactly the canonical key set (None-normalized),
    including source_portal and devex_opportunity_id, excluding portal_source;
    the created id is returned and the id cache is updated."""
    record = OpportunityRecord(
        devex_opportunity_id="samgov-ABC123",
        opportunity_title="Water Systems Tender",
        funder_organisation="World Bank",
        country_region="Colombia",
        deadline=None,                 # -> "" after normalization
        contract_value=None,           # -> "" after normalization
        opportunity_link="https://sam.gov/opp/ABC123/view",
        description_snippet="snippet",
        summary=None,                  # -> "" after normalization
        relevance_score=RelevanceScore.HIGH,
        source_portal="samgov",
    )

    # Value-level expectation still derived from the serializer (proves forwarding
    # and the documented None -> "" normalization).
    expected_payload = {
        k: ("" if v is None else v) for k, v in record.to_dict().items()
    }

    adapter = _make_adapter()
    adapter.table.create.return_value = {"id": "rec_XYZ"}

    with patch("store.adapter_airtable.time.sleep") as mock_sleep:
        returned = adapter.write_record(record)

    # table.create called exactly once.
    assert adapter.table.create.call_count == 1
    (called_payload,), _kwargs = adapter.table.create.call_args

    # Independent key-set completeness: exactly the canonical keys, no more, no less.
    assert set(called_payload.keys()) == EXPECTED_CANONICAL_KEYS

    # Value-level forwarding + documented None normalization.
    assert called_payload == expected_payload

    # Canonical internal keys present; external Sheet label absent.
    assert called_payload["source_portal"] == "samgov"
    assert called_payload["devex_opportunity_id"] == "samgov-ABC123"
    assert "portal_source" not in called_payload

    # Documented None normalization applied.
    assert called_payload["deadline"] == ""
    assert called_payload["contract_value"] == ""
    assert called_payload["summary"] == ""

    # Created id returned; id cache updated; sleep mocked (fast).
    assert returned == "rec_XYZ"
    assert "samgov-ABC123" in adapter._existing_ids
    mock_sleep.assert_called_once()