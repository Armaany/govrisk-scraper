"""Tests for new Config fields and load_config() validation (Requirements 5.1–5.9)."""
import os
import pytest
from unittest.mock import patch

from config import Config, _parse_bool_env, load_config


# --- Unit tests for new Config dataclass fields ---

def test_config_new_fields_defaults():
    """Config new fields have correct defaults (Req 5.1–5.5)."""
    cfg = Config(devex_email="a@b.com", devex_password="pw")
    assert cfg.devex_enabled is True
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


def test_load_config_samgov_enabled_without_key_raises():
    """load_config() raises ValueError when SAM_GOV_ENABLED=true but SAM_GOV_API_KEY absent (Req 5.6)."""
    env = {**BASE_ENV, "SAM_GOV_ENABLED": "true"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match="SAM_GOV_API_KEY"):
            load_config()


def test_load_config_perplexity_enabled_without_key_raises():
    """load_config() raises ValueError when PERPLEXITY_ENABLED=true but PERPLEXITY_API_KEY absent (Req 5.7)."""
    env = {**BASE_ENV, "PERPLEXITY_ENABLED": "true"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match="PERPLEXITY_API_KEY"):
            load_config()


def test_load_config_samgov_enabled_with_key_ok():
    """load_config() succeeds when SAM_GOV_ENABLED=true and SAM_GOV_API_KEY is set (Req 5.6, 5.8)."""
    env = {**BASE_ENV, "SAM_GOV_ENABLED": "true", "SAM_GOV_API_KEY": "test-key"}
    with patch.dict(os.environ, env, clear=True):
        cfg = load_config()
    assert cfg.samgov_enabled is True
    assert cfg.samgov_api_key == "test-key"


def test_load_config_perplexity_enabled_with_key_ok():
    """load_config() succeeds when PERPLEXITY_ENABLED=true and PERPLEXITY_API_KEY is set (Req 5.7, 5.8)."""
    env = {**BASE_ENV, "PERPLEXITY_ENABLED": "true", "PERPLEXITY_API_KEY": "pplx-key"}
    with patch.dict(os.environ, env, clear=True):
        cfg = load_config()
    assert cfg.perplexity_enabled is True
    assert cfg.perplexity_api_key == "pplx-key"


def test_load_config_devex_enabled_reads_env(monkeypatch):
    """load_config() reads DEVEX_ENABLED from env (Req 5.8)."""
    env = {**BASE_ENV, "DEVEX_ENABLED": "false"}
    with patch.dict(os.environ, env, clear=True):
        cfg = load_config()
    assert cfg.devex_enabled is False


def test_load_config_defaults_when_portal_vars_absent():
    """load_config() uses safe defaults when portal env vars are absent (Req 5.1–5.5)."""
    with patch.dict(os.environ, BASE_ENV, clear=True):
        cfg = load_config()
    assert cfg.devex_enabled is True
    assert cfg.samgov_enabled is False
    assert cfg.samgov_api_key is None
    assert cfg.perplexity_enabled is False
    assert cfg.perplexity_api_key is None
