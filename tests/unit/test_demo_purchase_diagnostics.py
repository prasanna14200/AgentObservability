"""Diagnostic output never echoes provider or transport exception details."""

import importlib.util
import sys
from pathlib import Path

import pytest

from agentsentinel.paypal_sandbox import SandboxError, UncertainOutcome

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "demo_purchase.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("demo_purchase", SCRIPT)
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


@pytest.mark.parametrize(("error", "expected"), [
    (SandboxError("Sandbox GET request failed (HTTP 404)"), "GET_HTTP_404"),
    (SandboxError("Sandbox OAuth failed (HTTP 401)"), "OAUTH_HTTP_401"),
    (UncertainOutcome("private transport detail"), "NETWORK_OUTCOME_UNKNOWN"),
    (SandboxError("Bearer private-token"), "SANDBOX_OPERATION_UNAVAILABLE"),
    (ValueError("approval URL private-token"), "INVALID_INPUT_OR_RESPONSE"),
])
def test_safe_failure_codes_are_bounded(error, expected):
    assert demo._safe_failure_code(error) == expected
    assert "private" not in demo._safe_failure_code(error)
