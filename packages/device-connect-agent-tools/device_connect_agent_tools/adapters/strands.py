# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Strands adapter — wraps Device Connect tools with @strands.tool.

Selector-driven discovery and invocation keep LLM context small:

    from device_connect_agent_tools import connect
    from device_connect_agent_tools.adapters.strands import (
        discover_labels, discover, invoke, invoke_many,
    )
    from strands import Agent

    connect()
    agent = Agent(tools=[discover_labels, discover, invoke, invoke_many])
    agent("What devices are online?")

Requires: pip install device-connect-agent-tools[strands]
"""

from strands import tool as strands_tool

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

# Selector-driven discovery (recommended)
discover_labels = strands_tool(_discover_labels)
discover = strands_tool(_discover)

# Selector-driven invocation (recommended)
invoke = strands_tool(_invoke)
invoke_many = strands_tool(_invoke_many)
broadcast = strands_tool(_broadcast)
await_replies = strands_tool(_await_replies)

# Other invocation helpers
invoke_device_with_fallback = strands_tool(_invoke_device_with_fallback)
get_device_status = strands_tool(_get_device_status)

# Backward-compatible (long-deprecated -- prefer discover() / invoke())
discover_devices = strands_tool(_discover_devices)

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
]
