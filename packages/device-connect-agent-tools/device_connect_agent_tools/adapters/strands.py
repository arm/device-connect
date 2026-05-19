# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Strands adapter — wraps Device Connect tools with @strands.tool.

Selector-driven discovery keeps LLM context small:

    from device_connect_agent_tools import connect
    from device_connect_agent_tools.adapters.strands import (
        discover_labels, discover, invoke_device,
    )
    from strands import Agent

    connect()
    agent = Agent(tools=[discover_labels, discover, invoke_device])
    agent("What devices are online?")

Requires: pip install device-connect-agent-tools[strands]
"""

from strands import tool as strands_tool

from device_connect_agent_tools.tools import (
    discover as _discover,
    discover_labels as _discover_labels,
    discover_devices as _discover_devices,
    invoke_device as _invoke_device,
    invoke_device_with_fallback as _invoke_device_with_fallback,
    get_device_status as _get_device_status,
)

# Selector-driven discovery tools (recommended)
discover_labels = strands_tool(_discover_labels)
discover = strands_tool(_discover)

# Invocation tools
invoke_device = strands_tool(_invoke_device)
invoke_device_with_fallback = strands_tool(_invoke_device_with_fallback)
get_device_status = strands_tool(_get_device_status)

# Backward-compatible (long-deprecated — prefer discover() / discover_labels())
discover_devices = strands_tool(_discover_devices)

__all__ = [
    "discover_labels",
    "discover",
    "invoke_device",
    "invoke_device_with_fallback",
    "get_device_status",
    "discover_devices",
]
