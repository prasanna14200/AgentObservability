from __future__ import annotations

from pathlib import Path

import pytest

from run import build_server_environment, resolve_approver_secret


def test_approver_secret_uses_configured_value_without_prompting():
    secret = "a" * 40

    assert resolve_approver_secret(
        {"APPROVER_SECRET": secret},
        interactive=False,
        prompt=lambda _: pytest.fail("configured secret must not prompt"),
    ) == secret


def test_approver_secret_prompts_hidden_input_and_confirms_match():
    responses = iter(("b" * 40, "b" * 40))

    secret = resolve_approver_secret(
        {},
        interactive=True,
        prompt=lambda _: next(responses),
    )

    assert secret == "b" * 40


@pytest.mark.parametrize(
    ("environment", "interactive", "responses", "message"),
    [
        ({}, False, (), "interactive terminal"),
        ({}, True, ("c" * 40, "d" * 40), "do not match"),
        ({"APPROVER_SECRET": "short"}, False, (), "at least 32"),
    ],
)
def test_approver_secret_rejects_unavailable_or_invalid_credentials(
    environment, interactive, responses, message
):
    prompt_values = iter(responses)
    with pytest.raises(RuntimeError, match=message):
        resolve_approver_secret(
            environment,
            interactive=interactive,
            prompt=lambda _: next(prompt_values),
        )


def test_server_environment_excludes_paypal_credentials_and_sets_local_source():
    environment = {
        "PAYPAL_ENVIRONMENT": "sandbox",
        "PAYPAL_CLIENT_ID": "sandbox-client-id",
        "PAYPAL_CLIENT_SECRET": "TEST_FAKE_SECRET",
        "PAYPAL_MERCHANT_ID": "merchant-id",
        "PAYPAL_ACCESS_TOKEN": "access-token",
        "OTHER_SETTING": "preserved",
        "PYTHONPATH": "existing-path",
    }

    child = build_server_environment(environment, "new-secret-value" * 3)

    assert "APPROVER_SECRET" in child
    assert len(child["APPROVER_SECRET"]) == len("new-secret-value" * 3)
    assert child["OTHER_SETTING"] == "preserved"
    assert child["PYTHONPATH"].split(";")[0] == str(
        Path(__file__).resolve().parents[2] / "src"
    )
    assert not any(name.startswith("PAYPAL_") for name in child)
