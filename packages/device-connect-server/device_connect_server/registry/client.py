"""Registry client for querying registered devices.

This module provides RegistryClient, which communicates with the
device registry service over the messaging layer using JSON-RPC.

Example:
    from device_connect_server.registry import RegistryClient
    from device_connect_server.messaging import create_client
    from device_connect_server.messaging.config import MessagingConfig

    config = MessagingConfig()
    messaging = create_client(config.backend)

    async with RegistryClient(messaging, config) as registry:
        # List all devices
        devices = await registry.list_devices()

        # Get specific device
        camera = await registry.get_device("camera-001")

        # Filter devices
        cameras = await registry.list_devices(device_type="camera")
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from device_connect_server.messaging import MessagingClient
from device_connect_server.messaging.config import MessagingConfig


class RegistryClient:
    """Client for querying the device registry.

    Communicates with the device registry service via JSON-RPC over
    the messaging layer. Supports listing devices, getting specific
    devices, and filtering by various criteria.

    Args:
        messaging_client: MessagingClient instance for communication
        config: MessagingConfig with connection settings
        tenant: Tenant namespace (default: "default")
        timeout: Request timeout in seconds (default: 5.0)

    Example:
        messaging = create_client("nats")
        config = MessagingConfig()

        async with RegistryClient(messaging, config) as registry:
            devices = await registry.list_devices()

        # Or without context manager:
        registry = RegistryClient(messaging, config)
        await registry.connect()
        devices = await registry.list_devices()
        await registry.close()
    """

    def __init__(
        self,
        messaging_client: MessagingClient,
        config: Optional[MessagingConfig] = None,
        tenant: str = "default",
        timeout: float = 5.0,
    ):
        """Initialize the registry client.

        Args:
            messaging_client: MessagingClient instance for communication
            config: MessagingConfig with connection settings (optional if already connected)
            tenant: Tenant namespace
            timeout: Default request timeout in seconds
        """
        self._messaging = messaging_client
        self._config = config or MessagingConfig()
        self._tenant = tenant
        self._timeout = timeout
        self._logger = logging.getLogger(f"{__name__}.RegistryClient")
        self._connected = False

    async def connect(self) -> None:
        """Connect to the messaging broker.

        Only needed if the messaging client is not already connected.
        """
        if self._messaging.is_connected:
            self._connected = True
            return

        await self._messaging.connect(
            servers=self._config.servers,
            credentials=self._config.credentials,
            tls_config=self._config.tls_config,
        )
        self._connected = True
        self._logger.info("Connected to messaging broker")

    async def close(self) -> None:
        """Close the messaging connection."""
        if self._connected and self._messaging.is_connected:
            await self._messaging.close()
            self._connected = False
            self._logger.info("Disconnected from messaging broker")

    async def __aenter__(self) -> "RegistryClient":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()

    async def _request(
        self,
        subject: str,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Send a JSON-RPC request and return the result.

        Args:
            subject: Messaging subject to send to
            method: JSON-RPC method name
            params: Method parameters
            timeout: Request timeout (default: instance timeout)

        Returns:
            The 'result' field from the JSON-RPC response

        Raises:
            RuntimeError: If the response contains an error
            TimeoutError: If the request times out
        """
        timeout = timeout or self._timeout
        req_id = f"reg-{int(time.time() * 1000)}"

        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params:
            payload["params"] = params

        self._logger.debug("Sending request: %s -> %s", method, subject)

        response_data = await self._messaging.request(
            subject,
            json.dumps(payload).encode(),
            timeout=timeout,
        )

        response = json.loads(response_data.decode())

        if "error" in response:
            error = response["error"]
            msg = error.get("message", "Unknown error")
            code = error.get("code", -1)
            raise RuntimeError(f"Registry error ({code}): {msg}")

        return response.get("result")

    async def list_devices(
        self,
        *,
        device_type: Optional[str] = None,
        location: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
        timeout: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """List all registered devices.

        Args:
            device_type: Filter by device type (e.g., "camera", "robot")
            location: Filter by location
            capabilities: Filter by required capabilities
            timeout: Request timeout

        Returns:
            List of device dictionaries with full registration data

        Example:
            # Get all devices
            devices = await registry.list_devices()

            # Get only cameras
            cameras = await registry.list_devices(device_type="camera")
        """
        subject = f"device-connect.{self._tenant}.discovery"

        params: Dict[str, Any] = {}
        if device_type:
            params["device_type"] = device_type
        if location:
            params["location"] = location
        if capabilities:
            params["capabilities"] = capabilities

        result = await self._request(
            subject,
            "discovery/listDevices",
            params if params else None,
            timeout,
        )

        devices = result.get("devices", [])
        self._logger.debug("Listed %d devices", len(devices))
        return devices

    async def get_device(
        self,
        device_id: str,
        timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get a specific device by ID.

        Args:
            device_id: Device identifier
            timeout: Request timeout

        Returns:
            Device dictionary or None if not found

        Example:
            camera = await registry.get_device("camera-001")
            if camera:
                print(f"Found: {camera['device_id']}")
        """
        # Query all devices and filter
        # TODO: Add dedicated getDevice RPC method to registry service
        devices = await self.list_devices(timeout=timeout)

        for device in devices:
            if device.get("device_id") == device_id:
                return device

        return None

    async def get_device_functions(
        self,
        device_id: str,
        timeout: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Get functions exposed by a device.

        Args:
            device_id: Device identifier
            timeout: Request timeout

        Returns:
            List of function definitions

        Example:
            functions = await registry.get_device_functions("camera-001")
            for fn in functions:
                print(f"{fn['name']}: {fn['description']}")
        """
        device = await self.get_device(device_id, timeout)
        if not device:
            return []

        # Functions can be in base or static depending on registration format
        base = device.get("base", {})
        static = device.get("static", {})

        functions = base.get("functions", []) or static.get("functions", [])
        return functions

    async def get_device_events(
        self,
        device_id: str,
        timeout: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Get events emitted by a device.

        Args:
            device_id: Device identifier
            timeout: Request timeout

        Returns:
            List of event definitions

        Example:
            events = await registry.get_device_events("camera-001")
            for ev in events:
                print(f"{ev['name']}: {ev['description']}")
        """
        device = await self.get_device(device_id, timeout)
        if not device:
            return []

        # Events can be in base or static depending on registration format
        base = device.get("base", {})
        static = device.get("static", {})

        events = base.get("events", []) or static.get("events", [])
        return events

    async def wait_for_device(
        self,
        device_id: str,
        timeout: float = 30.0,
        poll_interval: float = 1.0,
    ) -> Optional[Dict[str, Any]]:
        """Wait for a device to become available.

        Polls the registry until the device appears or timeout is reached.

        Args:
            device_id: Device identifier to wait for
            timeout: Maximum wait time in seconds
            poll_interval: Time between polls in seconds

        Returns:
            Device dictionary or None if timeout

        Example:
            # Wait for camera to come online
            camera = await registry.wait_for_device("camera-001", timeout=60)
            if camera:
                print("Camera is online!")
        """
        import asyncio

        start = time.time()
        while time.time() - start < timeout:
            device = await self.get_device(device_id, timeout=poll_interval)
            if device:
                return device
            await asyncio.sleep(poll_interval)

        return None
