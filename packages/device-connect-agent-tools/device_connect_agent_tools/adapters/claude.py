# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Claude Agent SDK adapter — exposes Device Connect tools to claude-agent-sdk.

Selector-driven discovery and invocation keep LLM context small::

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
    discover as _discover,
    discover_labels as _discover_labels,
    discover_devices as _discover_devices,
    invoke as _invoke,
    invoke_many as _invoke_many,
    broadcast as _broadcast,
    await_replies as _await_replies,
    invoke_device_with_fallback as _invoke_device_with_fallback,
    get_device_status as _get_device_status,
)


def _text(result: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}


# Selector-driven discovery tools (recommended)


@tool(
    "discover_labels",
    "Browse the label vocabulary across the fleet. Returns label keys "
    "(category, location, direction, modality, ...) with their values and "
    "counts. Call with no arguments to see all keys, or with key="
    "'device.location' / 'function.direction' / etc. to paginate one key. "
    "Use this first to learn what dimensions are available before calling "
    "discover().",
    {"key": str, "offset": int, "limit": int},
)
async def discover_labels(args: dict[str, Any]) -> dict[str, Any]:
    return _text(
        _discover_labels(
            key=args.get("key"),
            offset=int(args.get("offset", 0)),
            limit=int(args.get("limit", 50)),
        )
    )


@tool(
    "discover",
    "Resolve a selector to matched devices, functions, or events. Selector "
    "grammar: device(<filters>), device(<filters>).function(<filters>), "
    "device(<filters>).event(<filters>), function(<filters>), or "
    "event(<filters>). Filters are key:value pairs (AND across keys with "
    "commas, OR within a key with bracket lists, glob with *). Examples: "
    "'device(category:camera, location:zone-A/*)', "
    "'device(*).function(direction:write)', 'event(modality:motion)'. "
    "Response includes a label_histogram (per-key vocabulary across the "
    "matched set) so the agent can narrow next.",
    {"selector": str, "offset": int, "limit": int},
)
async def discover(args: dict[str, Any]) -> dict[str, Any]:
    return _text(
        _discover(
            selector=args["selector"],
            offset=int(args.get("offset", 0)),
            limit=int(args.get("limit", 200)),
        )
    )


# Selector-driven invocation tools (recommended)


@tool(
    "invoke",
    "Call exactly one function on one device. The selector must resolve "
    "to a single (device, function) tuple -- use device(<id>).function(<name>) "
    "or function(<name>) scope. Returns {success, device_id, function, "
    "result|error}. Use invoke_many for fan-out across multiple targets.",
    {
        "selector": str, "params": dict, "llm_reasoning": str,
        "mandate": dict,
    },
)
async def invoke(args: dict[str, Any]) -> dict[str, Any]:
    return _text(
        _invoke(
            selector=args["selector"],
            params=args.get("params"),
            llm_reasoning=args.get("llm_reasoning"),
            mandate=args.get("mandate"),
        )
    )


@tool(
    "invoke_many",
    "Fan out a function call over a selector-resolved set of (device, "
    "function) tuples in parallel. Partial-failure semantics: per-target "
    "results and errors are returned even if some targets fail. Returns "
    "{candidates, matched, succeeded, failed, results, errors}. Each "
    "target gets a per-call timeout (default 30s).",
    {
        "selector": str, "params": dict, "timeout": float,
        "max_concurrency": int, "llm_reasoning": str, "mandate": dict,
    },
)
async def invoke_many(args: dict[str, Any]) -> dict[str, Any]:
    return _text(
        _invoke_many(
            selector=args["selector"],
            params=args.get("params"),
            timeout=float(args.get("timeout", 30.0)),
            max_concurrency=int(args.get("max_concurrency", 32)),
            llm_reasoning=args.get("llm_reasoning"),
            mandate=args.get("mandate"),
        )
    )


@tool(
    "broadcast",
    "Async selector-driven fan-out. Returns immediately with a "
    "correlation_id; replies stream on a per-device subject keyed by id. "
    "Each candidate self-elects via the optional CEL `where` predicate "
    "(evaluated at the edge against identity/labels/status/bindings) and "
    "executes the function. Use fire_at (wall-clock epoch seconds) + "
    "on_late (skip|fire) for synchronized fan-out. Pair with "
    "await_replies(correlation_id) to collect outcomes.",
    {
        "selector": str, "params": dict, "where": str, "bindings": dict,
        "fire_at": float, "on_late": str, "llm_reasoning": str,
        "mandate": dict,
    },
)
async def broadcast(args: dict[str, Any]) -> dict[str, Any]:
    return _text(
        _broadcast(
            selector=args["selector"],
            params=args.get("params"),
            where=args.get("where"),
            bindings=args.get("bindings"),
            fire_at=args.get("fire_at"),
            on_late=args.get("on_late", "skip"),
            llm_reasoning=args.get("llm_reasoning"),
            mandate=args.get("mandate"),
        )
    )


@tool(
    "await_replies",
    "Collect replies for a broadcast() call. Subscribes to the "
    "correlation reply subject, drains for up to `timeout` seconds (or "
    "until `until` replies have arrived), then returns the list.",
    {
        "correlation_id": str, "timeout": float, "until": int,
        "poll_interval": float,
    },
)
async def await_replies(args: dict[str, Any]) -> dict[str, Any]:
    return _text(
        _await_replies(
            correlation_id=args["correlation_id"],
            timeout=float(args.get("timeout", 10.0)),
            until=int(args["until"]) if args.get("until") is not None else None,
            poll_interval=float(args.get("poll_interval", 0.05)),
        )
    )


# Other invocation helpers


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


# Backward-compatible (long-deprecated -- prefer discover() / invoke())


@tool(
    "discover_devices",
    "Deprecated -- use discover() and discover_labels() instead. Discovers "
    "all devices with full function schemas.",
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
            discover_labels,
            discover,
            invoke,
            invoke_many,
            broadcast,
            await_replies,
            invoke_device_with_fallback,
            get_device_status,
            discover_devices,
        ],
    )


__all__ = [
    "discover_labels",
    "discover",
    "invoke",
    "invoke_many",
    "broadcast",
    "await_replies",
    "invoke_device_with_fallback",
    "get_device_status",
    "discover_devices",
    "create_device_connect_server",
]
