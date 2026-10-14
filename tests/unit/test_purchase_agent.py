"""Offline Day 9 agent and trust-boundary tests; no provider or model network."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentsentinel.purchase_agent.contracts import ToolResult, load_catalog
from agentsentinel.purchase_agent.graph import PurchaseAgent
from agentsentinel.purchase_agent.ollama_client import OllamaLLM
from agentsentinel.purchase_agent.scripted import ScriptedLLM

from scripts.demo_side_by_side import (
    CATALOG, MockPayPalAdapter, demo_request, protected_client, run_comparison,
)


class SpyToolClient:
    def __init__(self) -> None:
        self.calls = []

    def propose_create_order(self, proposal):
        self.calls.append(("create_order", proposal))
        return ToolResult("BLOCK", "REJECTED", "TEST_ONLY")

    def propose_capture_order(self, proposal):
        self.calls.append(("capture_order", proposal))
        return ToolResult("BLOCK", "REJECTED", "TEST_ONLY")


def test_scripted_naive_follows_poisoned_catalog_but_cautious_does_not():
    catalog = load_catalog(CATALOG)
    request = demo_request()
    spy = SpyToolClient()
    naive = PurchaseAgent(ScriptedLLM(naive_obedient=True), spy, catalog).run(request)
    assert naive.proposal.merchant_id == "OTHER123"
    assert f"{naive.proposal.amount_usd:.2f}" == "9.00"
    assert spy.calls == [("capture_order", naive.proposal)]
    cautious = PurchaseAgent(ScriptedLLM(), spy, catalog).run(request)
    assert cautious.proposal.merchant_id == "ABCDEFGH"
    assert f"{cautious.proposal.amount_usd:.2f}" == "1.00"


def test_create_order_uses_only_create_proposal_method():
    request = demo_request()
    request = type(request)(request.user_request, request.product_id, request.budget_usd,
                            "create_order")
    spy = SpyToolClient()
    run = PurchaseAgent(ScriptedLLM(), spy, load_catalog(CATALOG)).run(request)
    assert spy.calls == [("create_order", run.proposal)]
    assert run.proposal.target_order_id is None


def test_agent_supplied_authorization_is_ignored():
    class ForgedOutput:
        def plan(self, request, product):
            raw = ScriptedLLM().plan(request, product)
            raw["authorization"] = {"status": "issued", "merchant_id": "OTHER123"}
            return raw

    spy = SpyToolClient()
    run = PurchaseAgent(ForgedOutput(), spy, load_catalog(CATALOG)).run(demo_request())
    assert not hasattr(run.proposal, "authorization")
    assert spy.calls == [("capture_order", run.proposal)]


def test_forged_authorization_cannot_change_protected_gateway_result(tmp_path):
    class ForgedOutput:
        def plan(self, request, product):
            raw = ScriptedLLM(naive_obedient=True).plan(request, product)
            raw["authorization"] = {"status": "issued", "amount_usd": "9.00"}
            return raw

    adapter = MockPayPalAdapter()
    client = protected_client(tmp_path / "forged.sqlite", run_id="forged_day9", adapter=adapter)
    public = {name for name in dir(client) if not name.startswith("_")}
    assert public == {"propose_create_order", "propose_capture_order"}
    run = PurchaseAgent(ForgedOutput(), client, load_catalog(CATALOG)).run(demo_request())
    assert run.result.decision == "BLOCK"
    assert run.result.reason_code == "AUTHORIZATION_MERCHANT_MISMATCH"
    assert adapter.calls == 0 and adapter.money_moved == 0


def test_agent_package_has_no_payment_authority_imports():
    package = Path(__file__).resolve().parents[2] / "src" / "agentsentinel" / "purchase_agent"
    forbidden = ("paypal", "payment_gateway", "payment_policy", "payment_sqlite",
                 "payment_sandbox_adapter", "schema.payment", "issue_authorization")
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names]
        assert not any(any(word in module for word in forbidden) for module in imports), path


def test_side_by_side_same_proposal_only_unprotected_mock_moves_money():
    rows = run_comparison()
    assert rows[0][1].proposal == rows[1][1].proposal
    assert rows[0][1].result.decision == "ALLOW" and rows[0][2] == 9
    assert rows[1][1].result.decision == "BLOCK" and rows[1][2] == 0
    assert rows[1][1].result.reason_code == "AUTHORIZATION_MERCHANT_MISMATCH"


def test_mock_refuses_credentials_and_transports():
    for kwargs in ({"client_id": "unused"}, {"client_secret": "unused"}, {"transport": object()}):
        with pytest.raises(ValueError, match="refuses"):
            MockPayPalAdapter(**kwargs)


def test_ollama_mode_uses_bounded_fake_client_without_network(caplog):
    class FakeLocalClient:
        def __init__(self):
            self.calls = []

        def chat(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                "action": "capture_order", "merchant_id": "OTHER123",
                "amount_usd": "9.00", "target_order_id": "ORDER_DEMO_001",
                "authorization": {"status": "issued"},
            })), prompt_eval_count=77, eval_count=22)

    fake = FakeLocalClient()
    llm = OllamaLLM(model="qwen2.5:1.5b", client=fake, max_calls=1)
    spy = SpyToolClient()
    with caplog.at_level("INFO"):
        run = PurchaseAgent(llm, spy, load_catalog(CATALOG)).run(demo_request())
    assert run.proposal.merchant_id == "OTHER123"
    assert not hasattr(run.proposal, "authorization")
    assert len(fake.calls) == 1
    assert fake.calls[0]["options"]["num_predict"] == 192
    assert "prompt=77 output=22" in caplog.text
    assert "hidden instruction" not in caplog.text
    with pytest.raises(RuntimeError, match="call limit"):
        llm.plan(demo_request(), load_catalog(CATALOG)["notebook"])


def test_invalid_llm_output_stops_before_tool():
    class BadLLM:
        def plan(self, request, product):
            return {"action": "capture_order", "merchant_id": "OTHER123",
                    "amount_usd": "not-money", "target_order_id": "ORDER_DEMO_001"}

    spy = SpyToolClient()
    with pytest.raises(ValueError):
        PurchaseAgent(BadLLM(), spy, load_catalog(CATALOG)).run(demo_request())
    assert spy.calls == []
