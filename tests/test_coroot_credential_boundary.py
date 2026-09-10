from harness.agent_exec.environment import AGENT_ENV_ALLOWLIST
from harness.d0.common import redact_sensitive_text
from scripts.run_harness_trial import ALLOWED_RUNTIME_ENV, redact_json, redact_text
from stage2_service.channel_qualification import _COROOT_ENV_KEYS


def test_coroot_session_is_controller_only_and_redacted_from_artifacts():
    key = "RESBENCH_COROOT_SESSION_COOKIE"
    secret = "example-private-viewer-session"
    assert key in _COROOT_ENV_KEYS
    assert "RESBENCH_COROOT_BEARER_TOKEN" not in _COROOT_ENV_KEYS
    assert key not in AGENT_ENV_ALLOWLIST
    assert key not in ALLOWED_RUNTIME_ENV
    assert secret not in redact_json({"echo": secret}, {key: secret})["echo"]
    assert secret not in redact_text(f"Cookie: coroot_session={secret}; path=/", {})
    assert secret not in redact_sensitive_text(f"Cookie: coroot_session={secret}; path=/")
    assert secret not in redact_sensitive_text('{"session_cookie":"' + secret + '"}')
