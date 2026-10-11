"""Render canonical prompt texts for the bound replica and check prompt hygiene.

The authoritative L0--L3 prompts name the system under test literally
(``otel-demo``).  A replica Controller substitutes its own namespace for that
token; the default binding returns every text unchanged, byte for byte.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping

from .target_binding import DEFAULT_APPLICATION_NAMESPACE, current


PROMPT_HYGIENE_ENV = "RESBENCH_PROMPT_HYGIENE"
_DISABLED_VALUES = frozenset({"0", "false", "off", "no", "disabled"})
_BOUNDARY_BEFORE = r"(?<![A-Za-z0-9-])"
_BOUNDARY_AFTER = r"(?![A-Za-z0-9-])"


class PromptHygieneError(ValueError):
    """The prompt names a replica other than the one this Controller is bound to."""

    def __init__(self, tokens: list[str], own_namespace: str):
        self.tokens = list(tokens)
        self.own_namespace = own_namespace
        super().__init__(
            "prompt names another replica's namespace: "
            + ", ".join(self.tokens)
            + f" (this Controller is bound to {own_namespace})"
        )


def _token_pattern(token: str) -> re.Pattern[str]:
    return re.compile(_BOUNDARY_BEFORE + re.escape(token) + _BOUNDARY_AFTER)


def render_prompt(text: str, namespace: str | None = None) -> str:
    """Substitute the bound namespace for the literal default token.

    Only whole ``otel-demo`` tokens are replaced; ``otel-demo-01`` is already a
    namespace and is left alone.  With the default binding the text is
    returned unchanged.
    """
    target = namespace if namespace is not None else current().application_namespace
    if target == DEFAULT_APPLICATION_NAMESPACE:
        return text
    return _token_pattern(DEFAULT_APPLICATION_NAMESPACE).sub(target, text)


def prompt_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def namespace_tokens(text: str, *, prefix: str = DEFAULT_APPLICATION_NAMESPACE) -> list[str]:
    """Every ``<prefix>`` or ``<prefix>-NN`` token in ``text``, in order, deduplicated."""
    pattern = re.compile(_BOUNDARY_BEFORE + re.escape(prefix) + r"(?:-[0-9]+)?" + _BOUNDARY_AFTER)
    found: list[str] = []
    for match in pattern.finditer(text):
        token = match.group(0)
        if token not in found:
            found.append(token)
    return found


def foreign_namespace_tokens(
    text: str,
    *,
    own_namespace: str | None = None,
    prefix: str = DEFAULT_APPLICATION_NAMESPACE,
) -> list[str]:
    own = own_namespace if own_namespace is not None else current().application_namespace
    return [token for token in namespace_tokens(text, prefix=prefix) if token != own]


def hygiene_enabled(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return (values.get(PROMPT_HYGIENE_ENV) or "").strip().lower() not in _DISABLED_VALUES


def assert_prompt_hygiene(text: str, *, own_namespace: str | None = None) -> None:
    """Refuse a prompt that names a different replica (HTTP 422 at the API).

    Can be switched off with ``RESBENCH_PROMPT_HYGIENE=off``.
    """
    if not hygiene_enabled():
        return
    own = own_namespace if own_namespace is not None else current().application_namespace
    tokens = foreign_namespace_tokens(text, own_namespace=own)
    if tokens:
        raise PromptHygieneError(tokens, own)


def prompt_source(text: str, canonical_texts: Mapping[str, str] | None) -> str:
    """``canonical`` when ``text`` equals a rendered authoritative prompt, else ``manual``."""
    if canonical_texts and text in set(canonical_texts.values()):
        return "canonical"
    return "manual"
