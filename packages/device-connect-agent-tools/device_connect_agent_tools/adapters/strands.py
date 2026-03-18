"""Strands adapter — wraps Device Connect tools with @strands.tool.

Usage:
    from device_connect_agent_tools import connect
    from device_connect_agent_tools.adapters.strands import discover_devices, invoke_device
    from strands import Agent

    connect()
    agent = Agent(tools=[discover_devices, invoke_device])
    agent("What devices are online?")

Requires: pip install device-connect-agent-tools[strands]
"""

from strands import tool as strands_tool

from device_connect_agent_tools.tools import (
    discover_devices as _discover_devices,
    invoke_device as _invoke_device,
    invoke_device_with_fallback as _invoke_device_with_fallback,
    get_device_status as _get_device_status,
)

discover_devices = strands_tool(_discover_devices)
invoke_device = strands_tool(_invoke_device)
invoke_device_with_fallback = strands_tool(_invoke_device_with_fallback)
get_device_status = strands_tool(_get_device_status)

__all__ = [
    "discover_devices",
    "invoke_device",
    "invoke_device_with_fallback",
    "get_device_status",
]
