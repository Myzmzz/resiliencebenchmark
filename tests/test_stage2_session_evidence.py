"""Session feedback is recorded when emitted, not reconstructed after a turn."""

from pathlib import Path

from stage2_service.session import HarnessSession


def test_record_observer_runs_even_without_a_transcript_file() -> None:
    """An observer must see each event immediately, including delivery facts."""
    received = []
    session = HarnessSession(
        argv=["unused"], stdin=b"", env={}, timeout_seconds=1,
        stdout_line_observer=lambda line: [], record_observer=received.append,
    )
    session._record("FEEDBACK_QUEUED", {"category": "FACT_EVENT", "message": "queued"})
    assert [row["event"] for row in received] == ["FEEDBACK_QUEUED"]
    session._record("FEEDBACK_DELIVERED", {"category": "FACT_EVENT", "message": "delivered"})
    assert [row["event"] for row in received] == ["FEEDBACK_QUEUED", "FEEDBACK_DELIVERED"]
    assert received[0]["ts"] <= received[1]["ts"]


def test_record_observer_receives_the_same_redacted_record_as_archive(tmp_path: Path) -> None:
    """No private pre-redaction payload may leak through the live observer."""
    import json

    received = []
    path = tmp_path / "session.jsonl"
    session = HarnessSession(
        argv=["unused"], stdin=b"", env={}, timeout_seconds=1,
        stdout_line_observer=lambda line: [], record_observer=received.append,
        transcript_path=path, redactor=lambda payload: {**payload, "message": "redacted"},
    )
    session._record("FEEDBACK_DELIVERED", {"message": "private-message"})
    assert received == [json.loads(path.read_text())]
    assert received[0]["payload"]["message"] == "redacted"
    assert path.stat().st_mode & 0o777 == 0o600
