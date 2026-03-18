"""MCP Bridge Server for Claude Desktop.

Connects Claude Desktop to Device Connect devices by:
1. Discovering devices via NATS registry
2. Exposing device functions as MCP tools
3. Routing MCP tool calls to devices via NATS

Usage:
    # As a module (for Claude Desktop config)
    python -m device_connect_agent_tools.mcp

    # Programmatically
    from device_connect_agent_tools.mcp import MCPBridgeServer, BridgeConfig

    config = BridgeConfig.from_environment()
    server = MCPBridgeServer(config)
    await server.start()
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from device_connect_sdk.messaging import create_client
from device_connect_sdk.messaging.base import MessagingClient
from device_connect_agent_tools.mcp.config import BridgeConfig
from device_connect_agent_tools.mcp.discovery import DeviceDiscoveryClient, DiscoveryError
from device_connect_agent_tools.mcp.router import ToolRouter, ToolInvocationError
from device_connect_agent_tools.mcp.schema import MCPToolDefinition

logger = logging.getLogger(__name__)

# Try to import fastmcp, provide helpful error if not installed
try:
    from fastmcp import FastMCP
    FASTMCP_AVAILABLE = True
except ImportError:
    FASTMCP_AVAILABLE = False
    FastMCP = None  # type: ignore


class MCPBridgeServer:
    """MCP Bridge Server connecting Claude Desktop to Device Connect devices.

    This server:
    - Runs as an MCP server via stdio transport
    - Connects to Device Connect NATS for device discovery
    - Exposes all device functions as MCP tools
    - Routes tool calls through NATS to devices

    Example:
        config = BridgeConfig.from_environment()
        server = MCPBridgeServer(config)
        await server.start()
    """

    def __init__(self, config: BridgeConfig):
        """Initialize MCP Bridge Server.

        Args:
            config: Bridge configuration
        """
        if not FASTMCP_AVAILABLE:
            raise ImportError(
                "FastMCP is required for MCP Bridge. "
                "Install it with: pip install 'device-connect-agent-tools[mcp]'"
            )

        self.config = config
        self._mcp: Optional[FastMCP] = None
        self._messaging_client: Optional[MessagingClient] = None
        self._discovery: Optional[DeviceDiscoveryClient] = None
        self._router: Optional[ToolRouter] = None
        self._tools: Dict[str, MCPToolDefinition] = {}
        self._running = False

    async def start(self) -> None:
        """Start the MCP Bridge Server.

        This connects to NATS, discovers devices, and runs the MCP server.
        The method blocks until the server is stopped.
        """
        if self._running:
            logger.warning("MCPBridgeServer already running")
            return

        self._running = True
        logger.info("Starting MCP Bridge Server")
        logger.debug(f"Config: {self.config.to_dict()}")

        try:
            # Connect to NATS
            await self._connect_messaging()

            # Initialize discovery and router
            self._discovery = DeviceDiscoveryClient(
                self._messaging_client,
                tenant=self.config.tenant,
                cache_ttl=self.config.refresh_interval,
            )
            self._router = ToolRouter(
                self._messaging_client,
                tenant=self.config.tenant,
                timeout=self.config.request_timeout,
            )

            # Create FastMCP server
            self._mcp = FastMCP("Device Connect Bridge")

            # Register the dynamic tool handler
            self._register_dynamic_tools()

            # Do initial tool discovery BEFORE starting MCP server
            # This ensures tools are available when Claude Desktop first queries
            logger.info("Performing initial tool discovery...")
            await self._refresh_tools()

            # Start background refresh task
            refresh_task = asyncio.create_task(self._refresh_loop())

            try:
                # Run MCP server (async - for stdio transport)
                logger.info("MCP Bridge ready - waiting for connections")
                await self._mcp.run_stdio_async()
            finally:
                refresh_task.cancel()
                try:
                    await refresh_task
                except asyncio.CancelledError:
                    pass

        finally:
            await self._cleanup()

    async def _connect_messaging(self) -> None:
        """Connect to NATS messaging."""
        logger.info(f"Connecting to NATS: {self.config.messaging_urls}")

        self._messaging_client = create_client("nats")
        await self._messaging_client.connect(
            servers=self.config.messaging_urls,
            credentials=self.config.messaging_auth,
            tls_config=self.config.messaging_tls,
        )

        logger.info("Connected to NATS")

    def _register_dynamic_tools(self) -> None:
        """Register the dynamic tool handler with FastMCP.

        Since device tools are discovered dynamically, we register a single
        handler that routes to the appropriate device based on tool name.
        """
        # Register tools/list handler to return discovered tools
        @self._mcp.resource("device-connect://tools")
        async def list_device_connect_tools() -> str:
            """List all available Device Connect device tools."""
            tools = await self._get_tools()
            return "\n".join(
                f"- {t.name}: {t.description}"
                for t in tools
            )

    async def _refresh_loop(self) -> None:
        """Periodically refresh the tool list from device registry."""
        while self._running:
            try:
                await self._refresh_tools()
            except Exception as e:
                logger.error(f"Tool refresh failed: {e}")

            await asyncio.sleep(self.config.refresh_interval)

    async def _refresh_tools(self) -> None:
        """Refresh available tools from device registry."""
        if not self._discovery or not self._mcp:
            return

        try:
            tools = await self._discovery.get_tools()
            logger.info(f"Discovered {len(tools)} tools from registry")

            # Update tool cache
            new_tools = {t.name: t for t in tools}

            # Register new tools with FastMCP
            for name, tool_def in new_tools.items():
                if name not in self._tools:
                    self._register_tool(tool_def)

            self._tools = new_tools

        except DiscoveryError as e:
            logger.warning(f"Discovery failed: {e}")

    def _register_tool(self, tool_def: MCPToolDefinition) -> None:
        """Register a single tool with FastMCP."""
        # Original name uses :: separator (e.g., device-id::function_name)
        original_name = tool_def.name

        # MCP-compliant name uses -- separator (only a-zA-Z0-9_- allowed)
        mcp_name = original_name.replace("::", "--")

        logger.info(f"Registering tool: {mcp_name}")

        # FastMCP doesn't support **kwargs, so we use a single dict parameter
        # that we'll pass through to the device
        async def tool_handler(arguments: str = "{}") -> Any:
            """Dynamic tool handler.

            Args:
                arguments: JSON string of arguments to pass to the device function
            """
            import json
            try:
                args = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError:
                args = {}
            # Use original name with :: for routing to device
            return await self._invoke_tool(original_name, args)

        # Set function metadata for FastMCP
        tool_handler.__name__ = mcp_name.replace(".", "_").replace("-", "_")
        tool_handler.__doc__ = tool_def.description or f"Invoke {original_name}"

        # Register with FastMCP using the MCP-compliant name
        self._mcp.tool(
            name=mcp_name,
            description=tool_def.description or f"Invoke {original_name}",
        )(tool_handler)

    async def _invoke_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Any:
        """Invoke a tool via the router.

        Args:
            tool_name: Tool name (device_id::function_name)
            arguments: Tool arguments

        Returns:
            Tool result
        """
        if not self._router:
            raise RuntimeError("Router not initialized")

        logger.info(f"Invoking tool: {tool_name}")

        try:
            result = await self._router.invoke(tool_name, arguments)
            return result
        except ToolInvocationError as e:
            logger.error(f"Tool invocation failed: {e}")
            raise

    async def _get_tools(self) -> List[MCPToolDefinition]:
        """Get current list of tools."""
        if not self._discovery:
            return []

        try:
            return await self._discovery.get_tools()
        except DiscoveryError:
            # Return cached tools on discovery failure
            return list(self._tools.values())

    async def _cleanup(self) -> None:
        """Clean up resources."""
        self._running = False

        if self._messaging_client:
            try:
                await self._messaging_client.close()
            except Exception as e:
                logger.warning(f"Error closing messaging client: {e}")

        self._messaging_client = None
        self._discovery = None
        self._router = None
        self._mcp = None

    async def stop(self) -> None:
        """Stop the server gracefully."""
        self._running = False
        await self._cleanup()


# Convenience function for running the bridge
async def run_bridge() -> None:
    """Run the MCP Bridge Server with configuration from environment."""
    config = BridgeConfig.from_environment()
    server = MCPBridgeServer(config)
    await server.start()
