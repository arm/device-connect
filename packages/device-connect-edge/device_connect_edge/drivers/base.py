"""Device driver abstract base class with D2D communication.

This module defines the DeviceDriver ABC, which provides the foundation
for implementing device-specific logic. Device drivers define what
functions a device exposes and how to invoke them.

All devices have D2D (device-to-device) capabilities built in:
    - invoke_remote(): Call functions on other devices
    - list_devices(): Query available devices from registry
    - @on: Subscribe to events from other devices

Relationship to DeviceRuntime:
    - DeviceRuntime = runtime (messaging, registration, heartbeats)
    - DeviceDriver = logic (what functions exist, how to call hardware)

DeviceRuntime uses a DeviceDriver to know what to expose and how to
handle incoming commands.

Example:
    class CameraDriver(DeviceDriver):
        device_type = "camera"

        @rpc()
        async def capture_image(self, resolution: str = "1080p") -> dict:
            '''Capture an image from the camera.'''
            image = await self._capture(resolution)
            return {"image_b64": image}

        @emit()
        async def motion_detected(self, zone: str, confidence: float):
            '''Motion detected in camera view.'''
            pass  # Optional pre-processing

        @before_emit("motion_detected")
        async def on_motion(self, zone: str, confidence: float, **kwargs):
            '''React locally before pubsub emission.'''
            if confidence > 0.9:
                await self.alert_security(zone)

        async def detection_loop(self):
            # Calling the decorated method emits the event
            await self.motion_detected(zone="A", confidence=0.95)

        # D2D: Subscribe to events from other devices
        @on(device_type="robot", event_name="cleaning_complete")
        async def on_cleaning_done(self, device_id: str, event_name: str, payload: dict):
            '''React to robot completion events.'''
            await self.verify_zone(payload.get("zone"))

        # D2D: Call other devices
        async def dispatch_robot(self, zone: str):
            robots = await self.list_devices(device_type="robot")
            if robots:
                await self.invoke_remote(robots[0]["device_id"], "start_cleaning", zone=zone)

        async def connect(self) -> None:
            self._camera = await CameraSDK.connect(self._url)

        async def disconnect(self) -> None:
            await self._camera.close()
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

from device_connect_edge.types import (
    FunctionDef,
    EventDef,
    DeviceCapabilities,
    DeviceIdentity,
    DeviceStatus,
)
from device_connect_edge.drivers.decorators import build_function_schema, build_event_schema
from device_connect_edge.telemetry.tracer import get_tracer, get_current_trace_id, SpanKind, StatusCode
from device_connect_edge.telemetry.metrics import get_metrics
from device_connect_edge.telemetry.propagation import inject_into_meta

if TYPE_CHECKING:
    from device_connect_edge.device import DeviceRuntime, _RemoteInvoker as DeviceRouter
    from device_connect_edge.discovery_provider import DiscoveryProvider


logger = logging.getLogger("device_connect.drivers.base")


class DeviceDriver(ABC):
    """Abstract base class for device drivers with D2D communication.

    For most use cases, use @rpc decorators instead of
    implementing invoke() directly. The ABC scans for decorated
    methods and auto-generates the capabilities.

    All devices have D2D (device-to-device) capabilities built in:
        - invoke_remote(): Call functions on other devices
        - list_devices(): Query available devices from registry
        - get_device(): Get a specific device by ID
        - @on decorator: Subscribe to events from other devices

    Subclasses must:
        1. Set the `device_type` class attribute
        2. Implement `connect()` and `disconnect()` methods
        3. Decorate functions with @rpc
        4. Optionally decorate event emitters with @emit
        5. Optionally use @on to subscribe to other devices' events

    The driver is associated with a DeviceRuntime via `set_device()`,
    which provides access to event emission and D2D infrastructure.

    Attributes:
        device_type: String identifying the device type (e.g., "camera")
        router: DeviceRouter for invoking functions on other devices
        registry: RegistryClient for querying available devices
    """

    # Override this class attribute in subclasses
    device_type: str = "unknown"

    # Override in subclasses to gate startup until these device types are present.
    # DeviceRuntime.run() will call wait_for_device() for each type before
    # starting background tasks. Example: depends_on = ("robot", "speaker")
    depends_on: Tuple[str, ...] = ()

    # Type alias for event callback
    EventCallback = Callable[[str, Dict[str, Any]], Any]

    def __init__(self):
        """Initialize the driver."""
        self._device: Optional[DeviceRuntime] = None
        self._functions_cache: Optional[List[FunctionDef]] = None
        self._events_cache: Optional[List[EventDef]] = None
        self._event_callback: Optional[Callable[[str, Dict[str, Any]], Any]] = None
        self._function_methods: Optional[Dict[str, Callable]] = None

        # Internal event handlers (@before_emit decorated methods)
        self._internal_handlers: Dict[str, List[Callable]] = {}
        self._internal_handlers_collected = False

        # Device routines (@periodic decorated methods)
        self._routines: Dict[str, Dict[str, Any]] = {}  # name -> config
        self._routine_tasks: Dict[str, asyncio.Task] = {}  # name -> task
        self._routines_collected = False

        # D2D: Router and registry (set by DeviceRuntime)
        self._router: Optional[DeviceRouter] = None
        self._registry: Optional[DiscoveryProvider] = None

        # D2D: Event subscriptions (@on decorated methods)
        self._subscriptions: List[Any] = []
        self._subscription_tasks: List[asyncio.Task] = []

        # Device identity (set by DeviceRuntime via _setup_agentic_driver)
        self._device_id: Optional[str] = None

        # Raw transport for hardware-native topic access
        self._transport: Optional["DriverTransport"] = None  # noqa: F821

    def set_device(self, device: DeviceRuntime) -> None:
        """Associate this driver with a DeviceRuntime.

        Called by DeviceRuntime during initialization to provide
        access to device-level operations like event emission.

        Args:
            device: The DeviceRuntime instance using this driver
        """
        self._device = device

    def set_event_callback(
        self,
        callback: Callable[[str, Dict[str, Any]], Any]
    ) -> None:
        """Set the callback for event emission.

        Called by DeviceRuntime to wire up event handling. Events
        emitted via emit_event() will be forwarded to this callback.

        Args:
            callback: Async function(event_name, payload)
        """
        self._event_callback = callback

    @property
    def transport(self) -> Optional["DriverTransport"]:  # noqa: F821
        """Raw messaging transport for hardware-native topics.

        Returns ``None`` before the driver is associated with a
        ``DeviceRuntime`` or before messaging is connected.  After
        that, returns a lazily-created ``DriverTransport`` that wraps
        the runtime's ``MessagingClient``.
        """
        if self._transport is not None:
            return self._transport
        if self._device is None or self._device.messaging is None:
            return None
        from device_connect_edge.drivers.transport import DriverTransport
        self._transport = DriverTransport(self._device.messaging)
        return self._transport

    def _get_functions(self) -> Dict[str, Callable]:
        """Get mapping of function names to methods.

        Used by DeviceRuntime for command dispatch.

        Returns:
            Dict mapping function names to callable methods
        """
        if self._function_methods is not None:
            return self._function_methods

        self._function_methods = {}
        for attr_name in dir(self):
            if attr_name.startswith("_"):
                continue

            if attr_name in self._SKIP_ATTRS:
                continue

            attr = getattr(self, attr_name, None)
            if attr is None or not callable(attr):
                continue

            if not getattr(attr, "_is_device_function", False):
                continue

            func_name = getattr(attr, "_function_name", attr_name)
            self._function_methods[func_name] = attr

        return self._function_methods

    @property
    def capabilities(self) -> DeviceCapabilities:
        """Get device capabilities (functions and events).

        Auto-generated from @rpc and @emit
        decorated methods.

        Returns:
            DeviceCapabilities with functions and events
        """
        return DeviceCapabilities(
            description=self.__class__.__doc__ or "",
            functions=self.functions,
            events=self.events
        )

    @property
    def functions(self) -> List[FunctionDef]:
        """Get list of functions from @rpc decorated methods.

        Returns:
            List of FunctionDef describing each function
        """
        if self._functions_cache is not None:
            return self._functions_cache

        self._functions_cache = self._collect_functions()
        return self._functions_cache

    @property
    def events(self) -> List[EventDef]:
        """Get list of events from @emit decorated methods.

        Returns:
            List of EventDef describing each event
        """
        if self._events_cache is not None:
            return self._events_cache

        self._events_cache = self._collect_events()
        return self._events_cache

    def _invalidate_caches(self) -> None:
        """Reset cached function/event lists so they are rebuilt on next access."""
        self._functions_cache = None
        self._events_cache = None
        self._function_methods = None

    @property
    def identity(self) -> DeviceIdentity:
        """Get static device identity.

        Override in subclasses to provide device-specific identity.

        Returns:
            DeviceIdentity with manufacturer, model, etc.
        """
        return DeviceIdentity(device_type=self.device_type)

    @property
    def status(self) -> DeviceStatus:
        """Get current device status.

        Override in subclasses to provide initial/default status
        like location. For dynamic fields (busy_score), use
        set_heartbeat_provider() on DeviceRuntime.

        Returns:
            DeviceStatus with current state
        """
        return DeviceStatus()

    async def connect(self) -> None:
        """Initialize connection to hardware.

        Called by DeviceRuntime before starting the main run loop.
        Override to establish connections to cameras, sensors,
        actuators, or other hardware.

        Raises:
            MessagingConnectionError: If connection fails
        """

    async def disconnect(self) -> None:
        """Cleanup connection to hardware.

        Called by DeviceRuntime during shutdown. Override to
        gracefully close connections and release resources.
        """

    async def invoke(self, function_name: str, **params: Any) -> Any:
        """Invoke a device function by name.

        Default implementation routes to @rpc decorated
        methods. Override only if you need custom routing logic.

        Args:
            function_name: Name of the function to invoke
            **params: Function parameters

        Returns:
            Function result (usually a dict)

        Raises:
            FunctionInvocationError: If function not found or fails
        """
        return await self._invoke_decorated(function_name, **params)

    # Properties to skip during attribute scanning to avoid recursion
    _SKIP_ATTRS = frozenset([
        "capabilities", "functions", "events", "identity", "status",
        "device_type"
    ])

    def _collect_functions(self) -> List[FunctionDef]:
        """Scan for @rpc decorated methods.

        Returns:
            List of FunctionDef for each decorated method
        """
        functions = []

        for attr_name in dir(self):
            if attr_name.startswith("_"):
                continue

            if attr_name in self._SKIP_ATTRS:
                continue

            attr = getattr(self, attr_name, None)
            if attr is None:
                continue

            if not callable(attr):
                continue

            if not getattr(attr, "_is_device_function", False):
                continue

            # Build function definition
            func_name = getattr(attr, "_function_name", attr_name)
            description = getattr(attr, "_description", "")
            parameters = build_function_schema(attr)

            functions.append(FunctionDef(
                name=func_name,
                description=description,
                parameters=parameters,
                tags=[]
            ))

        return functions

    def _collect_events(self) -> List[EventDef]:
        """Scan for @emit decorated methods.

        Returns:
            List of EventDef for each decorated method
        """
        events = []

        for attr_name in dir(self):
            if attr_name.startswith("_"):
                continue

            if attr_name in self._SKIP_ATTRS:
                continue

            attr = getattr(self, attr_name, None)
            if attr is None:
                continue

            if not callable(attr):
                continue

            if not getattr(attr, "_is_device_event", False):
                continue

            # Build event definition
            event_name = getattr(attr, "_event_name", attr_name)
            description = getattr(attr, "_event_description", "")
            payload_schema = build_event_schema(attr)

            events.append(EventDef(
                name=event_name,
                description=description,
                payload_schema=payload_schema,
                tags=[]
            ))

        return events

    async def _invoke_decorated(self, function_name: str, **params: Any) -> Any:
        """Invoke a decorated method by function name.

        Args:
            function_name: The function name to invoke
            **params: Parameters to pass to the function

        Returns:
            Result from the function

        Raises:
            FunctionInvocationError: If function not found
        """
        from device_connect_edge.errors import FunctionInvocationError

        funcs = self._get_functions()
        method = funcs.get(function_name)
        if method is None:
            raise FunctionInvocationError(
                f"Function '{function_name}' not found",
                function_name=function_name
            )
        try:
            return await method(**params)
        except Exception as e:
            raise FunctionInvocationError(
                f"Error invoking {function_name}: {e}",
                function_name=function_name,
                original_error=e
            ) from e

    async def _emit_event_internal(self, event_name: str, payload: Dict[str, Any]) -> None:
        """Internal event emission - called by @emit decorator.

        Uses the event callback if set (by DeviceRuntime), or falls back
        to direct device emission if associated with a DeviceRuntime.

        Args:
            event_name: Event name (e.g., 'event/objectDetected')
            payload: Event payload dictionary

        Raises:
            RuntimeError: If neither callback nor device is available
        """
        if self._event_callback is not None:
            # Use callback (async call)
            import asyncio
            result = self._event_callback(event_name, payload)
            if asyncio.iscoroutine(result):
                await result
        elif self._device is not None:
            # Fall back to direct device emission
            await self._device.enqueue_event(event_name, payload)
        else:
            raise RuntimeError(
                "Driver not associated with a DeviceRuntime. "
                "Call set_device() or set_event_callback() first."
            )

    async def emit_event(self, event_name: str, payload: Dict[str, Any]) -> None:
        """Emit an event.

        DEPRECATED: Use @emit decorated methods instead.

        Example migration:
            # Old (deprecated):
            await self.emit_event("my_event", {"value": 42})

            # New (recommended):
            @emit()
            async def my_event(self, value: int):
                pass

            await self.my_event(value=42)

        Args:
            event_name: Event name (e.g., 'event/objectDetected')
            payload: Event payload dictionary

        Raises:
            RuntimeError: If neither callback nor device is available
        """
        import warnings
        warnings.warn(
            "emit_event() is deprecated. Use @emit decorated methods instead."
            "See DeviceDriver docstring for migration example.",
            DeprecationWarning,
            stacklevel=2
        )
        await self._emit_event_internal(event_name, payload)

    # =========================================================================
    # Internal Event Handlers (@before_emit)
    # =========================================================================

    def _collect_internal_handlers(self) -> None:
        """Collect all @before_emit decorated methods.

        Scans the class for methods decorated with @before_emit and
        organizes them by event name for efficient dispatch.
        """
        if self._internal_handlers_collected:
            return

        for attr_name in dir(self):
            if attr_name.startswith("__"):
                continue

            attr = getattr(self, attr_name, None)
            if attr is None or not callable(attr):
                continue

            if not getattr(attr, "_is_internal_handler", False):
                continue

            event_name = getattr(attr, "_internal_event_name", None)
            if event_name is None:
                continue

            if event_name not in self._internal_handlers:
                self._internal_handlers[event_name] = []
            self._internal_handlers[event_name].append(attr)

        self._internal_handlers_collected = True

    async def _dispatch_internal_event(
        self,
        event_name: str,
        payload: Dict[str, Any]
    ) -> Tuple[bool, Dict[str, Any]]:
        """Dispatch event to internal handlers before pubsub emission.

        Internal handlers are called BEFORE the event is sent to pubsub.
        This allows:
        - Local reaction without network roundtrip
        - Optional suppression of pubsub emission
        - Modification of payload before emission

        Handler return values:
        - None: Propagate with current payload
        - False: Suppress pubsub emission
        - dict: Propagate with modified payload

        Args:
            event_name: Name of the event being emitted
            payload: Original event payload

        Returns:
            Tuple of (should_propagate, final_payload)
        """
        # Ensure handlers are collected
        self._collect_internal_handlers()

        handlers = self._internal_handlers.get(event_name, [])
        if not handlers:
            return True, payload

        should_propagate = True
        current_payload = payload.copy()

        for handler in handlers:
            try:
                # Call handler with payload as kwargs
                result = await handler(**current_payload)

                if result is False:
                    # Handler explicitly suppressed propagation
                    should_propagate = False
                    logger.debug(
                        "Internal handler %s suppressed event %s",
                        handler.__name__, event_name
                    )
                elif isinstance(result, dict):
                    # Handler returned modified payload
                    current_payload = result
                    logger.debug(
                        "Internal handler %s modified payload for %s",
                        handler.__name__, event_name
                    )

                # Check decorator-level suppression
                if getattr(handler, "_suppress_propagation", False):
                    should_propagate = False

            except Exception as e:
                logger.error(
                    "Error in internal handler %s for event %s: %s",
                    handler.__name__, event_name, e
                )
                # Continue with other handlers despite error

        return should_propagate, current_payload

    # =========================================================================
    # Device Routines (@periodic)
    # =========================================================================

    def _collect_routines(self) -> None:
        """Collect all @periodic decorated methods.

        Scans the class for methods decorated with @periodic and
        stores their configuration for lifecycle management.
        """
        if self._routines_collected:
            return

        for attr_name in dir(self):
            if attr_name.startswith("__"):
                continue

            attr = getattr(self, attr_name, None)
            if attr is None or not callable(attr):
                continue

            if not getattr(attr, "_is_device_routine", False):
                continue

            name = getattr(attr, "_routine_name", attr_name)
            self._routines[name] = {
                "func": attr,
                "interval": getattr(attr, "_routine_interval", 1.0),
                "wait_for_completion": getattr(attr, "_routine_wait_for_completion", True),
                "start_on_connect": getattr(attr, "_routine_start_on_connect", True),
            }

        self._routines_collected = True

    async def _start_routines(self) -> None:
        """Start all routines marked with start_on_connect=True.

        Called by DeviceRuntime after driver.connect() completes.
        """
        self._collect_routines()

        for name, config in self._routines.items():
            if config["start_on_connect"]:
                await self.start_routine(name)

    async def _stop_routines(self) -> None:
        """Stop all running routines.

        Called by DeviceRuntime before driver.disconnect().
        """
        for name in list(self._routine_tasks.keys()):
            await self.stop_routine(name)

    async def start_routine(self, name: str) -> None:
        """Start a specific routine by name.

        Args:
            name: Name of the routine to start

        Raises:
            ValueError: If routine name is not found
        """
        if name in self._routine_tasks:
            logger.debug("Routine %s already running", name)
            return

        self._collect_routines()

        config = self._routines.get(name)
        if not config:
            raise ValueError(f"Unknown routine: {name}")

        logger.info("Starting routine: %s (interval=%.1fs)", name, config["interval"])
        self._routine_tasks[name] = asyncio.create_task(
            self._run_routine(name, config)
        )

    async def stop_routine(self, name: str) -> None:
        """Stop a specific routine by name.

        Args:
            name: Name of the routine to stop
        """
        task = self._routine_tasks.pop(name, None)
        if task:
            logger.info("Stopping routine: %s", name)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run_routine(self, name: str, config: Dict[str, Any]) -> None:
        """Run a routine loop until cancelled.

        Args:
            name: Routine name (for logging)
            config: Routine configuration dict
        """
        from device_connect_edge.drivers.decorators import set_call_origin, reset_call_origin

        func = config["func"]
        interval = config["interval"]
        wait_for_completion = config["wait_for_completion"]

        while True:
            start_time = asyncio.get_running_loop().time()

            # Set call origin to "routine" so RPC logs show LOCAL instead of EXEC
            token = set_call_origin("routine")
            try:
                await func()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Routine %s error: %s", name, e)
            finally:
                reset_call_origin(token)

            if wait_for_completion:
                # Wait remaining time (or 0 if routine took longer than interval)
                elapsed = asyncio.get_running_loop().time() - start_time
                sleep_time = max(0, interval - elapsed)
                await asyncio.sleep(sleep_time)
            else:
                # Fixed interval regardless of execution time
                await asyncio.sleep(interval)

    def get_routine_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all routines.

        Returns:
            Dict mapping routine names to status info
        """
        self._collect_routines()

        status = {}
        for name, config in self._routines.items():
            task = self._routine_tasks.get(name)
            status[name] = {
                "interval": config["interval"],
                "wait_for_completion": config["wait_for_completion"],
                "start_on_connect": config["start_on_connect"],
                "running": task is not None and not task.done(),
            }
        return status

    # =========================================================================
    # D2D: Device-to-Device Communication
    # =========================================================================

    @property
    def router(self) -> Optional[DeviceRouter]:
        """Get the device router for remote invocation."""
        return self._router

    @router.setter
    def router(self, value: DeviceRouter) -> None:
        """Set the device router."""
        self._router = value

    @property
    def registry(self) -> Optional[DiscoveryProvider]:
        """Get the registry client for device discovery."""
        return self._registry

    @registry.setter
    def registry(self, value: DiscoveryProvider) -> None:
        """Set the registry client."""
        self._registry = value

    async def invoke_remote(
        self,
        device_id: str,
        function_name: str,
        timeout: Optional[float] = None,
        **params: Any
    ) -> Dict[str, Any]:
        """Invoke a function on another device.

        Sends a JSON-RPC request to the target device and waits for response.

        Args:
            device_id: Target device identifier
            function_name: Function to invoke
            timeout: Optional timeout in seconds
            **params: Function parameters

        Returns:
            Response dict with 'result' or 'error'

        Raises:
            RuntimeError: If router is not configured
            TimeoutError: If request times out

        Example:
            result = await self.invoke_remote(
                "robot-001",
                "start_cleaning",
                zone="A",
                priority="high"
            )
            if "error" in result:
                logger.error(f"Failed: {result['error']}")
            else:
                logger.info(f"Success: {result['result']}")
        """

        if self._router is None:
            raise RuntimeError(
                "Router not configured. Ensure DeviceRuntime has set up D2D infrastructure."
            )

        tracer = get_tracer()
        get_metrics()  # initialize metrics subsystem

        # Get caller device_id for logging
        caller_id = getattr(self, "_device_id", None) or "unknown"
        trace_id_short = get_current_trace_id()[:12]

        with tracer.start_as_current_span(
            f"d2d.invoke/{function_name}",
            kind=SpanKind.CLIENT,
            attributes={
                "device_connect.target_device": device_id,
                "rpc.method": function_name,
                "device_connect.source_device": caller_id,
            },
        ) as span:
            # Build params with W3C trace context in _dc_meta
            params_with_meta = dict(params) if params else {}
            meta = {
                "source_device": caller_id,
                "source_type": "device",
            }
            inject_into_meta(meta)
            params_with_meta["_dc_meta"] = meta

            kwargs = {"params": params_with_meta}
            if timeout is not None:
                kwargs["timeout"] = timeout

            # Summarize params for logging (exclude _dc_meta)
            params_summary = ", ".join(f"{k}={repr(v)[:30]}" for k, v in params.items()) if params else "(none)"
            logger.info("-" * 60)
            logger.info("--> RPC CALL [%s] %s -> %s::%s", trace_id_short, caller_id, device_id, function_name)
            logger.info("    args: %s", params_summary)

            result = await self._router.invoke(device_id, function_name, **kwargs)

            # Log result
            if "error" in result:
                error_msg = result.get("error", {})
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                logger.warning("<-- RPC CALL [%s] %s -> %s::%s -> ERROR: %s", trace_id_short, caller_id, device_id, function_name, error_msg)
                span.set_status(StatusCode.ERROR, str(error_msg))
            else:
                # Summarize result
                res = result.get("result", result)
                if isinstance(res, dict):
                    res_summary = ", ".join(f"{k}={repr(v)[:30]}" for k, v in list(res.items())[:5])
                    if len(res) > 5:
                        res_summary += f"... ({len(res)} keys)"
                else:
                    res_summary = repr(res)[:100]
                logger.info("<-- RPC CALL [%s] %s -> %s::%s -> OK: %s", trace_id_short, caller_id, device_id, function_name, res_summary)
                span.set_status(StatusCode.OK)
            logger.info("-" * 60)

            return result

    async def list_devices(
        self,
        device_type: Optional[str] = None,
        location: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """List available devices from the registry.

        Args:
            device_type: Filter by device type (e.g., "camera", "robot")
            location: Filter by location
            capabilities: Filter by required capabilities

        Returns:
            List of device dictionaries

        Raises:
            RuntimeError: If registry is not configured

        Example:
            # Get all cameras
            cameras = await self.list_devices(device_type="camera")

            # Get robots in a specific zone
            robots = await self.list_devices(
                device_type="robot",
                location="warehouse-A"
            )
        """
        if self._registry is None:
            raise RuntimeError(
                "Registry not configured. Ensure DeviceRuntime has set up D2D infrastructure."
            )

        return await self._registry.list_devices(
            device_type=device_type,
            location=location,
            capabilities=capabilities,
        )

    async def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific device by ID.

        Args:
            device_id: Device identifier

        Returns:
            Device dictionary or None if not found

        Raises:
            RuntimeError: If registry is not configured
        """
        if self._registry is None:
            raise RuntimeError(
                "Registry not configured. Ensure DeviceRuntime has set up D2D infrastructure."
            )

        return await self._registry.get_device(device_id)

    async def wait_for_device(
        self,
        device_type: Optional[str] = None,
        device_id: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Wait for a device to become available, then return its info.

        Works in both D2D mode (polls PresenceCollector) and registry mode
        (polls RegistryClient).  Use this instead of manual retry loops
        around ``list_devices()``.

        Args:
            device_type: Wait for any device of this type.
            device_id: Wait for a specific device by ID.
            timeout: Maximum seconds to wait (default 10).

        Returns:
            Device info dictionary.

        Raises:
            ValueError: If neither *device_type* nor *device_id* is specified.
            RuntimeError: If registry is not configured.
            DeviceDependencyError: If *timeout* expires without finding a match.

        Example::

            robot = await self.wait_for_device(device_type="robot", timeout=15.0)
            await self.invoke_remote(robot["device_id"], "wave")
        """
        if not device_type and not device_id:
            raise ValueError("Must specify device_type or device_id")

        if self._registry is None:
            raise RuntimeError(
                "Registry not configured. Ensure DeviceRuntime has set up D2D infrastructure."
            )

        from device_connect_edge.errors import DeviceDependencyError

        # Fast path: delegate to PresenceCollector if available (D2D mode)
        collector = getattr(self._device, '_d2d_collector', None) if self._device else None
        if collector is not None:
            if device_id:
                result = await collector.wait_for_device_id(device_id, timeout=timeout)
            else:
                result = await collector.wait_for_device_type(device_type, timeout=timeout)
            if result is not None:
                return result
            target = device_id or device_type
            raise DeviceDependencyError(
                f"Device '{target}' not found after {timeout}s",
                device_type=device_type or "",
                timeout=timeout,
            )

        # Registry mode: poll list_devices / get_device
        deadline = time.time() + timeout
        while time.time() < deadline:
            if device_id:
                result = await self._registry.get_device(device_id)
                if result is not None:
                    return result
            else:
                results = await self._registry.list_devices(device_type=device_type)
                if results:
                    return results[0]
            await asyncio.sleep(0.25)

        target = device_id or device_type
        raise DeviceDependencyError(
            f"Device '{target}' not found after {timeout}s",
            device_type=device_type or "",
            timeout=timeout,
        )

    # =========================================================================
    # D2D: Event Subscriptions (@on decorator)
    # =========================================================================

    def _collect_event_subscriptions(self) -> List[Dict[str, Any]]:
        """Collect all @on decorated methods.

        Returns:
            List of subscription definitions
        """
        subscriptions = []

        for attr_name in dir(self):
            if attr_name.startswith("_"):
                continue

            attr = getattr(self, attr_name, None)
            if attr is None or not callable(attr):
                continue

            if not getattr(attr, "_is_event_subscription", False):
                continue

            subscriptions.append({
                "device_id": getattr(attr, "_sub_device_id", None),
                "device_type": getattr(attr, "_sub_device_type", None),
                "event_name": getattr(attr, "_sub_event_name", None),
                "handler": attr,
            })

        return subscriptions

    async def setup_subscriptions(self) -> None:
        """Set up event subscriptions based on @on decorators.

        Called by DeviceRuntime after setting router and registry.
        Subscribes to events from other devices based on decorated methods.
        """
        if self._router is None:
            logger.debug("Cannot setup subscriptions: router not configured")
            return

        subscriptions = self._collect_event_subscriptions()
        if not subscriptions:
            logger.debug("No event subscriptions to set up")
            return

        logger.info("Setting up %d event subscriptions", len(subscriptions))

        for sub in subscriptions:
            try:
                await self._setup_subscription(sub)
            except Exception as e:
                handler = sub.get("handler")
                name = getattr(handler, "__name__", str(handler)) if callable(handler) else str(handler)
                logger.error(
                    "Failed to set up subscription %s (device_type=%s, event=%s): %s",
                    name, sub.get("device_type"), sub.get("event_name"), e,
                )

    async def _setup_subscription(self, sub: Dict[str, Any]) -> None:
        """Set up a single event subscription.

        Args:
            sub: Subscription definition dict
        """
        device_id = sub.get("device_id")
        device_type = sub.get("device_type")
        event_name = sub.get("event_name")
        handler = sub["handler"]

        # Build subscription pattern
        # Pattern: device-connect.{tenant}.{device_pattern}.event.{event_pattern}
        # NOTE: NATS wildcards (*) only work at token boundaries (between dots).
        # We can't do "robot-*" to match "robot-001" - must use "*" and filter.
        if device_id:
            device_pattern = device_id
        else:
            # Use wildcard for all devices, filter by device_type in handler
            device_pattern = "*"

        event_pattern = event_name if event_name else "*"

        # Clean event name (remove "event/" prefix if present)
        if event_pattern.startswith("event/"):
            event_pattern = event_pattern[6:]

        # Get tenant from router
        tenant = getattr(self._router, "_tenant", "default")
        subject = f"device-connect.{tenant}.{device_pattern}.event.{event_pattern}"

        self_id = getattr(self, "_device_id", None) or "unknown"
        logger.info("[%s] Subscribing to: %s", self_id, subject)

        # Use subscribe_with_subject to get the matched subject in callback
        # This allows extracting device_id from wildcard subscriptions
        messaging_client = self._router._messaging

        async def message_handler(data: bytes, matched_subject: str, _reply: Optional[str]):
            try:
                parsed = json.loads(data.decode())
                # Extract device_id from subject: device-connect.{tenant}.{device_id}.event.{event}
                # Zenoh delivers key expressions with slashes; NATS uses dots — handle both.
                sep = "/" if "/" in matched_subject else "."
                parts = matched_subject.split(sep)
                # Parse from the end for robustness: {prefix}.{tenant}.{device_id}.event.{name}
                # This handles device IDs that might contain the separator.
                if len(parts) >= 5 and parts[-2] == "event":
                    source_device_id = sep.join(parts[2:-2])
                elif len(parts) > 2:
                    source_device_id = parts[2]
                else:
                    source_device_id = "unknown"
                source_event_name = parsed.get("method", event_name)
                payload = parsed.get("params", {})

                # Filter by device_type if specified
                if device_type:
                    # Try to resolve type from D2D peer cache
                    source_type = ""
                    collector = getattr(self._device, '_d2d_collector', None) if self._device else None
                    if collector is not None:
                        peer = await collector.get_device(source_device_id)
                        if peer:
                            source_type = (peer.get("identity") or {}).get("device_type", "")
                    if source_type:
                        if source_type.lower() != device_type.lower():
                            return
                    else:
                        # Cannot resolve device_type from D2D peer cache.
                        # Don't filter — false negatives (dropping valid events)
                        # are worse than false positives (passing extra events).
                        logger.debug(
                            "[%s] Cannot resolve device_type for %s, "
                            "allowing event through (wanted type=%s)",
                            self_id, source_device_id, device_type,
                        )

                await handler(source_device_id, source_event_name, payload)
            except Exception as e:
                logger.error("Error in subscription handler: %s", e)

        subscription = await messaging_client.subscribe_with_subject(subject, message_handler, subscribe_only=True)
        self._subscriptions.append(subscription)

    async def teardown_subscriptions(self) -> None:
        """Tear down all event subscriptions.

        Called by DeviceRuntime during shutdown.
        """
        if self._subscriptions:
            logger.info("Tearing down %d event subscriptions", len(self._subscriptions))

            for sub in self._subscriptions:
                try:
                    await sub.unsubscribe()
                except Exception as e:
                    logger.error("Error unsubscribing: %s", e)

            self._subscriptions.clear()

        # Cancel any subscription tasks
        for task in self._subscription_tasks:
            if not task.done():
                task.cancel()
        self._subscription_tasks.clear()

        # Tear down transport subscriptions
        if self._transport is not None:
            await self._transport.teardown()


def on(
    device_id: Optional[str] = None,
    device_type: Optional[str] = None,
    event_name: Optional[str] = None,
) -> Callable:
    """Decorator to subscribe to events from OTHER devices (via pubsub).

    Use this to react to events from devices of a DIFFERENT type.
    For reacting to your OWN device's internal state, use @before_emit instead.

    The decorated method will be called when matching events are received.
    At least one of device_id, device_type, or event_name should be specified.

    Args:
        device_id: Subscribe to events from a specific device
        device_type: Subscribe to events from all devices of this type
        event_name: Subscribe to a specific event type

    Returns:
        Decorated method

    Example:
        # In a coordinator device, subscribe to camera events
        @on(device_type="camera", event_name="motion_detected")
        async def on_motion(self, device_id: str, event_name: str, payload: dict):
            '''React to motion from cameras (different device type).'''
            await self.dispatch_robot(payload.get("zone"))

        # In a camera, subscribe to robot completion events
        @on(device_type="robot", event_name="cleaning_complete")
        async def on_cleaning_done(self, device_id: str, event_name: str, payload: dict):
            '''React to robot completion (different device type).'''
            await self.verify_cleanup(payload.get("zone"))
    """
    def decorator(func: Callable) -> Callable:
        func._is_event_subscription = True
        func._sub_device_id = device_id
        func._sub_device_type = device_type
        func._sub_event_name = event_name
        return func

    return decorator
