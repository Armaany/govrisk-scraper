"""Property-based test for the canonical to_dict() round-trip.

# Feature: multi-portal-adapter-architecture, Property 2: Canonical to_dict round-trips every field

**Validates: Requirements 6.6, 9.1, 9.2**
- Requirement 9.1: OpportunityRecord has a source_portal: str field defaulting to "devex".
- Requirement 9.2: to_dict() produces a canonical, round-trippable representation containing
  all dataclass fields under their internal names (including source_portal) and never contains
  both portal_source and source_portal.
- Requirement 6.6: the source_portal value is threaded through OpportunityRecord so it persists
  (i.e. it survives a to_dict() -> from_dict() round-trip) for arbitrary portal values.
"""
from datetime import date, datetime, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from models import (
    BidRecommendation,
    LLMConfidence,
    OpportunityRecord,
    RelevanceScore,
    ReviewStatus,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# source_portal is a free-form str field. Include the known portal identifiers
# alongside arbitrary non-empty printable strings to exercise the full space.
_source_portal = st.one_of(
    st.sampled_from(["devex", "samgov", "perplexity", "undp"]),
    st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126),
        min_size=1,
        max_size=30,
    ).filter(lambda s: s.strip() != ""),
)

# Optional string fields: exercise None (must stay distinct from "") and text
# (including empty strings and strings with commas/whitespace).
_optional_text = st.one_of(
    st.none(),
    st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=40),
)

# Required string fields.
_text = st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=40)

# List fields: exercise empty and non-empty lists whose items may contain
# commas and whitespace (these must survive because to_dict emits real lists).
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
    """Build an OpportunityRecord exercising the full field space.

    Covers: None and non-None optionals, empty and non-empty lists, list items
    with commas/whitespace, all enum values (and None), and arbitrary
    source_portal strings.
    """
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
# Property 2: Canonical to_dict round-trips every field
#
# For any OpportunityRecord (with an arbitrary source_portal), the canonical
# to_dict() -> from_dict() round-trip reproduces the original record exactly,
# and the serialized payload carries the internal source_portal key while never
# carrying the external portal_source label.
#
# **Validates: Requirements 6.6, 9.1, 9.2**
# ---------------------------------------------------------------------------
@given(record=_records())
@settings(max_examples=300)
def test_property_2_canonical_to_dict_round_trip(record):
    """from_dict(to_dict(record)) reproduces every field of the original record.

    **Validates: Requirements 6.6, 9.1, 9.2**
    """
    payload = record.to_dict()

    # Requirement 9.2: canonical payload emits the internal source_portal key
    # and never the external portal_source label; never both.
    assert "source_portal" in payload, (
        f"to_dict() must include a 'source_portal' key; got keys: {sorted(payload)}"
    )
    assert "portal_source" not in payload, (
        f"to_dict() must NOT include the external 'portal_source' label; "
        f"got keys: {sorted(payload)}"
    )
    assert payload["source_portal"] == record.source_portal

    # Full-field round-trip: comparing the dataclasses directly exercises every
    # field via the generated __eq__.
    restored = OpportunityRecord.from_dict(payload)
    assert restored == record, (
        "Canonical round-trip did not reproduce the record.\n"
        f"original={record!r}\nrestored={restored!r}"
    )


# ---------------------------------------------------------------------------
# Property 2 (default branch): legacy/missing data defaults to "devex"
#
# Requirement 9.1: source_portal defaults to "devex".
# Legacy rows lacking the field default to "devex".
#
# **Validates: Requirements 9.1**
# ---------------------------------------------------------------------------
@given(
    payload=st.fixed_dictionaries(
        {
            "opportunity_title": st.text(max_size=40),
            "opportunity_link": st.text(max_size=40),
        }
    )
)
@settings(max_examples=100)
def test_property_2_default_devex_for_legacy_data(payload):
    """When source_portal is absent from the payload, from_dict() defaults to "devex".

    **Validates: Requirements 9.1**
    """
    assert "source_portal" not in payload
    restored = OpportunityRecord.from_dict(payload)
    assert restored.source_portal == "devex"


def test_default_source_portal_is_devex():
    """A freshly constructed OpportunityRecord defaults source_portal to "devex"."""
    record = OpportunityRecord(
        devex_opportunity_id="id-1",
        opportunity_title="Title",
        funder_organisation="Org",
        country_region="Colombia",
        deadline=None,
        contract_value=None,
        opportunity_link="https://example.com/1",
        description_snippet="snippet",
    )
    assert record.source_portal == "devex"
    assert record.scraped_at is not None and isinstance(record.scraped_at, datetime)
