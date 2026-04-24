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
from device_connect_agent_tools.mcp.event_subscriptions import (
    EventSubscriptionManager, device_id_from_uri,
)
from device_connect_agent_tools.mcp.router import ToolRouter, ToolInvocationError
from device_connect_agent_tools.tools import SMALL_FLEET_THRESHOLD
from device_connect_agent_tools.connection import flatten_device
from device_connect_agent_tools._normalize import (
    full_device, compact_device, fuzzy_filter_by_type, extract_status,
    aggregate_fleet, group_devices,
)

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
        self._event_subs: Optional[EventSubscriptionManager] = None
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

            # Create FastMCP server with 4 meta-tools + event resource
            self._mcp = FastMCP("Device Connect Bridge")
            self._register_meta_tools()

            self._event_subs = EventSubscriptionManager(
                self._messaging_client, self.config.tenant,
            )
            self._register_event_resource()
            self._register_subscription_handlers()
            self._enable_subscribe_capability()

            logger.info("MCP Bridge ready — 4 meta-tools + events resource registered")
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
        # "auto": D2D when backend is Zenoh
        backend = self.config.get_backend()
        is_d2d = backend == "zenoh"
        logger.info("Auto-detected discovery mode: %s (backend=%s)", "d2d" if is_d2d else "infra", backend)
        return is_d2d

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

    async def _list_devices(
        self,
        location: str | None = None,
    ) -> list[dict]:
        """List devices from registry and flatten to canonical shape.

        Only *location* is passed server-side (exact-match filter).
        ``device_type`` filtering is done client-side via
        :func:`fuzzy_filter_by_type` — the provider's strict filter can
        reject valid fuzzy matches (e.g. "environmentsensor" vs
        "environment_sensor").  See ``tools.py:list_devices`` for the
        same pattern and rationale.
        """
        raw = await self._registry.list_devices(location=location)
        return [flatten_device(d) for d in raw]

    async def _get_device(self, device_id: str) -> dict | None:
        """Get one device from registry and flatten to canonical shape."""
        raw = await self._registry.get_device(device_id)
        return flatten_device(raw) if raw else None

    async def _connect_messaging(self) -> None:
        backend = self.config.get_backend()
        logger.info("Connecting to messaging: %s (backend=%s)", self.config.messaging_urls, backend)
        self._messaging_client = create_client(backend)
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
                "For small fleets, full device details are included automatically. "
                "Call this first to understand what devices are available."
            ),
        )
        async def describe_fleet() -> str:
            """Fleet summary with type/location groupings."""
            devices = await self._list_devices()

            result = aggregate_fleet(devices)

            # Auto-expand: include full device details for small fleets
            if SMALL_FLEET_THRESHOLD > 0 and len(devices) <= SMALL_FLEET_THRESHOLD:
                result["devices"] = [full_device(d) for d in devices]
                result["hint"] = (
                    "Full device details included — skip list_devices / "
                    "get_device_functions and go straight to invoke_device."
                )

            return json.dumps(result, indent=2)

        @self._mcp.tool(
            name="list_devices",
            description=(
                "Browse available IoT devices with filtering and pagination. "
                "Returns compact summaries; for small fleets, full function "
                "schemas are included automatically. "
                "Use get_device_functions(device_id) for full schemas on larger fleets."
            ),
        )
        async def list_devices(
            device_type: str = "",
            location: str = "",
            status: str = "",
            group_by: str = "",
            offset: int = 0,
            limit: int = 20,
        ) -> str:
            """Paginated, filterable device list."""
            devices = await self._list_devices(location=location or None)

            # Client-side fuzzy type filter — sole type filter; provider-level
            # type filtering is intentionally skipped (see _list_devices docstring).
            if device_type:
                devices = fuzzy_filter_by_type(devices, device_type)

            # Client-side status filter
            if status:
                s = status.lower()
                devices = [d for d in devices if s in extract_status(d).lower()]

            def _summary(d: dict, expand: bool) -> dict:
                result = compact_device(d, expand)
                result["status"] = extract_status(d)
                return result

            total = len(devices)

            if group_by in ("location", "device_type"):
                expand = SMALL_FLEET_THRESHOLD > 0 and total <= SMALL_FLEET_THRESHOLD
                result = group_devices(devices, group_by, expand)
            else:
                page = devices[offset:offset + limit]
                expand = SMALL_FLEET_THRESHOLD > 0 and total <= SMALL_FLEET_THRESHOLD
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
            device = await self._get_device(device_id)
            if not device:
                return json.dumps({"error": f"Device {device_id} not found"})
            return json.dumps(full_device(device), indent=2)

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
            except json.JSONDecodeError as e:
                return json.dumps({"success": False, "error": f"Invalid JSON arguments: {e}"})

            tool_name = f"{device_id}::{function}"
            try:
                result = await self._router.invoke(tool_name, args)
                return json.dumps({"success": True, "result": result}, indent=2)
            except ToolInvocationError as e:
                return json.dumps({"success": False, "error": str(e)})

    def _register_event_resource(self) -> None:
        """Resource template returning the latest Device Connect event for a device."""
        assert self._mcp is not None
        assert self._event_subs is not None
        event_subs = self._event_subs

        @self._mcp.resource(
            "events://devices/{device_id}/latest",
            mime_type="application/json",
            description=(
                "Latest Device Connect event for the given device. Subscribe via "
                "resources/subscribe to receive notifications/resources/updated when "
                "the device emits a new event (progress, work_done, work_failed, ...)."
            ),
        )
        async def latest_event(device_id: str) -> str:
            return await event_subs.read(device_id)

    def _register_subscription_handlers(self) -> None:
        """Wire the low-level subscribe/unsubscribe MCP handlers."""
        assert self._mcp is not None
        assert self._event_subs is not None
        low = self._mcp._mcp_server
        event_subs = self._event_subs

        @low.subscribe_resource()
        async def _on_subscribe(uri):
            await event_subs.subscribe(str(uri))

        @low.unsubscribe_resource()
        async def _on_unsubscribe(uri):
            await event_subs.unsubscribe(str(uri))

    def _enable_subscribe_capability(self) -> None:
        """Advertise resources.subscribe=True; the MCP SDK hardcodes False otherwise."""
        assert self._mcp is not None
        low = self._mcp._mcp_server
        original = low.get_capabilities

        def patched(notification_options, experimental_capabilities):
            caps = original(notification_options, experimental_capabilities)
            if caps.resources is not None:
                caps.resources.subscribe = True
            return caps

        low.get_capabilities = patched  # type: ignore[assignment]

    async def _cleanup(self) -> None:
        self._running = False
        if self._event_subs:
            try:
                await self._event_subs.close()
            except Exception as e:
                logger.warning("Error closing event subscription manager: %s", e)
            self._event_subs = None
        if self._d2d_collector:
            try:
                await self._d2d_collector.stop()
            except Exception as e:
                logger.warning("Error stopping D2D collector: %s", e)
            self._d2d_collector = None
        if self._messaging_client:
            try:
                await self._messaging_client.close()
            except Exception as e:
                logger.warning("Error closing messaging client: %s", e)
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
