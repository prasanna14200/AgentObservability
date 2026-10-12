"""The MCP experiment may initialize and list tools, never invoke them."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "paypal_mcp_discover.py"
spec = importlib.util.spec_from_file_location("paypal_mcp_discover", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class FakeTransport:
    async def __aenter__(self):
        return object(), object()

    async def __aexit__(self, *args):
        return None


class FakeSession:
    def __init__(self, *_):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def initialize(self):
        self.calls.append("initialize")

    async def list_tools(self, cursor=None):
        self.calls.append("list_tools")
        return SimpleNamespace(tools=[SimpleNamespace(name="get_order")], nextCursor=None)


def test_discovery_only_initializes_and_lists(monkeypatch):
    session = FakeSession()
    fake_mcp = ModuleType("mcp")
    fake_mcp.ClientSession = lambda *args: session
    fake_client = ModuleType("mcp.client")
    fake_sse = ModuleType("mcp.client.sse")
    fake_sse.sse_client = lambda *args, **kwargs: FakeTransport()
    fake_http = ModuleType("mcp.client.streamable_http")
    fake_http.streamablehttp_client = lambda *args, **kwargs: FakeTransport()
    for module in (fake_mcp, fake_client, fake_sse, fake_http):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    names = asyncio.run(probe.discover("fake-token", "sse"))
    assert names == ["get_order"]
    assert session.calls == ["initialize", "list_tools"]


def test_error_diagnostics_do_not_include_exception_message():
    error = RuntimeError("private-token-should-not-appear")
    assert probe.safe_error_kinds(error) == ["RuntimeError"]
