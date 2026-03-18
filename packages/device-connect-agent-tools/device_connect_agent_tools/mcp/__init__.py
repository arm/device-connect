"""MCP Bridge for Device Connect.

This module provides:
- DeviceConnectMCP: FastMCP-compatible API for building Device Connect devices
- MCPBridgeServer: Bridge server connecting Claude Desktop to Device Connect

Device-side usage (FastMCP-compatible):
    from device_connect_agent_tools.mcp import DeviceConnectMCP

    mcp = DeviceConnectMCP("my-sensor-001")

    @mcp.tool()
    async def read_temperature() -> dict:
        '''Read current temperature.'''
        return {"celsius": 22.5}

    await mcp.run()

Bridge-side usage (Claude Desktop):
    Configure in ~/Library/Application Support/Claude/claude_desktop_config.json:
    {"mcpServers": {"device-connect": {"command": "python", "args": ["-m", "device_connect_agent_tools.mcp"]}}}
"""

from device_connect_agent_tools.mcp.device_connect_mcp import DeviceConnectMCP
from device_connect_agent_tools.mcp.bridge import MCPBridgeServer, run_bridge
from device_connect_agent_tools.mcp.config import BridgeConfig
from device_connect_agent_tools.mcp.discovery import DeviceDiscoveryClient, DiscoveryError
from device_connect_agent_tools.mcp.router import ToolRouter, ToolInvocationError, ToolNotFoundError
from device_connect_agent_tools.mcp.schema import (
    MCPToolDefinition,
    function_to_mcp_tool,
    parse_tool_name,
    devices_to_mcp_tools,
)
__all__ = [
    # Device-side API
    "DeviceConnectMCP",
    # Bridge-side API
    "MCPBridgeServer",
    "run_bridge",
    "BridgeConfig",
    # Discovery
    "DeviceDiscoveryClient",
    "DiscoveryError",
    # Routing
    "ToolRouter",
    "ToolInvocationError",
    "ToolNotFoundError",
    # Schema
    "MCPToolDefinition",
    "function_to_mcp_tool",
    "parse_tool_name",
    "devices_to_mcp_tools",
]


def __getattr__(name):
    """Lazy import for DeviceToolsServer to avoid circular import issues."""
    if name == "DeviceToolsServer":
        from device_connect_agent_tools.mcp.device_tools import DeviceToolsServer
        return DeviceToolsServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
