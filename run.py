"""Start the localhost approval and dashboard service."""

from __future__ import annotations

import getpass
import hmac
import os
import socket
import sys
from pathlib import Path
from typing import Callable, Mapping

_ROOT = Path(__file__).resolve().parent
_PAYPAL_ENVIRONMENT_NAMES = (
    "PAYPAL_ENVIRONMENT",
    "PAYPAL_CLIENT_ID",
    "PAYPAL_CLIENT_SECRET",
    "PAYPAL_MERCHANT_ID",
    "PAYPAL_ACCESS_TOKEN",
)


def resolve_approver_secret(
    environment: Mapping[str, str],
    *,
    interactive: bool,
    prompt: Callable[[str], str],
) -> str:
    secret = environment.get("APPROVER_SECRET")
    if secret is None:
        if not interactive:
            raise RuntimeError("configure APPROVER_SECRET or start from an interactive terminal")
        secret = prompt("Set APPROVER_SECRET (input hidden): ")
        confirmation = prompt("Confirm APPROVER_SECRET (input hidden): ")
        if not hmac.compare_digest(secret, confirmation):
            raise RuntimeError("approver credentials do not match")
    if len(secret) < 32:
        raise RuntimeError("APPROVER_SECRET must be at least 32 characters")
    return secret


def build_server_environment(
    environment: Mapping[str, str],
    approver_secret: str,
) -> dict[str, str]:
    child_environment = dict(environment)
    child_environment["APPROVER_SECRET"] = approver_secret
    existing_pythonpath = child_environment.get("PYTHONPATH")
    child_environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(_ROOT / "src"), existing_pythonpath) if part
    )
    for name in _PAYPAL_ENVIRONMENT_NAMES:
        child_environment.pop(name, None)
    return child_environment


def main() -> None:
    os.chdir(_ROOT)
    try:
        secret = resolve_approver_secret(
            os.environ,
            interactive=sys.stdin.isatty(),
            prompt=getpass.getpass,
        )
    except RuntimeError as error:
        raise SystemExit(str(error)) from None

    with socket.socket() as check:
        try:
            check.bind(("127.0.0.1", 8000))
        except OSError:
            raise SystemExit("Port 8000 is occupied; no server was started.") from None

    environment = build_server_environment(os.environ, secret)
    print("Starting the local dashboard at http://127.0.0.1:8000")
    sys.stdout.flush()
    os.execvpe(
        sys.executable,
        [sys.executable, "-m", "agentsentinel.approval_service"],
        environment,
    )


if __name__ == "__main__":
    main()
