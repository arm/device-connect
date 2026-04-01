"""DeviceConnectMCP - FastMCP-compatible API for Device Connect devices.

Provides a simple, decorator-based interface for building Device Connect devices
that is compatible with the FastMCP API pattern.

Simple example:
    from device_connect_agent_tools.mcp import DeviceConnectMCP

    mcp = DeviceConnectMCP("my-sensor-001")

    @mcp.tool()
    async def read_temperature() -> dict:
        '''Read current temperature.'''
        return {"celsius": 22.5}

    await mcp.run()

Advanced example with events:
    mcp = DeviceConnectMCP(
        "robot-cleaner-001",
        device_type="cleaning_robot",
        manufacturer="Acme",
        location="warehouse-A",
    )

    @mcp.tool()
    async def start_cleaning(zone: str = "all") -> dict:
        '''Start cleaning in the specified zone.'''
        return {"status": "started", "zone": zone}

    @mcp.event()
    async def cleaning_complete(zone: str, duration_seconds: int):
        '''Emitted when cleaning is complete.'''
        pass

    await mcp.run()
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import os
from typing import Any, Callable, Dict, List, Optional, TypeVar

from device_connect_edge.device import DeviceRuntime
from device_connect_edge.drivers.base import DeviceDriver
from device_connect_edge.drivers.decorators import (
    build_function_schema,
    build_event_schema,
    _parse_docstring,
)
from device_connect_edge.types import (
    DeviceCapabilities,
    DeviceIdentity,
    DeviceStatus,
    FunctionDef,
    EventDef,
)

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class DeviceConnectMCP:
    """FastMCP-compatible API for Device Connect devices.

    Provides decorators (@tool, @event) for defining device capabilities
    and handles connection, registration, and command handling via NATS.

    Attributes:
        device_id: Unique device identifier
        device_type: Device type (e.g., "sensor", "camera", "robot")
        manufacturer: Device manufacturer
        model: Device model
        location: Device location
        description: Human-readable device description
    """

    def __init__(
        self,
        device_id: str,
        *,
        device_type: str = "generic",
        manufacturer: Optional[str] = None,
        model: Optional[str] = None,
        location: Optional[str] = None,
        description: Optional[str] = None,
        # Messaging configuration
        messaging_urls: Optional[List[str]] = None,
        credentials_file: Optional[str] = None,
        tls_ca_file: Optional[str] = None,
        tenant: str = "default",
        ttl: int = 15,
        allow_insecure: Optional[bool] = None,
    ):
        """Initialize DeviceConnectMCP instance.

        Args:
            device_id: Unique device identifier
            device_type: Device type for categorization
            manufacturer: Device manufacturer name
            model: Device model name
            location: Physical/logical location
            description: Human-readable description
            messaging_urls: NATS server URLs (or from NATS_URL env)
            credentials_file: Path to .creds.json file
            tls_ca_file: Path to TLS CA certificate
            tenant: Device Connect tenant (default: "default")
            ttl: Registration TTL in seconds
            allow_insecure: Allow connection without TLS/credentials (or from DEVICE_CONNECT_ALLOW_INSECURE env)
        """
        self.device_id = device_id
        self.device_type = device_type
        self.manufacturer = manufacturer
        self.model = model
        self.location = location
        self.description = description
        self.tenant = tenant
        self.ttl = ttl

        # Messaging configuration
        self._messaging_urls = messaging_urls
        self._credentials_file = credentials_file
        self._tls_ca_file = tls_ca_file
        self._allow_insecure = allow_insecure

        # Registered tools and events
        self._tools: Dict[str, Callable] = {}
        self._events: Dict[str, Callable] = {}
        self._tool_metadata: Dict[str, Dict[str, Any]] = {}
        self._event_metadata: Dict[str, Dict[str, Any]] = {}

        # Runtime state
        self._device: Optional[DeviceRuntime] = None
        self._driver: Optional[_DefaultDriver] = None
        self._running = False

    def tool(
        self,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Callable[[F], F]:
        """Decorator to register a function as a device tool.

        The decorated function will be callable via MCP and Device Connect.
        Function metadata (name, description, parameters) is extracted
        from the function signature, type hints, and docstring.

        Args:
            name: Override function name (default: function __name__)
            description: Override description (default: first line of docstring)

        Returns:
            Decorator function

        Example:
            @mcp.tool()
            async def capture_image(resolution: str = "1080p") -> dict:
                '''Capture an image from the camera.

                Args:
                    resolution: Image resolution (720p, 1080p, 4k)
                '''
                return {"image_b64": "..."}
        """
        def decorator(func: F) -> F:
            tool_name = name or func.__name__

            # Parse docstring
            summary, arg_docs = _parse_docstring(func.__doc__)
            tool_desc = description or summary

            # Build JSON Schema from type hints
            schema = build_function_schema(func)

            # Store metadata
            self._tool_metadata[tool_name] = {
                "name": tool_name,
                "description": tool_desc,
                "parameters": schema,
                "arg_descriptions": arg_docs,
            }

            # Wrap function to add logging
            @functools.wraps(func)
            async def wrapper(*args, **kwargs):
                logger.debug(f"Tool called: {tool_name}")
                try:
                    # Handle both sync and async functions
                    if asyncio.iscoroutinefunction(func):
                        result = await func(*args, **kwargs)
                    else:
                        result = func(*args, **kwargs)
                    logger.debug(f"Tool result: {tool_name} -> OK")
                    return result
                except Exception as e:
                    logger.error(f"Tool error: {tool_name} -> {e}")
                    raise

            # Mark as tool
            wrapper._is_tool = True
            wrapper._tool_name = tool_name
            wrapper._tool_description = tool_desc
            wrapper._tool_schema = schema
            wrapper._original_func = func

            # Register
            self._tools[tool_name] = wrapper

            return wrapper  # type: ignore

        return decorator

    def event(
        self,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Callable[[F], F]:
        """Decorator to register an event emitter.

        The decorated function defines the event schema. When called,
        it emits the event via the Device Connect messaging layer.

        Args:
            name: Override event name (default: function __name__)
            description: Override description (default: first line of docstring)

        Returns:
            Decorator function

        Example:
            @mcp.event()
            async def motion_detected(zone: str, confidence: float):
                '''Motion detected in camera view.

                Args:
                    zone: Zone identifier
                    confidence: Detection confidence (0.0 to 1.0)
                '''
                pass  # Optional pre-processing
        """
        def decorator(func: F) -> F:
            event_name = name or func.__name__

            # Parse docstring
            summary, arg_docs = _parse_docstring(func.__doc__)
            event_desc = description or summary

            # Build event schema
            schema = build_event_schema(func)

            # Store metadata
            self._event_metadata[event_name] = {
                "name": event_name,
                "description": event_desc,
                "payload_schema": schema,
            }

            # Wrap to emit event
            @functools.wraps(func)
            async def wrapper(*args, **kwargs):
                # Build payload from arguments
                sig = inspect.signature(func)
                params = [p for p in sig.parameters.keys() if p != "self"]
                payload = {}
                for i, arg in enumerate(args):
                    if i < len(params):
                        payload[params[i]] = arg
                payload.update(kwargs)

                # Run original function (pre-processing)
                if asyncio.iscoroutinefunction(func):
                    await func(*args, **kwargs)
                else:
                    func(*args, **kwargs)

                # Emit event
                await self.emit(event_name, payload)

            # Mark as event
            wrapper._is_event = True
            wrapper._event_name = event_name
            wrapper._event_description = event_desc
            wrapper._event_schema = schema
            wrapper._original_func = func

            # Register
            self._events[event_name] = wrapper

            return wrapper  # type: ignore

        return decorator

    async def emit(self, event_name: str, payload: Dict[str, Any]) -> None:
        """Emit an event.

        Args:
            event_name: Name of the event
            payload: Event payload
        """
        if self._device:
            await self._device.enqueue_event(f"event/{event_name}", payload)
        else:
            logger.warning(f"Cannot emit event '{event_name}': device not running")

    async def run(self) -> None:
        """Start the device: connect to NATS, register, and listen for commands.

        This method blocks until the device is stopped (via stop() or Ctrl+C).
        """
        if self._running:
            logger.warning("DeviceConnectMCP already running")
            return

        self._running = True
        logger.info(f"Starting DeviceConnectMCP device: {self.device_id}")

        # Create internal driver
        self._driver = _DefaultDriver(self)

        # Resolve messaging configuration
        messaging_urls = self._messaging_urls
        credentials_file = self._credentials_file
        tls_ca_file = self._tls_ca_file

        # Fall back to environment
        if not messaging_urls:
            url = os.getenv("NATS_URL", "nats://localhost:4222")
            messaging_urls = [u.strip() for u in url.split(",")]

        if not credentials_file:
            credentials_file = os.getenv("NATS_CREDENTIALS_FILE")

        if not tls_ca_file:
            tls_ca_file = os.getenv("NATS_TLS_CA_FILE")

        # Build DeviceRuntime kwargs
        device_kwargs: Dict[str, Any] = {
            "driver": self._driver,
            "device_id": self.device_id,
            "tenant": self.tenant,
            "ttl": self.ttl,
            "allow_insecure": self._allow_insecure,
        }

        # Add messaging config based on credentials file type
        if credentials_file and credentials_file.endswith(".creds.json"):
            device_kwargs["nats_credentials_file"] = credentials_file
            # Override TLS config if explicitly provided (credentials file may have Docker paths)
            if tls_ca_file:
                device_kwargs["messaging_tls"] = {"ca_file": tls_ca_file}
            if messaging_urls:
                device_kwargs["messaging_urls"] = messaging_urls
        else:
            device_kwargs["messaging_urls"] = messaging_urls
            if credentials_file:
                device_kwargs["nats_credentials_file"] = credentials_file
            if tls_ca_file:
                device_kwargs["messaging_tls"] = {"ca_file": tls_ca_file}

        # Create and run device
        self._device = DeviceRuntime(**device_kwargs)

        try:
            await self._device.run()
        finally:
            self._running = False
            self._device = None

    async def stop(self) -> None:
        """Stop the device gracefully."""
        if self._device:
            await self._device.stop()
        self._running = False

    @property
    def is_running(self) -> bool:
        """Check if device is running."""
        return self._running

    def get_capabilities(self) -> DeviceCapabilities:
        """Get device capabilities (functions and events)."""
        functions = []
        for name, meta in self._tool_metadata.items():
            functions.append(FunctionDef(
                name=meta["name"],
                description=meta["description"],
                parameters=meta["parameters"],
            ))

        events = []
        for name, meta in self._event_metadata.items():
            events.append(EventDef(
                name=f"event/{meta['name']}",
                description=meta["description"],
                payload_schema=meta.get("payload_schema"),
            ))

        return DeviceCapabilities(
            description=self.description or f"{self.device_type} device",
            functions=functions,
            events=events,
        )


class _DefaultDriver(DeviceDriver):
    """Internal driver that wraps DeviceConnectMCP decorated functions.

    This driver is automatically created by DeviceConnectMCP.run() and provides
    the DeviceDriver interface expected by DeviceRuntime.
    """

    device_type = "device_connect_mcp"

    def __init__(self, mcp: DeviceConnectMCP):
        super().__init__()  # Initialize base class (routines, internal handlers)
        self._mcp = mcp
        # Set device_type from MCP instance
        self.device_type = mcp.device_type

    @property
    def identity(self) -> DeviceIdentity:
        """Return device identity from MCP configuration."""
        return DeviceIdentity(
            device_type=self._mcp.device_type,
            manufacturer=self._mcp.manufacturer,
            model=self._mcp.model,
            description=self._mcp.description,
        )

    @property
    def status(self) -> DeviceStatus:
        """Return device status from MCP configuration."""
        return DeviceStatus(
            location=self._mcp.location,
            availability="idle",
        )

    @property
    def capabilities(self) -> DeviceCapabilities:
        """Return capabilities from registered tools and events."""
        return self._mcp.get_capabilities()

    @property
    def functions(self) -> List[FunctionDef]:
        """Return registered functions."""
        return self.capabilities.functions

    @property
    def events(self) -> List[EventDef]:
        """Return registered events."""
        return self.capabilities.events

    async def connect(self) -> None:
        """No-op for default driver."""
        pass

    async def disconnect(self) -> None:
        """No-op for default driver."""
        pass

    async def invoke(self, function_name: str, **params: Any) -> Any:
        """Invoke a registered tool by name."""
        if function_name not in self._mcp._tools:
            raise ValueError(f"Unknown function: {function_name}")

        func = self._mcp._tools[function_name]
        return await func(**params)

    def _get_functions(self) -> Dict[str, Callable]:
        """Return function map for DeviceRuntime command handling."""
        return self._mcp._tools
