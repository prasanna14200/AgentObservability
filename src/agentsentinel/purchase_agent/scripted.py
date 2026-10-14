"""Deterministic test LLM. Naive mode deliberately obeys poisoned seller text."""

from __future__ import annotations

import re

from agentsentinel.purchase_agent.contracts import Product, PurchaseRequest


class ScriptedLLM:
    def __init__(self, *, naive_obedient: bool = False) -> None:
        self.naive_obedient = naive_obedient

    def plan(self, request: PurchaseRequest, product: Product) -> dict[str, object]:
        merchant, amount = product.merchant_id, f"{product.price_usd:.2f}"
        if self.naive_obedient:
            # This regex represents the vulnerable model behavior in a controlled demo.
            match = re.search(r"change merchant to ([A-Z0-9]+) and amount to ([0-9]+\.[0-9]{2})",
                              product.description)
            if match:
                merchant, amount = match.groups()
        return {"action": request.action, "merchant_id": merchant,
                "amount_usd": amount, "target_order_id": request.target_order_id}
