"""Harness-specific adapters for canonical Stage-2 events."""

from __future__ import annotations

from stage2_service.contracts import HarnessKind

from .base import (
    AgentMessage,
    CanonicalEvent,
    Checkpoint,
    HarnessAdapterError,
    HarnessAdapter,
    HarnessCapability,
    Question,
    ToolCall,
    ToolResult,
)
from .bladeai import BladeAIHarnessAdapter
from .claude_code import ClaudeCodeHarnessAdapter
from .codex import CodexHarnessAdapter
from .deepseek import DeepSeekHarnessAdapter, DeepSeekTraceError, iter_zstd_jsonl_lines


def create_adapter(harness: HarnessKind) -> HarnessAdapter:
    if harness is HarnessKind.CODEX:
        return CodexHarnessAdapter()
    if harness is HarnessKind.CLAUDE_CODE:
        return ClaudeCodeHarnessAdapter()
    if harness is HarnessKind.DEEPSEEK:
        return DeepSeekHarnessAdapter()
    if harness is HarnessKind.BLADEAI:
        return BladeAIHarnessAdapter()
    raise ValueError(f"unsupported harness: {harness}")


__all__ = [
    "AgentMessage",
    "BladeAIHarnessAdapter",
    "CanonicalEvent",
    "Checkpoint",
    "ClaudeCodeHarnessAdapter",
    "CodexHarnessAdapter",
    "DeepSeekHarnessAdapter",
    "DeepSeekTraceError",
    "HarnessAdapter",
    "HarnessAdapterError",
    "HarnessCapability",
    "Question",
    "ToolCall",
    "ToolResult",
    "create_adapter",
    "iter_zstd_jsonl_lines",
]
