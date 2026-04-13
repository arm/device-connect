"""Claude Agent SDK adapter — exposes Device Connect tools to claude-agent-sdk.

Hierarchical discovery keeps LLM context small::

    import anyio
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, TextBlock
    from device_connect_agent_tools import connect
    from device_connect_agent_tools.adapters.claude import create_device_connect_server

    async def main():
        connect()
        options = ClaudeAgentOptions(
            model="claude-sonnet-4-6",
            mcp_servers={"device_connect": create_device_connect_server()},
        )
        async with ClaudeSDKClient(options=options) as client:
            await client.query("What devices are online?")
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            print(block.text)

    anyio.run(main)

Unlike the Strands and LangChain adapters, ``claude_agent_sdk.tool`` does not
introspect the wrapped function — name, description, and schema are passed
explicitly per tool, which is why this module is longer than its siblings.

Requires: pip install device-connect-agent-tools[claude]
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import tool, create_sdk_mcp_server

from device_connect_agent_tools.tools import (
    describe_fleet as _describe_fleet,
    list_devices as _list_devices,
    get_device_functions as _get_device_functions,
    discover_devices as _discover_devices,
    invoke_device as _invoke_device,
    invoke_device_with_fallback as _invoke_device_with_fallback,
    get_device_status as _get_device_status,
)


def _text(result: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}


# Hierarchical discovery tools (recommended)


@tool(
    "describe_fleet",
    "Get a high-level summary of all available devices, grouped by type and "
    "location. Use this first to understand what is available, then call "
    "list_devices to browse specific types or locations.",
    {},
)
async def describe_fleet(args: dict[str, Any]) -> dict[str, Any]:
    return _text(_describe_fleet())


@tool(
    "list_devices",
    "Browse available devices with filtering and pagination. Returns compact "
    "device summaries (no full schemas). Use get_device_functions for details.",
    {
        "device_type": str,
        "location": str,
        "status": str,
        "group_by": str,
        "offset": int,
        "limit": int,
    },
)
async def list_devices(args: dict[str, Any]) -> dict[str, Any]:
    return _text(
        _list_devices(
            device_type=args.get("device_type"),
            location=args.get("location"),
            status=args.get("status"),
            group_by=args.get("group_by"),
            offset=int(args.get("offset", 0)),
            limit=int(args.get("limit", 20)),
        )
    )


@tool(
    "get_device_functions",
    "Get full function schemas for a specific device. Call this after "
    "list_devices to see what a device can do and what parameters each "
    "function accepts.",
    {"device_id": str},
)
async def get_device_functions(args: dict[str, Any]) -> dict[str, Any]:
    return _text(_get_device_functions(device_id=args["device_id"]))


# Invocation tools


@tool(
    "invoke_device",
    "Call a function on a Device Connect device. Use get_device_functions "
    "first to learn available functions and parameters.",
    {"device_id": str, "function": str, "params": dict, "llm_reasoning": str},
)
async def invoke_device(args: dict[str, Any]) -> dict[str, Any]:
    return _text(
        _invoke_device(
            device_id=args["device_id"],
            function=args["function"],
            params=args.get("params"),
            llm_reasoning=args.get("llm_reasoning"),
        )
    )


@tool(
    "invoke_device_with_fallback",
    "Call a function with automatic fallback across a list of device IDs. "
    "Tries each device in order until one succeeds.",
    {"device_ids": list, "function": str, "params": dict, "llm_reasoning": str},
)
async def invoke_device_with_fallback(args: dict[str, Any]) -> dict[str, Any]:
    return _text(
        _invoke_device_with_fallback(
            device_ids=args["device_ids"],
            function=args["function"],
            params=args.get("params"),
            llm_reasoning=args.get("llm_reasoning"),
        )
    )


@tool(
    "get_device_status",
    "Get detailed status of a specific Device Connect device.",
    {"device_id": str},
)
async def get_device_status(args: dict[str, Any]) -> dict[str, Any]:
    return _text(_get_device_status(device_id=args["device_id"]))


# Backward-compatible (deprecated — use hierarchical tools instead)


@tool(
    "discover_devices",
    "Deprecated — use describe_fleet, list_devices, and get_device_functions "
    "instead. Discover all devices with full function schemas.",
    {"device_type": str, "refresh": bool},
)
async def discover_devices(args: dict[str, Any]) -> dict[str, Any]:
    return _text(
        _discover_devices(
            device_type=args.get("device_type"),
            refresh=bool(args.get("refresh", False)),
        )
    )


def create_device_connect_server(name: str = "device-connect"):
    """Return an in-process SDK MCP server exposing all Device Connect tools.

    Pass the result in ``ClaudeAgentOptions(mcp_servers={...})``.
    """
    return create_sdk_mcp_server(
        name,
        tools=[
            describe_fleet,
            list_devices,
            get_device_functions,
            invoke_device,
            invoke_device_with_fallback,
            get_device_status,
            discover_devices,
        ],
    )


__all__ = [
    "describe_fleet",
    "list_devices",
    "get_device_functions",
    "discover_devices",
    "invoke_device",
    "invoke_device_with_fallback",
    "get_device_status",
    "create_device_connect_server",
]
