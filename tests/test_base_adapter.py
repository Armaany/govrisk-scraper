"""Unit tests for BasePortalAdapter ABC enforcement (Requirements 1.3)."""
import pytest

from portals.base_adapter import BasePortalAdapter


# ---------------------------------------------------------------------------
# Helpers — minimal Config stand-in so we don't need a real .env
# ---------------------------------------------------------------------------

class _FakeConfig:
    pass


# ---------------------------------------------------------------------------
# Tests: ABC enforcement
# ---------------------------------------------------------------------------

def test_missing_both_abstract_members_raises_type_error():
    """A subclass that implements neither portal_name nor fetch_opportunities raises TypeError."""

    class IncompleteAdapter(BasePortalAdapter):
        pass

    with pytest.raises(TypeError):
        IncompleteAdapter(_FakeConfig())


def test_missing_portal_name_raises_type_error():
    """A subclass that omits portal_name raises TypeError on instantiation."""

    class NoPortalName(BasePortalAdapter):
        async def fetch_opportunities(self) -> list[dict]:
            return []

    with pytest.raises(TypeError):
        NoPortalName(_FakeConfig())


def test_missing_fetch_opportunities_raises_type_error():
    """A subclass that omits fetch_opportunities raises TypeError on instantiation."""

    class NoFetch(BasePortalAdapter):
        @property
        def portal_name(self) -> str:
            return "test"

    with pytest.raises(TypeError):
        NoFetch(_FakeConfig())


def test_complete_subclass_instantiates_successfully():
    """A fully implemented subclass can be instantiated without error."""

    class FullAdapter(BasePortalAdapter):
        @property
        def portal_name(self) -> str:
            return "test"

        async def fetch_opportunities(self) -> list[dict]:
            return []

    adapter = FullAdapter(_FakeConfig())
    assert adapter.portal_name == "test"


def test_config_stored_on_instance():
    """__init__ stores the config object as self.config."""

    class FullAdapter(BasePortalAdapter):
        @property
        def portal_name(self) -> str:
            return "test"

        async def fetch_opportunities(self) -> list[dict]:
            return []

    cfg = _FakeConfig()
    adapter = FullAdapter(cfg)
    assert adapter.config is cfg
