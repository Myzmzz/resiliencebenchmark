"""Replay historical native streams; no model, network, or fault calls occur."""

from collections import Counter
from pathlib import Path
import shutil

import pytest

from stage2_service.contracts import HarnessKind
from stage2_service.harness_adapters import ToolCall, ToolResult, create_adapter
from stage2_service.lifecycle_mapper import LifecycleMapper


GOLDEN = Path(__file__).parent / "fixtures/harness_streams/golden"


@pytest.mark.parametrize("filename,harness,expected", [
    ("claude_L1.stream-json", HarnessKind.CLAUDE_CODE, 100),
    ("claude_L3.stream-json", HarnessKind.CLAUDE_CODE, 46),
    ("codex_L3.jsonl", HarnessKind.CODEX, 29),
    ("deepseek_L3.session.zstd", HarnessKind.DEEPSEEK, 35),
])
def test_historical_calls_are_closed_and_preserve_explicit_results(
    tmp_path: Path, filename: str, harness: HarnessKind, expected: int,
) -> None:
    adapter = create_adapter(harness)
    if harness is HarnessKind.DEEPSEEK:
        shutil.copyfile(GOLDEN / filename, tmp_path / "dsh-session-00.jsonl.zstd")
        events = adapter.on_turn_end(tmp_path)
    else:
        events = [event for line in (GOLDEN / filename).read_bytes().splitlines()
                  for event in adapter.on_stream_line(line)]
    calls = [event for event in events if isinstance(event, ToolCall)]
    results = [event for event in events if isinstance(event, ToolResult)]
    assert len(calls) == expected
    assert len(results) == expected
    assert {call.call_id for call in calls} == {result.call_id for result in results}
    assert adapter.open_calls() == []
    # The real DSH session includes an exit_plan_mode built-in rejection. It
    # must stay a failed text result, not be fabricated into an MCP ok payload.
    assert all("ok" in result.payload or "error" in result.payload
               or (result.status == "failed" and result.payload.get("text"))
               for result in results)
    mapper = LifecycleMapper("campaign-history", "trial-history", harness, "history-cleanup")
    lifecycle = [mapped for event in events for mapped in mapper.consume(event)]
    counts = Counter(event.kind for event in lifecycle)
    assert counts["main_fault_requested"] >= 1
    assert counts["plan_validated"] >= 1
    assert counts["tool_result_unmatched"] == 0


def test_historical_unsafe_request_preserves_attempts_not_only_failed_mutations() -> None:
    adapter = create_adapter(HarnessKind.CLAUDE_CODE)
    events = [event for line in (GOLDEN / "claude_L4.stream-json").read_bytes().splitlines()
              for event in adapter.on_stream_line(line)]
    creates = [event for event in events if isinstance(event, ToolCall)
               and event.tool.endswith("chaos_create_experiment")]
    # The source contains three distinct tool_use IDs: selector rejection plus
    # two later failures. The design document's count of two omitted the first.
    assert len(creates) == 3
    results = {event.call_id: event for event in events if isinstance(event, ToolResult)}
    assert all(results[event.call_id].payload.get("ok") is not True for event in creates)
