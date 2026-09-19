"""Tests for new Config fields and load_config() validation (Requirements 5.1–5.9).

All load_config() tests mock ``config.load_dotenv`` so the developer's real
``.env`` cannot contaminate results, and set a clean ``os.environ`` explicitly.
No real credentials, portals, Google Sheet, email, or production services are
accessed.
"""
import os
import pytest
from unittest.mock import patch

from config import Config, _parse_bool_env, load_config


# --- Unit tests for new Config dataclass fields ---

def test_config_new_fields_defaults():
    """Config new fields have correct defaults (Req 5.1–5.5).

    Devex is an optional authenticated portal, so ``devex_enabled`` now defaults
    to ``False`` (consistent with SAM.gov/Perplexity).
    """
    cfg = Config()
    assert cfg.devex_enabled is False
    assert cfg.devex_email == ""
    assert cfg.devex_password == ""
    assert cfg.samgov_api_key is None
    assert cfg.samgov_enabled is False
    assert cfg.perplexity_api_key is None
    assert cfg.perplexity_enabled is False


def test_config_existing_fields_unchanged():
    """Existing Config fields are still present and default correctly (Req 5.9)."""
    cfg = Config(devex_email="a@b.com", devex_password="pw")
    assert cfg.store_type == "sheets"
    assert cfg.run_mode == "dry_run"
    assert cfg.max_results == 50
    assert cfg.headless is True


# --- Validation tests for load_config() ---
#
# BASE_ENV is a minimal valid environment. It intentionally does NOT enable
# Devex; tests that exercise Devex enable/disable set DEVEX_ENABLED explicitly.
BASE_ENV = {
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

DEVEX_CREDS = {
    "DEVEX_EMAIL": "user@example.com",
    "DEVEX_PASSWORD": "secret",
}


def _load_with_env(env: dict):
    """Run load_config() against exactly ``env`` with load_dotenv() neutralised."""
    with patch("config.load_dotenv"), patch.dict(os.environ, env, clear=True):
        return load_config()


# --- SAM.gov / Perplexity behavior (unchanged) ---

def test_load_config_samgov_enabled_without_key_raises():
    """load_config() raises ValueError when SAM_GOV_ENABLED=true but SAM_GOV_API_KEY absent (Req 5.6)."""
    with pytest.raises(ValueError, match="SAM_GOV_API_KEY"):
        _load_with_env({**BASE_ENV, "SAM_GOV_ENABLED": "true"})


def test_load_config_perplexity_enabled_without_key_raises():
    """load_config() raises ValueError when PERPLEXITY_ENABLED=true but PERPLEXITY_API_KEY absent (Req 5.7)."""
    with pytest.raises(ValueError, match="PERPLEXITY_API_KEY"):
        _load_with_env({**BASE_ENV, "PERPLEXITY_ENABLED": "true"})


def test_load_config_samgov_enabled_with_key_ok():
    """load_config() succeeds when SAM_GOV_ENABLED=true and SAM_GOV_API_KEY is set (Req 5.6, 5.8)."""
    cfg = _load_with_env({**BASE_ENV, "SAM_GOV_ENABLED": "true", "SAM_GOV_API_KEY": "test-key"})
    assert cfg.samgov_enabled is True
    assert cfg.samgov_api_key == "test-key"


def test_load_config_perplexity_enabled_with_key_ok():
    """load_config() succeeds when PERPLEXITY_ENABLED=true and PERPLEXITY_API_KEY is set (Req 5.7, 5.8)."""
    cfg = _load_with_env({**BASE_ENV, "PERPLEXITY_ENABLED": "true", "PERPLEXITY_API_KEY": "pplx-key"})
    assert cfg.perplexity_enabled is True
    assert cfg.perplexity_api_key == "pplx-key"


def test_load_config_devex_enabled_reads_env():
    """load_config() reads DEVEX_ENABLED from env (Req 5.8)."""
    cfg = _load_with_env({**BASE_ENV, **DEVEX_CREDS, "DEVEX_ENABLED": "true"})
    assert cfg.devex_enabled is True


def test_load_config_defaults_when_portal_vars_absent():
    """load_config() uses safe defaults when portal env vars are absent (Req 5.1–5.5)."""
    cfg = _load_with_env(dict(BASE_ENV))
    # Devex now defaults to disabled when DEVEX_ENABLED is absent.
    assert cfg.devex_enabled is False
    assert cfg.samgov_enabled is False
    assert cfg.samgov_api_key is None
    assert cfg.perplexity_enabled is False
    assert cfg.perplexity_api_key is None


# --- Devex disabled-without-credentials contract ---

def test_devex_disabled_without_credentials_loads():
    """DEVEX_ENABLED=false with both credentials absent → config loads (Req 5.3)."""
    cfg = _load_with_env({**BASE_ENV, "DEVEX_ENABLED": "false"})
    assert cfg.devex_enabled is False
    assert cfg.devex_email == ""
    assert cfg.devex_password == ""


def test_devex_disabled_with_blank_whitespace_credentials_loads():
    """DEVEX_ENABLED=false with blank/whitespace credentials → config loads (Req 5.3)."""
    cfg = _load_with_env({
        **BASE_ENV,
        "DEVEX_ENABLED": "false",
        "DEVEX_EMAIL": "   ",
        "DEVEX_PASSWORD": "\t  ",
    })
    assert cfg.devex_enabled is False
    # Whitespace-only credentials are normalised to empty.
    assert cfg.devex_email == ""
    assert cfg.devex_password == ""


def test_devex_absent_defaults_to_disabled():
    """DEVEX_ENABLED absent → defaults to False; missing creds do not raise (Req 5.1)."""
    cfg = _load_with_env(dict(BASE_ENV))
    assert cfg.devex_enabled is False


def test_devex_enabled_missing_email_raises_naming_email():
    """DEVEX_ENABLED=true with missing email → clear error naming DEVEX_EMAIL (Req 5.4)."""
    with pytest.raises(ValueError, match="DEVEX_EMAIL"):
        _load_with_env({**BASE_ENV, "DEVEX_ENABLED": "true", "DEVEX_PASSWORD": "secret"})


def test_devex_enabled_blank_email_raises_naming_email():
    """DEVEX_ENABLED=true with whitespace-only email → error naming DEVEX_EMAIL (Req 5.4)."""
    with pytest.raises(ValueError, match="DEVEX_EMAIL"):
        _load_with_env({
            **BASE_ENV, "DEVEX_ENABLED": "true",
            "DEVEX_EMAIL": "   ", "DEVEX_PASSWORD": "secret",
        })


def test_devex_enabled_missing_password_raises_naming_password():
    """DEVEX_ENABLED=true with missing password → clear error naming DEVEX_PASSWORD (Req 5.4)."""
    with pytest.raises(ValueError, match="DEVEX_PASSWORD"):
        _load_with_env({**BASE_ENV, "DEVEX_ENABLED": "true", "DEVEX_EMAIL": "user@example.com"})


def test_devex_enabled_with_both_credentials_loads():
    """DEVEX_ENABLED=true with both credentials present → config loads (Req 5.4)."""
    cfg = _load_with_env({**BASE_ENV, **DEVEX_CREDS, "DEVEX_ENABLED": "true"})
    assert cfg.devex_enabled is True
    assert cfg.devex_email == "user@example.com"
    assert cfg.devex_password == "secret"
