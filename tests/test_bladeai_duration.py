from types import ModuleType
import sys

import pytest

from stage2_service.bladeai_duration import _explicit_seconds, preserve_explicit_fault_duration


@pytest.mark.parametrize("value", [True, -1, 2.5, "bad", "30s"])
def test_invalid_explicit_duration_is_not_replaced_by_a_longer_fault(value):
    with pytest.raises(ValueError):
        _explicit_seconds(value)


def test_explicit_duration_is_preserved_and_sdk_defaults_and_aliases_are_restored(monkeypatch):
    original = lambda *_args: 600
    module = ModuleType("chaos_agent.utils.fault_type")
    module.ensure_min_duration = original
    alias = ModuleType("chaos_agent.agent.plan_generator")
    alias.ensure_min_duration = original
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(sys.modules, alias.__name__, alias)
    with pytest.raises(RuntimeError, match="fixture"):
        with preserve_explicit_fault_duration():
            assert module.ensure_min_duration(30, "pod", "network", "delay") == 30
            assert alias.ensure_min_duration("30", "pod", "network", "delay") == 30
            assert module.ensure_min_duration(None, "pod", "network", "delay") == 600
            raise RuntimeError("fixture")
    assert module.ensure_min_duration is original
    assert alias.ensure_min_duration is original
