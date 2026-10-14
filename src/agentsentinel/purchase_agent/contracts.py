"""Small agent-facing contracts. No provider response or authorization type enters here."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal, Mapping, Protocol


Action = Literal["create_order", "capture_order"]
Decision = Literal["ALLOW", "REVIEW", "BLOCK", "NO_DECISION"]
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_:-]{2,79}\Z")


def _amount(value: object) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]{2}", value):
        raise ValueError("amount must be USD cents")
    amount = Decimal(value)
    if not Decimal("0.01") <= amount <= Decimal("10.00"):
        raise ValueError("amount outside demo bounds")
    return amount


@dataclass(frozen=True)
class Product:
    product_id: str
    merchant_id: str
    price_usd: Decimal
    description: str  # Untrusted catalog data, including simulated injected text.


def load_catalog(path: Path) -> dict[str, Product]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not 1 <= len(raw) <= 20:
        raise ValueError("invalid catalog")
    products: dict[str, Product] = {}
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"product_id", "merchant_id", "price_usd", "description"}:
            raise ValueError("invalid catalog entry")
        product_id, merchant = item["product_id"], item["merchant_id"]
        description = item["description"]
        if (not isinstance(product_id, str) or not _IDENTIFIER.fullmatch(product_id)
                or not isinstance(merchant, str) or not _IDENTIFIER.fullmatch(merchant)
                or not isinstance(description, str) or len(description) > 500):
            raise ValueError("invalid catalog fields")
        if product_id in products:
            raise ValueError("duplicate product")
        products[product_id] = Product(product_id, merchant, _amount(item["price_usd"]), description)
    return products


@dataclass(frozen=True)
class PurchaseRequest:
    user_request: str
    product_id: str
    budget_usd: Decimal
    action: Action
    target_order_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.user_request, str) or not 1 <= len(self.user_request) <= 500:
            raise ValueError("invalid user request")
        if not _IDENTIFIER.fullmatch(self.product_id):
            raise ValueError("invalid product ID")
        if (not isinstance(self.budget_usd, Decimal) or not self.budget_usd.is_finite()
                or self.budget_usd <= 0 or self.budget_usd > Decimal("10.00")):
            raise ValueError("invalid budget")
        if self.action not in {"create_order", "capture_order"}:
            raise ValueError("invalid action")
        if (self.action == "capture_order") != (self.target_order_id is not None):
            raise ValueError("capture requires an existing order ID")
        if self.target_order_id is not None and not _IDENTIFIER.fullmatch(self.target_order_id):
            raise ValueError("invalid order ID")


@dataclass(frozen=True)
class Proposal:
    product_id: str
    merchant_id: str
    amount_usd: Decimal
    action: Action
    target_order_id: str | None


def parse_proposal(raw: Mapping[str, object], product: Product) -> Proposal:
    """Select payment fields only; an agent-supplied authorization is ignored."""
    if not isinstance(raw, Mapping):
        raise ValueError("invalid model proposal")
    action = raw.get("action")
    merchant = raw.get("merchant_id")
    target = raw.get("target_order_id")
    if action not in {"create_order", "capture_order"}:
        raise ValueError("invalid proposed action")
    if not isinstance(merchant, str) or not _IDENTIFIER.fullmatch(merchant):
        raise ValueError("invalid proposed merchant")
    if (action == "capture_order") != (target is not None):
        raise ValueError("invalid proposed target")
    if target is not None and (not isinstance(target, str) or not _IDENTIFIER.fullmatch(target)):
        raise ValueError("invalid proposed target")
    return Proposal(product.product_id, merchant, _amount(raw.get("amount_usd")), action, target)


@dataclass(frozen=True)
class ToolResult:
    decision: Decision
    status: str
    reason_code: str


class LLMClient(Protocol):
    def plan(self, request: PurchaseRequest, product: Product) -> Mapping[str, object]: ...


class PaymentToolClient(Protocol):
    def propose_create_order(self, proposal: Proposal) -> ToolResult: ...

    def propose_capture_order(self, proposal: Proposal) -> ToolResult: ...
