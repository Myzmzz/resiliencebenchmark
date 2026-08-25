"""ResourceEnvelope 三态封装：ok / not_ready / parse_error。"""

from backend.models.common import ResourceEnvelope


def test_ok_envelope_carries_data_and_sources() -> None:
    envelope = ResourceEnvelope.ok({"answer": 42}, source_files=["benchmarkfactory.yaml"])
    assert envelope.status == "ok"
    assert envelope.data == {"answer": 42}
    assert envelope.source_files == ["benchmarkfactory.yaml"]
    assert envelope.error is None
    assert envelope.parsed_at is not None


def test_not_ready_envelope_has_no_data() -> None:
    envelope = ResourceEnvelope.not_ready(source_files=["artifacts/runs"])
    assert envelope.status == "not_ready"
    assert envelope.data is None


def test_parse_error_envelope_carries_message() -> None:
    envelope = ResourceEnvelope.parse_error("bad yaml at line 3", source_files=["x.yaml"])
    assert envelope.status == "parse_error"
    assert envelope.error == "bad yaml at line 3"
    assert envelope.data is None
