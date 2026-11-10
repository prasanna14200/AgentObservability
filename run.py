Set-Location D:\AgentObservability

@'
import os
import secrets
import socket
import sys

with socket.socket() as check:
    try:
        check.bind(("127.0.0.1", 8000))
    except OSError:
        raise SystemExit("Port 8000 is occupied. No new server or credential was created.")

secret = secrets.token_urlsafe(64)
env = os.environ.copy()
env["PYTHONPATH"] = "src"
env["APPROVER_SECRET"] = secret

for name in (
    "PAYPAL_ENVIRONMENT",
    "PAYPAL_CLIENT_ID",
    "PAYPAL_CLIENT_SECRET",
    "PAYPAL_MERCHANT_ID",
    "PAYPAL_ACCESS_TOKEN",
):
    env.pop(name, None)

print("Temporary approver credential (copy it from this terminal):")
print(secret)
print("Starting local dashboard at http://127.0.0.1:8000")
sys.stdout.flush()

os.execvpe(
    sys.executable,
    [sys.executable, "-m", "agentsentinel.approval_service"],
    env,
)
'@ | .\.venv\Scripts\python.exe -