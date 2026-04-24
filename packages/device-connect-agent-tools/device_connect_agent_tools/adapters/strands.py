# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Strands adapter — wraps Device Connect tools with @strands.tool.

Hierarchical discovery keeps LLM context small:

    from device_connect_agent_tools import connect
    from device_connect_agent_tools.adapters.strands import (
        describe_fleet, list_devices, get_device_functions, invoke_device,
    )
    from strands import Agent

    connect()
    agent = Agent(tools=[describe_fleet, list_devices, get_device_functions, invoke_device])
    agent("What devices are online?")

Requires: pip install device-connect-agent-tools[strands]
"""

from strands import tool as strands_tool

from device_connect_agent_tools.tools import (
    describe_fleet as _describe_fleet,
    list_devices as _list_devices,
    get_device_functions as _get_device_functions,
    discover_devices as _discover_devices,
    invoke_device as _invoke_device,
    invoke_device_with_fallback as _invoke_device_with_fallback,
    get_device_status as _get_device_status,
)

# Hierarchical discovery tools (recommended)
describe_fleet = strands_tool(_describe_fleet)
list_devices = strands_tool(_list_devices)
get_device_functions = strands_tool(_get_device_functions)

# Invocation tools
invoke_device = strands_tool(_invoke_device)
invoke_device_with_fallback = strands_tool(_invoke_device_with_fallback)
get_device_status = strands_tool(_get_device_status)

# Backward-compatible (deprecated — use hierarchical tools instead)
discover_devices = strands_tool(_discover_devices)

__all__ = [
    "describe_fleet",
    "list_devices",
    "get_device_functions",
    "discover_devices",
    "invoke_device",
    "invoke_device_with_fallback",
    "get_device_status",
]
