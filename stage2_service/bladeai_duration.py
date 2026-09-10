"""Preserve explicit Agent fault durations across the BladeAI SDK boundary."""
from contextlib import contextmanager
from importlib import import_module
import sys


def _explicit_seconds(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("fault duration must be an integer number of seconds")
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        seconds = int(value.strip())
    else:
        raise ValueError("fault duration must be an integer number of seconds")
    if seconds < 0:
        raise ValueError("fault duration must not be negative")
    return seconds or None


@contextmanager
def preserve_explicit_fault_duration():
    """Do not silently turn an approved short fault into the SDK's 600 seconds.

    Unspecified duration remains an SDK decision. Every eventual value still
    passes through Harness confirmation and the Controller execution contract.
    Patching is limited to this isolated worker and restored on every exit.
    """
    module = import_module("chaos_agent.utils.fault_type")
    original = module.ensure_min_duration
    if not callable(original):
        raise RuntimeError("BladeAI duration policy is unavailable")

    def preserve(value, scope, target, action):
        explicit = _explicit_seconds(value)
        return explicit if explicit is not None else original(value, scope, target, action)

    def replace_references(before, after):
        for name, loaded in tuple(sys.modules.items()):
            if name.startswith("chaos_agent.") and getattr(loaded, "ensure_min_duration", None) is before:
                setattr(loaded, "ensure_min_duration", after)

    replace_references(original, preserve)
    try:
        yield
    finally:
        replace_references(preserve, original)
