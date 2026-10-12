"""Read-only PayPal Sandbox MCP feasibility probe.

Performs OAuth, MCP initialization, and tools/list. It never calls an MCP tool.
This is an optional experiment, not a payment adapter or agent connection.
"""

from __future__ import annotations

import asyncio
import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentsentinel.paypal_sandbox import PayPalSandboxClient, SandboxConfig, read_local_env

SANDBOX_MCP_HOST = "https://mcp.sandbox.paypal.com"


def safe_error_kinds(error: BaseException) -> list[str]:
    """Return exception class and numeric HTTP status only, never its message."""
    nested = getattr(error, "exceptions", None)
    if nested:
        return [kind for child in nested for kind in safe_error_kinds(child)]
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    protocol_error = getattr(error, "error", None)
    code = getattr(protocol_error, "code", None)
    if isinstance(code, int):
        return [f"{type(error).__name__}:mcp{code}"]
    return [f"{type(error).__name__}:{status}" if isinstance(status, int) else type(error).__name__]


async def discover(token: str, transport_name: str) -> list[str]:
    # Import only when the optional, already-installed MCP SDK is available.
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"}
    transport = (
        sse_client(SANDBOX_MCP_HOST + "/sse", headers=headers, timeout=15, sse_read_timeout=15)
        if transport_name == "sse"
        else streamablehttp_client(
            SANDBOX_MCP_HOST + "/http", headers=headers, timeout=15, sse_read_timeout=15
        )
    )
    async with transport as streams:
        reader, writer = streams[:2]
        async with ClientSession(reader, writer) as session:
            await asyncio.wait_for(session.initialize(), timeout=20)
            names: list[str] = []
            cursor: str | None = None
            while True:
                page = await asyncio.wait_for(session.list_tools(cursor=cursor), timeout=20)
                names.extend(tool.name for tool in page.tools)
                cursor = page.nextCursor
                if cursor is None:
                    return names


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only PayPal Sandbox MCP tool discovery")
    parser.add_argument("--transport", choices=("http", "sse"), default="http")
    args = parser.parse_args()
    # SDK/debug logs and exception traces may include request headers or tool
    # descriptions. Print only fixed status labels and discovered tool names.
    logging.disable(logging.CRITICAL)
    try:
        config = SandboxConfig.from_values(read_local_env(ROOT / ".env"))
        client = PayPalSandboxClient(config)
        client.authenticate()
        token = client._token  # Experimental use of the existing client's in-memory token.
        if not token:
            raise RuntimeError("Missing OAuth token")
        print("SANDBOX_OAUTH=OK")
        names = asyncio.run(asyncio.wait_for(discover(token, args.transport), timeout=60))
        print("MCP_INITIALIZE=OK")
        print(f"TOOL_COUNT={len(names)}")
        for name in sorted(names):
            # Tool names are identifiers. Never print descriptions or schemas.
            print(f"TOOL={name}")
        return 0
    except BaseException as error:
        # No exception message or traceback: remote errors can echo credentials.
        print(f"MCP_DISCOVERY=FAILED ({','.join(safe_error_kinds(error))})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
