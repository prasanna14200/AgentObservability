"""Exercise the manual command journal with a fake client and temporary files."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from agentsentinel.paypal_sandbox import UncertainOutcome

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "paypal_sandbox_spike.py"
spec = importlib.util.spec_from_file_location("paypal_sandbox_spike", SCRIPT)
spike = importlib.util.module_from_spec(spec)
spec.loader.exec_module(spike)


class FakeClient:
    def __init__(self, config):
        self.posts = []
        self.status = "APPROVED"

    def create_order(self, operation_id, amount):
        self.posts.append("create")
        return {"order_id": "ORDER123", "status": "CREATED"}

    def get_order(self, order_id):
        return {"order_id": order_id, "status": self.status, "amount": "1.00", "currency": "USD",
                "merchant_id": "MERCHANT12345", "capture_id": None, "capture_status": None}

    def capture_order(self, order_id, operation_id, amount):
        self.posts.append("capture")
        raise UncertainOutcome("Network outcome uncertain")


def test_journal_prevents_duplicate_capture_after_timeout(tmp_path, monkeypatch, capsys):
    fake = FakeClient(None)
    monkeypatch.setattr(spike, "JOURNAL", tmp_path / "operations.json")
    monkeypatch.setattr(spike, "read_local_env", lambda path: {
        "PAYPAL_ENVIRONMENT": "sandbox", "PAYPAL_CLIENT_ID": "fake-id",
        "PAYPAL_CLIENT_SECRET": "fake-secret", "PAYPAL_MERCHANT_ID": "MERCHANT12345"})
    monkeypatch.setattr(spike, "PayPalSandboxClient", lambda config: fake)
    assert spike.main(["create", "--operation-id", "purchase-001", "--amount", "1.00"]) == 0
    assert spike.main(["create", "--operation-id", "purchase-001", "--amount", "1.00"]) == 0
    assert fake.posts == ["create"]
    fake.status = "CREATED"
    assert spike.main(["capture", "--operation-id", "purchase-001",
                       "--capture-operation-id", "capture-001"]) == 2
    assert fake.posts == ["create"]
    fake.status = "APPROVED"
    assert spike.main(["capture", "--operation-id", "purchase-001",
                       "--capture-operation-id", "capture-001"]) == 2
    assert fake.posts == ["create", "capture"]
    assert spike.main(["capture", "--operation-id", "purchase-001",
                       "--capture-operation-id", "capture-001"]) == 2
    assert fake.posts == ["create", "capture"]
    saved = json.loads(spike.JOURNAL.read_text(encoding="utf-8"))
    assert saved["capture"]["capture-001"]["state"] == "PENDING"
    assert "fake-secret" not in str(saved)
    assert "private-token" not in capsys.readouterr().out
