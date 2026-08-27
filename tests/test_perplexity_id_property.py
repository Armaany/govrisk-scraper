"""Property-based test for Perplexity opportunity_id determinism.

# Feature: multi-portal-adapter-architecture, Property 13: Perplexity opportunity_id is deterministic

Validates: Requirements 10.3

The Perplexity adapter derives an opportunity_id as
``f"perplexity-{_deterministic_hash(link)}"`` where
``_deterministic_hash(link) = hashlib.sha256(link.encode()).hexdigest()[:12]``.

This module asserts:
  (a) determinism — for the same link the derived id is identical across calls,
  (b) format — the id matches ``^perplexity-[0-9a-f]{12}$``,
exercising ``_deterministic_hash`` directly AND the mapping performed inside
``PerplexityAdapter._parse_response``.
"""
import hashlib
import json
import re

from hypothesis import given, settings
from hypothesis import strategies as st

from config import Config
from portals.perplexity_adapter import PerplexityAdapter, _deterministic_hash


# The canonical Perplexity opportunity_id pattern: "perplexity-" followed by a
# 12-character lowercase hex digest.
PERPLEXITY_ID_PATTERN = re.compile(r"^perplexity-[0-9a-f]{12}$")


def make_config() -> Config:
    return Config(
        devex_email="test@example.com",
        devex_password="password",
        sector_keywords=["governance"],
        target_countries=["Colombia"],
        notification_email="notify@example.com",
        admin_alert_email="admin@example.com",
        perplexity_enabled=True,
        perplexity_api_key="test-perplexity-key",
    )


def _wrap_items(items: list[dict]) -> dict:
    """Build a Perplexity-shaped API response envelope around a JSON item array."""
    return {"choices": [{"message": {"content": json.dumps(items)}}]}


# ---------------------------------------------------------------------------
# Property 13: Perplexity opportunity_id is deterministic
#
# Validates: Requirements 10.3
# ---------------------------------------------------------------------------
@given(link=st.text(min_size=0, max_size=200))
@settings(max_examples=200)
def test_property_13_deterministic_hash_is_deterministic_and_hex(link: str):
    """Property 13: ``_deterministic_hash`` is a pure, deterministic 12-char hex
    digest of the link, so the derived id is identical across calls and matches
    the perplexity-prefixed pattern.

    **Validates: Requirements 10.3**
    """
    digest1 = _deterministic_hash(link)
    digest2 = _deterministic_hash(link)

    # (a) determinism — same input yields the same digest across calls
    assert digest1 == digest2
    # ...and matches the exact SHA-256 truncation contract
    assert digest1 == hashlib.sha256(link.encode()).hexdigest()[:12]
    assert len(digest1) == 12
    assert re.fullmatch(r"[0-9a-f]{12}", digest1), digest1

    # (b) format — the derived id matches ^perplexity-[0-9a-f]{12}$
    opportunity_id = f"perplexity-{digest1}"
    assert PERPLEXITY_ID_PATTERN.match(opportunity_id), opportunity_id


@given(link=st.text(min_size=0, max_size=200))
@settings(max_examples=200)
def test_property_13_parse_response_id_is_deterministic_and_prefixed(link: str):
    """Property 13: The opportunity_id produced by ``_parse_response`` for a given
    link is deterministic across independent parse calls and always matches the
    perplexity-prefixed format.

    **Validates: Requirements 10.3**
    """
    adapter = PerplexityAdapter(make_config())
    item = {
        "opportunity_title": "Some Opportunity",
        "funder_organisation": "Some Funder",
        "country_region": "Colombia",
        "deadline": None,
        "opportunity_link": link,
        "description_snippet": "snippet",
    }

    results1 = adapter._parse_response(_wrap_items([item]))
    results2 = adapter._parse_response(_wrap_items([item]))

    assert len(results1) == 1
    assert len(results2) == 1

    id1 = results1[0]["opportunity_id"]
    id2 = results2[0]["opportunity_id"]

    # (a) determinism — same link maps to the same id across parse calls
    assert id1 == id2
    # ...and equals the direct derivation from _deterministic_hash
    assert id1 == f"perplexity-{_deterministic_hash(link)}"
    # (b) format — matches ^perplexity-[0-9a-f]{12}$
    assert PERPLEXITY_ID_PATTERN.match(id1), id1
    assert results1[0]["source_portal"] == "perplexity"
