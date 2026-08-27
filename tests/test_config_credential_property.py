"""Property-based test for Config credential validation.

Feature: multi-portal-adapter-architecture
Property 8: Config credential validation raises on enabled-but-missing key
Validates: Requirements 5.6, 5.7
"""
import os
from unittest.mock import patch

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from config import load_config


# Minimal valid environment for load_config() to reach the credential checks.
BASE_ENV = {
    "DEVEX_EMAIL": "user@example.com",
    "DEVEX_PASSWORD": "secret",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "STORE_TYPE": "sheets",
    "RUN_MODE": "dry_run",
    "NOTIFICATION_EMAIL": "notify@example.com",
    "ADMIN_ALERT_EMAIL": "admin@example.com",
    "GOOGLE_SHEETS_ID": "sheet123",
    "SERVICE_ACCOUNT_JSON": "./service_account.json",
    "SECTOR_KEYWORDS": "AML,corruption",
    "TARGET_COUNTRIES": "Colombia,Mexico",
}

# A key value is "present" only when it is non-empty after stripping, mirroring
# config.py's `os.getenv(...).strip() or None` handling. Restrict the alphabet to
# printable, env-safe characters (no null bytes / control chars, which the OS
# rejects as environment variable values).
_present_key = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=20,
).filter(lambda s: s.strip() != "")
# "Missing" means the env var is absent (None) or empty/whitespace-only.
_missing_key = st.one_of(st.none(), st.just(""), st.just("   "))
_key_state = st.one_of(_present_key, _missing_key)


def _is_missing(key: str | None) -> bool:
    return key is None or key.strip() == ""


# ---------------------------------------------------------------------------
# Property 8: Config credential validation raises on enabled-but-missing key
#
# Validates: Requirements 5.6, 5.7
# ---------------------------------------------------------------------------
@given(
    samgov_enabled=st.booleans(),
    perplexity_enabled=st.booleans(),
    samgov_key=_key_state,
    perplexity_key=_key_state,
)
@settings(max_examples=100)
def test_property_8_config_validation_raises_on_missing_key(
    samgov_enabled, perplexity_enabled, samgov_key, perplexity_key
):
    """Property 8: For any enabled portal whose API key is absent/empty,
    load_config() raises ValueError naming the missing variable.

    **Validates: Requirements 5.6, 5.7**
    """
    samgov_missing = samgov_enabled and _is_missing(samgov_key)
    perplexity_missing = perplexity_enabled and _is_missing(perplexity_key)

    # The property only concerns the enabled-but-missing case.
    assume(samgov_missing or perplexity_missing)

    env = {
        **BASE_ENV,
        "SAM_GOV_ENABLED": "true" if samgov_enabled else "false",
        "PERPLEXITY_ENABLED": "true" if perplexity_enabled else "false",
    }
    if samgov_key is not None:
        env["SAM_GOV_API_KEY"] = samgov_key
    if perplexity_key is not None:
        env["PERPLEXITY_API_KEY"] = perplexity_key

    # config.py validates SAM.gov before Perplexity, so the first missing
    # enabled key determines which variable is named in the error.
    expected_var = "SAM_GOV_API_KEY" if samgov_missing else "PERPLEXITY_API_KEY"

    # Mock config.load_dotenv so a developer's local .env cannot repopulate
    # SAM_GOV_API_KEY / PERPLEXITY_API_KEY after clear=True and invalidate the
    # missing-key cases (keeps the test hermetic).
    with patch.dict(os.environ, env, clear=True), patch("config.load_dotenv"):
        with pytest.raises(ValueError, match=expected_var):
            load_config()
