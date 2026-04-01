"""Device discovery client for MCP Bridge.

Queries the Device Connect device registry to discover available devices
and their capabilities (functions/tools).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from device_connect_edge.messaging.base import MessagingClient
from device_connect_agent_tools.mcp.schema import MCPToolDefinition, devices_to_mcp_tools

logger = logging.getLogger(__name__)


@dataclass
class DeviceInfo:
    """Information about a registered device."""

    device_id: str
    device_type: Optional[str] = None
    location: Optional[str] = None
    functions: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    identity: Dict[str, Any] = field(default_factory=dict)
    status: Dict[str, Any] = field(default_factory=dict)
    raw_data: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_registry_data(cls, data: Dict[str, Any]) -> "DeviceInfo":
        """Create DeviceInfo from registry discovery response."""
        capabilities = data.get("capabilities", {})
        identity = data.get("identity", {})
        status = data.get("status", {})

        return cls(
            device_id=data.get("device_id", "unknown"),
            device_type=identity.get("device_type"),
            location=status.get("location"),
            functions=capabilities.get("functions", []),
            events=capabilities.get("events", []),
            identity=identity,
            status=status,
            raw_data=data,
        )


class DeviceDiscoveryClient:
    """Client for discovering Device Connect devices and their capabilities.

    Uses JSON-RPC over NATS to query the device registry.

    Example:
        discovery = DeviceDiscoveryClient(messaging_client, tenant="default")
        devices = await discovery.list_devices()
        tools = await discovery.get_tools()
    """

    def __init__(
        self,
        messaging_client: MessagingClient,
        tenant: str = "default",
        cache_ttl: float = 30.0,
    ):
        """Initialize discovery client.

        Args:
            messaging_client: Connected messaging client (NATS)
            tenant: Device Connect tenant name
            cache_ttl: Cache TTL in seconds (0 to disable caching)
        """
        self._client = messaging_client
        self._tenant = tenant
        self._cache_ttl = cache_ttl
        self._cache: Optional[List[Dict[str, Any]]] = None
        self._cache_time: float = 0

    async def list_devices(self, use_cache: bool = True) -> List[DeviceInfo]:
        """Fetch all registered devices from the registry.

        Args:
            use_cache: Whether to use cached results (if available and fresh)

        Returns:
            List of DeviceInfo objects
        """
        raw_devices = await self._fetch_devices(use_cache)
        return [DeviceInfo.from_registry_data(d) for d in raw_devices]

    async def get_tools(self, use_cache: bool = True) -> List[MCPToolDefinition]:
        """Get all device functions as MCP tool definitions.

        Args:
            use_cache: Whether to use cached device data

        Returns:
            List of MCP tool definitions
        """
        raw_devices = await self._fetch_devices(use_cache)
        return devices_to_mcp_tools(raw_devices)

    async def _fetch_devices(self, use_cache: bool = True) -> List[Dict[str, Any]]:
        """Fetch raw device data from registry.

        Args:
            use_cache: Whether to use cached results

        Returns:
            List of raw device dictionaries
        """
        # Check cache
        if use_cache and self._cache is not None:
            age = time.time() - self._cache_time
            if age < self._cache_ttl:
                logger.debug(f"Using cached device list (age: {age:.1f}s)")
                return self._cache

        # Query registry
        logger.debug(f"Querying device registry: device-connect.{self._tenant}.discovery")

        request = {
            "jsonrpc": "2.0",
            "method": "discovery/listDevices",
            "id": str(uuid.uuid4()),
        }

        try:
            response = await self._client.request(
                f"device-connect.{self._tenant}.discovery",
                json.dumps(request).encode(),
                timeout=5.0,
            )

            result = json.loads(response.decode())

            if "error" in result:
                error = result["error"]
                logger.error(f"Discovery error: {error}")
                raise DiscoveryError(f"Registry error: {error.get('message', error)}")

            devices = result.get("result", {}).get("devices", [])
            logger.info(f"Discovered {len(devices)} devices")

            # Update cache
            self._cache = devices
            self._cache_time = time.time()

            return devices

        except Exception as e:
            if isinstance(e, DiscoveryError):
                raise
            logger.error(f"Discovery request failed: {e}")
            raise DiscoveryError(f"Failed to query registry: {e}") from e

    def invalidate_cache(self) -> None:
        """Force cache invalidation."""
        self._cache = None
        self._cache_time = 0


class DiscoveryError(Exception):
    """Raised when device discovery fails."""

    pass
