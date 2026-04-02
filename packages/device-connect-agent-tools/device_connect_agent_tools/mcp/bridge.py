"""MCP Bridge Server for Claude Desktop.

Connects Claude Desktop to Device Connect devices by exposing 4 meta-tools
for hierarchical device discovery, avoiding the MCP tool explosion problem:

1. describe_fleet     — bird's-eye fleet summary
2. list_devices       — paginated, filterable device roster (no schemas)
3. get_device_functions — full function schemas for ONE device
4. invoke_device      — call a function on a device

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

import json
import logging
from typing import Optional

from device_connect_edge.messaging import create_client
from device_connect_edge.messaging.base import MessagingClient
from device_connect_edge.registry_client import RegistryClient
from device_connect_agent_tools.mcp.config import BridgeConfig
from device_connect_agent_tools.mcp.router import ToolRouter, ToolInvocationError
from device_connect_agent_tools.tools import SMALL_FLEET_THRESHOLD

logger = logging.getLogger(__name__)

# Try to import fastmcp, provide helpful error if not installed
try:
    from fastmcp import FastMCP
    FASTMCP_AVAILABLE = True
except ImportError:
    FASTMCP_AVAILABLE = False
    FastMCP = None  # type: ignore


def _bridge_full_device(d: dict) -> dict:
    """Build full device dict from raw registry data (nested identity/status/capabilities)."""
    identity = d.get("identity") or {}
    status = d.get("status") or {}
    caps = d.get("capabilities") or {}
    functions = caps.get("functions", [])
    events = caps.get("events", [])
    return {
        "device_id": d.get("device_id"),
        "device_type": identity.get("device_type") or d.get("device_type"),
        "location": status.get("location") or d.get("location"),
        "functions": [
            {
                "name": f.get("name") if isinstance(f, dict) else f,
                "description": f.get("description", "") if isinstance(f, dict) else "",
                "parameters": f.get("parameters", {}) if isinstance(f, dict) else {},
            }
            for f in functions
        ],
        "events": [
            e.get("name") if isinstance(e, dict) else e for e in events
        ],
    }


class MCPBridgeServer:
    """MCP Bridge Server connecting Claude Desktop to Device Connect devices.

    This server:
    - Runs as an MCP server via stdio transport
    - Connects to Device Connect messaging for device discovery
    - Exposes 4 meta-tools for hierarchical discovery (not per-function)
    - Routes invocations through messaging to devices

    Example:
        config = BridgeConfig.from_environment()
        server = MCPBridgeServer(config)
        await server.start()
    """

    def __init__(self, config: BridgeConfig):
        if not FASTMCP_AVAILABLE:
            raise ImportError(
                "FastMCP is required for MCP Bridge. "
                "Install it with: pip install 'device-connect-agent-tools[mcp]'"
            )

        self.config = config
        self._mcp: Optional[FastMCP] = None
        self._messaging_client: Optional[MessagingClient] = None
        self._registry = None  # RegistryClient or D2DRegistry (DiscoveryProvider)
        self._router: Optional[ToolRouter] = None
        self._d2d_collector = None  # PresenceCollector, if in D2D mode
        self._running = False

    async def start(self) -> None:
        """Start the MCP Bridge Server."""
        if self._running:
            logger.warning("MCPBridgeServer already running")
            return

        self._running = True
        logger.info("Starting MCP Bridge Server")

        try:
            # Connect to messaging
            await self._connect_messaging()

            # Initialize discovery provider and router
            await self._init_discovery()
            self._router = ToolRouter(
                self._messaging_client,
                tenant=self.config.tenant,
                timeout=self.config.request_timeout,
            )

            # Create FastMCP server with 4 meta-tools
            self._mcp = FastMCP("Device Connect Bridge")
            self._register_meta_tools()

            logger.info("MCP Bridge ready — 4 meta-tools registered")
            await self._mcp.run_stdio_async()

        finally:
            await self._cleanup()

    def _is_d2d_mode(self) -> bool:
        """Determine if D2D (peer mesh) discovery should be used."""
        mode = self.config.discovery_mode
        if mode in ("d2d", "p2p"):
            return True
        if mode == "infra":
            return False
        # "auto": use D2D if backend is zenoh and no explicit URLs were configured
        is_zenoh = any(
            not url.startswith("nats://") and not url.startswith("tls://")
            for url in self.config.messaging_urls
        )
        default_url = self.config.messaging_urls == ["tcp/localhost:7447"]
        return is_zenoh and default_url

    async def _init_discovery(self) -> None:
        """Initialize discovery provider — D2D (PresenceCollector) or infra (RegistryClient)."""
        if self._is_d2d_mode():
            from device_connect_edge.discovery import PresenceCollector, D2DRegistry
            logger.info("Using D2D discovery (PresenceCollector)")
            self._d2d_collector = PresenceCollector(
                self._messaging_client, self.config.tenant
            )
            await self._d2d_collector.start()
            await self._d2d_collector.wait_for_peers(timeout=3.0)
            self._registry = D2DRegistry(self._d2d_collector)
        else:
            logger.info("Using infra discovery (RegistryClient)")
            self._registry = RegistryClient(
                self._messaging_client,
                tenant=self.config.tenant,
                timeout=self.config.request_timeout,
                cache_ttl=self.config.refresh_interval,
            )

    async def _connect_messaging(self) -> None:
        logger.info(f"Connecting to messaging: {self.config.messaging_urls}")
        self._messaging_client = create_client("nats")
        await self._messaging_client.connect(
            servers=self.config.messaging_urls,
            credentials=self.config.messaging_auth,
            tls_config=self.config.messaging_tls,
        )
        logger.info("Connected to messaging")

    def _register_meta_tools(self) -> None:
        """Register the 4 hierarchical discovery meta-tools."""

        @self._mcp.tool(
            name="describe_fleet",
            description=(
                "Get a high-level summary of all available IoT devices. "
                "Returns device counts grouped by type and location. "
                "Call this first to understand what devices are available."
            ),
        )
        async def describe_fleet() -> str:
            """Fleet summary with type/location groupings."""
            devices = await self._registry.list_devices()
            from collections import defaultdict

            by_type: dict = defaultdict(lambda: {"count": 0, "locations": set()})
            by_location: dict = defaultdict(lambda: {"count": 0, "types": set()})
            total_functions = 0

            for d in devices:
                identity = d.get("identity") or {}
                status = d.get("status") or {}
                caps = d.get("capabilities") or {}
                dt = identity.get("device_type") or d.get("device_type") or "unknown"
                loc = status.get("location") or d.get("location") or "unknown"
                funcs = caps.get("functions", [])
                total_functions += len(funcs)

                by_type[dt]["count"] += 1
                by_type[dt]["locations"].add(loc)
                by_location[loc]["count"] += 1
                by_location[loc]["types"].add(dt)

            result = {
                "total_devices": len(devices),
                "total_functions": total_functions,
                "by_type": {
                    k: {"count": v["count"], "locations": sorted(v["locations"])}
                    for k, v in sorted(by_type.items())
                },
                "by_location": {
                    k: {"count": v["count"], "types": sorted(v["types"])}
                    for k, v in sorted(by_location.items())
                },
            }

            # Auto-expand: include full device details for small fleets
            if SMALL_FLEET_THRESHOLD > 0 and len(devices) <= SMALL_FLEET_THRESHOLD:
                result["devices"] = [_bridge_full_device(d) for d in devices]
                result["hint"] = (
                    "Full device details included — skip list_devices / "
                    "get_device_functions and go straight to invoke_device."
                )

            return json.dumps(result, indent=2)

        @self._mcp.tool(
            name="list_devices",
            description=(
                "Browse available IoT devices with filtering and pagination. "
                "Returns compact summaries WITHOUT function schemas. "
                "Use get_device_functions(device_id) to see what a device can do."
            ),
        )
        async def list_devices(
            device_type: str = "",
            location: str = "",
            group_by: str = "",
            offset: int = 0,
            limit: int = 20,
        ) -> str:
            """Paginated, filterable device list."""
            devices = await self._registry.list_devices(
                device_type=device_type or None,
                location=location or None,
            )

            def _summary(d: dict, expand: bool) -> dict:
                identity = d.get("identity") or {}
                status = d.get("status") or {}
                caps = d.get("capabilities") or {}
                funcs = caps.get("functions", [])
                result = {
                    "device_id": d.get("device_id"),
                    "device_type": identity.get("device_type") or d.get("device_type"),
                    "location": status.get("location") or d.get("location"),
                    "function_count": len(funcs),
                    "function_names": [
                        f.get("name") if isinstance(f, dict) else f for f in funcs
                    ],
                }
                if expand:
                    result["functions"] = [
                        {
                            "name": f.get("name") if isinstance(f, dict) else f,
                            "description": f.get("description", "") if isinstance(f, dict) else "",
                            "parameters": f.get("parameters", {}) if isinstance(f, dict) else {},
                        }
                        for f in funcs
                    ]
                return result

            total = len(devices)

            if group_by in ("location", "device_type"):
                from collections import defaultdict as dd
                expand = SMALL_FLEET_THRESHOLD > 0 and total <= SMALL_FLEET_THRESHOLD
                groups: dict = dd(list)
                for d in devices:
                    c = _summary(d, expand)
                    key = c.get(group_by) or "unknown"
                    groups[key].append(c)
                result = {"groups": dict(sorted(groups.items())), "total": total}
            else:
                page = devices[offset:offset + limit]
                expand = SMALL_FLEET_THRESHOLD > 0 and len(page) <= SMALL_FLEET_THRESHOLD
                result = {
                    "devices": [_summary(d, expand) for d in page],
                    "total": total,
                    "offset": offset,
                    "limit": limit,
                    "has_more": offset + limit < total,
                }

            return json.dumps(result, indent=2)

        @self._mcp.tool(
            name="get_device_functions",
            description=(
                "Get full function schemas for a specific device. "
                "Call this after list_devices() to see what parameters "
                "each function accepts before invoking it."
            ),
        )
        async def get_device_functions(device_id: str) -> str:
            """Full function schemas for one device."""
            device = await self._registry.get_device(device_id)
            if not device:
                return json.dumps({"error": f"Device {device_id} not found"})

            identity = device.get("identity") or {}
            status = device.get("status") or {}
            caps = device.get("capabilities") or {}
            functions = caps.get("functions", [])
            events = caps.get("events", [])

            result = {
                "device_id": device.get("device_id"),
                "device_type": identity.get("device_type"),
                "location": status.get("location"),
                "functions": [
                    {
                        "name": f.get("name") if isinstance(f, dict) else f,
                        "description": f.get("description", "") if isinstance(f, dict) else "",
                        "parameters": f.get("parameters", {}) if isinstance(f, dict) else {},
                    }
                    for f in functions
                ],
                "events": [
                    e.get("name") if isinstance(e, dict) else e for e in events
                ],
            }
            return json.dumps(result, indent=2)

        @self._mcp.tool(
            name="invoke_device",
            description=(
                "Call a function on a Device Connect device. "
                "Use get_device_functions() first to see available functions "
                "and their parameter schemas."
            ),
        )
        async def invoke_device(
            device_id: str,
            function: str,
            arguments: str = "{}",
        ) -> str:
            """Invoke a device function."""
            try:
                args = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError:
                args = {}

            tool_name = f"{device_id}::{function}"
            try:
                result = await self._router.invoke(tool_name, args)
                return json.dumps({"success": True, "result": result}, indent=2)
            except ToolInvocationError as e:
                return json.dumps({"success": False, "error": str(e)})

    async def _cleanup(self) -> None:
        self._running = False
        if self._d2d_collector:
            try:
                await self._d2d_collector.stop()
            except Exception as e:
                logger.warning(f"Error stopping D2D collector: {e}")
            self._d2d_collector = None
        if self._messaging_client:
            try:
                await self._messaging_client.close()
            except Exception as e:
                logger.warning(f"Error closing messaging client: {e}")
        self._messaging_client = None
        self._registry = None
        self._router = None
        self._mcp = None

    async def stop(self) -> None:
        self._running = False
        await self._cleanup()


# Convenience function for running the bridge
async def run_bridge() -> None:
    """Run the MCP Bridge Server with configuration from environment."""
    config = BridgeConfig.from_environment()
    server = MCPBridgeServer(config)
    await server.start()
