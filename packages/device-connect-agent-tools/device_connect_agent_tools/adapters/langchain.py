# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""LangChain adapter — wraps Device Connect tools as LangChain StructuredTools.

Selector-driven discovery keeps LLM context small:

    from device_connect_agent_tools import connect
    from device_connect_agent_tools.adapters.langchain import (
        discover_labels, discover, invoke_device,
    )
    from langgraph.prebuilt import create_react_agent

    connect()
    agent = create_react_agent(model, [discover_labels, discover, invoke_device])

Requires: pip install device-connect-agent-tools[langchain]
"""

from langchain_core.tools import StructuredTool

from device_connect_agent_tools.tools import (
    discover as _discover,
    discover_labels as _discover_labels,
    discover_devices as _discover_devices,
    invoke_device as _invoke_device,
    invoke_device_with_fallback as _invoke_device_with_fallback,
    get_device_status as _get_device_status,
)

# Selector-driven discovery tools (recommended)
discover_labels = StructuredTool.from_function(_discover_labels)
discover = StructuredTool.from_function(_discover)

# Invocation tools
invoke_device = StructuredTool.from_function(_invoke_device)
invoke_device_with_fallback = StructuredTool.from_function(_invoke_device_with_fallback)
get_device_status = StructuredTool.from_function(_get_device_status)

# Backward-compatible (long-deprecated — prefer discover() / discover_labels())
discover_devices = StructuredTool.from_function(_discover_devices)

__all__ = [
    "discover_labels",
    "discover",
    "invoke_device",
    "invoke_device_with_fallback",
    "get_device_status",
    "discover_devices",
]
