"""One planner step and one tool step. The graph cannot issue authorization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agentsentinel.purchase_agent.contracts import (
    LLMClient, PaymentToolClient, Product, Proposal, PurchaseRequest, ToolResult, parse_proposal,
)


class _State(TypedDict):
    request: PurchaseRequest
    product: Product
    proposal: Proposal | None
    result: ToolResult | None


@dataclass(frozen=True)
class AgentRun:
    proposal: Proposal
    result: ToolResult


class PurchaseAgent:
    """Holds only a model interface, catalog data, and a narrow proposal client."""

    def __init__(self, llm: LLMClient, tool_client: PaymentToolClient,
                 catalog: dict[str, Product]) -> None:
        self._llm = llm
        self._tool_client = tool_client
        self._catalog = dict(catalog)
        graph = StateGraph(_State)
        graph.add_node("planner", self._planner)
        graph.add_node("tool", self._tool)
        graph.add_edge(START, "planner")
        graph.add_edge("planner", "tool")
        graph.add_edge("tool", END)
        self._graph = graph.compile()

    def _planner(self, state: _State) -> dict[str, Proposal]:
        raw = self._llm.plan(state["request"], state["product"])
        return {"proposal": parse_proposal(raw, state["product"])}

    def _tool(self, state: _State) -> dict[str, ToolResult]:
        proposal = state["proposal"]
        if proposal is None:
            raise ValueError("planner did not propose an action")
        if proposal.action == "create_order":
            return {"result": self._tool_client.propose_create_order(proposal)}
        return {"result": self._tool_client.propose_capture_order(proposal)}

    def run(self, request: PurchaseRequest) -> AgentRun:
        product = self._catalog[request.product_id]
        state = self._graph.invoke({"request": request, "product": product,
                                    "proposal": None, "result": None})
        proposal, result = state["proposal"], state["result"]
        if proposal is None or result is None:
            raise RuntimeError("purchase graph stopped without a sanitized result")
        return AgentRun(proposal, result)
