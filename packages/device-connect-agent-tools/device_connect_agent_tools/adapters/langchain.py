# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""LangChain adapter — wraps Device Connect tools as LangChain StructuredTools.

Hierarchical discovery keeps LLM context small:

    from device_connect_agent_tools import connect
    from device_connect_agent_tools.adapters.langchain import (
        describe_fleet, list_devices, get_device_functions, invoke_device,
    )
    from langgraph.prebuilt import create_react_agent

    connect()
    agent = create_react_agent(model, [describe_fleet, list_devices, get_device_functions, invoke_device])

Requires: pip install device-connect-agent-tools[langchain]
"""

from langchain_core.tools import StructuredTool

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
describe_fleet = StructuredTool.from_function(_describe_fleet)
list_devices = StructuredTool.from_function(_list_devices)
get_device_functions = StructuredTool.from_function(_get_device_functions)

# Invocation tools
invoke_device = StructuredTool.from_function(_invoke_device)
invoke_device_with_fallback = StructuredTool.from_function(_invoke_device_with_fallback)
get_device_status = StructuredTool.from_function(_get_device_status)

# Backward-compatible (deprecated — use hierarchical tools instead)
discover_devices = StructuredTool.from_function(_discover_devices)

__all__ = [
    "describe_fleet",
    "list_devices",
    "get_device_functions",
    "discover_devices",
    "invoke_device",
    "invoke_device_with_fallback",
    "get_device_status",
]
