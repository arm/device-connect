"""LangChain adapter — wraps Device Connect tools as LangChain StructuredTools.

Usage:
    from device_connect_agent_tools import connect
    from device_connect_agent_tools.adapters.langchain import discover_devices, invoke_device
    from langgraph.prebuilt import create_react_agent

    connect()
    agent = create_react_agent(model, [discover_devices, invoke_device])

Requires: pip install device-connect-agent-tools[langchain]
"""

from langchain_core.tools import StructuredTool

from device_connect_agent_tools.tools import (
    discover_devices as _discover_devices,
    invoke_device as _invoke_device,
    invoke_device_with_fallback as _invoke_device_with_fallback,
    get_device_status as _get_device_status,
)

discover_devices = StructuredTool.from_function(_discover_devices)
invoke_device = StructuredTool.from_function(_invoke_device)
invoke_device_with_fallback = StructuredTool.from_function(_invoke_device_with_fallback)
get_device_status = StructuredTool.from_function(_get_device_status)

__all__ = [
    "discover_devices",
    "invoke_device",
    "invoke_device_with_fallback",
    "get_device_status",
]
