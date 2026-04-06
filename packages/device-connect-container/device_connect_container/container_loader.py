"""Container-aware capability loader for Device Connect.

Replaces in-process importlib loading with OCI container sidecars.
Capabilities with a ``container`` key in their manifest run as separate
containers; those without are delegated to the standard CapabilityLoader.

The ContainerCapabilityProxy creates callable wrappers for each @rpc
method that forward calls over Zenoh JSON-RPC to the sidecar container,
and subscribes to event topics for @emit methods. This allows the
CapabilityDriverMixin to treat containerized capabilities identically
to in-process ones.
"""

import asyncio
import inspect
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, TYPE_CHECKING

from device_connect_container.manifest import ContainerManifest, ContainerConfig

if TYPE_CHECKING:
    from device_connect_edge.drivers import DeviceDriver
    from device_connect_edge.messaging import MessagingClient

logger = logging.getLogger(__name__)


class EventEmitter(Protocol):
    """Protocol for event emission callback."""

    async def __call__(self, event_name: str, payload: dict) -> None: ...


@dataclass
class ContainerCapabilityInfo:
    """Information about a containerized capability."""

    id: str
    manifest: ContainerManifest
    container_config: ContainerConfig
    proxy: "ContainerCapabilityProxy"
    functions: List[str] = field(default_factory=list)
    function_schemas: Dict[str, dict] = field(default_factory=dict)


class ContainerCapabilityProxy:
    """Proxy that forwards @rpc calls to a capability sidecar over Zenoh.

    The proxy subscribes to the sidecar's command response and event topics,
    and exposes callable wrappers that send JSON-RPC requests.

    This proxy implements the same interface that CapabilityLoader._register_functions()
    expects: each @rpc method is a callable attribute with ``_is_device_function``
    set to True.

    Topic scheme for containerized capabilities:
        Commands:  device-connect.{tenant}.{device_id}.cap.{cap_id}.cmd
        Events:    device-connect.{tenant}.{device_id}.cap.{cap_id}.event.{name}
        Health:    device-connect.{tenant}.{device_id}.cap.{cap_id}.health
    """

    def __init__(
        self,
        capability_id: str,
        manifest: ContainerManifest,
        messaging: "MessagingClient",
        device_id: str,
        tenant: str = "default",
        timeout: float = 30.0,
    ):
        self._capability_id = capability_id
        self._manifest = manifest
        self._messaging = messaging
        self._device_id = device_id
        self._tenant = tenant
        self._timeout = timeout
        self._event_subscriptions: list = []
        self._health_subscription = None
        self._healthy = False
        self._ready_event = asyncio.Event()

    @property
    def capability_id(self) -> str:
        return self._capability_id

    @property
    def cmd_subject(self) -> str:
        """Zenoh subject for sending commands to this capability's sidecar."""
        return f"device-connect.{self._tenant}.{self._device_id}.cap.{self._capability_id}.cmd"

    @property
    def event_subject_prefix(self) -> str:
        """Zenoh subject prefix for events from this capability's sidecar."""
        return f"device-connect.{self._tenant}.{self._device_id}.cap.{self._capability_id}.event"

    @property
    def health_subject(self) -> str:
        """Zenoh subject for health checks from this capability's sidecar."""
        return f"device-connect.{self._tenant}.{self._device_id}.cap.{self._capability_id}.health"

    async def start(self, event_emitter: EventEmitter) -> None:
        """Start the proxy: subscribe to health and event topics.

        Args:
            event_emitter: Callback for forwarding events from the sidecar
                to the device runtime's event dispatch.
        """
        # Subscribe to health topic to know when sidecar is ready
        self._health_subscription = await self._messaging.subscribe(
            self.health_subject,
            self._on_health_message,
        )

        # Subscribe to all events from this capability's sidecar
        event_pattern = f"{self.event_subject_prefix}.*"
        sub = await self._messaging.subscribe(
            event_pattern,
            lambda subject, data: asyncio.create_task(
                self._on_event_message(subject, data, event_emitter)
            ),
        )
        self._event_subscriptions.append(sub)

        logger.info(
            "ContainerCapabilityProxy started for %s (cmd=%s)",
            self._capability_id,
            self.cmd_subject,
        )

    async def stop(self) -> None:
        """Stop the proxy: unsubscribe from all topics."""
        if self._health_subscription:
            await self._health_subscription.unsubscribe()
            self._health_subscription = None
        for sub in self._event_subscriptions:
            await sub.unsubscribe()
        self._event_subscriptions.clear()
        logger.info("ContainerCapabilityProxy stopped for %s", self._capability_id)

    async def wait_ready(self, timeout: float = 60.0) -> bool:
        """Wait for the sidecar container to report healthy.

        Args:
            timeout: Maximum seconds to wait.

        Returns:
            True if sidecar is ready, False if timed out.
        """
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "Capability sidecar %s did not become ready within %.0fs",
                self._capability_id,
                timeout,
            )
            return False

    def create_rpc_callable(self, function_name: str, schema: dict) -> Callable:
        """Create a callable that forwards an @rpc invocation to the sidecar.

        The returned callable has ``_is_device_function = True`` and the
        expected metadata so it integrates with CapabilityDriverMixin's
        function registration.

        Args:
            function_name: Name of the @rpc method on the capability.
            schema: JSON Schema for the function's parameters.

        Returns:
            An async callable that sends JSON-RPC to the sidecar.
        """

        async def rpc_proxy(**params: Any) -> Any:
            req_id = f"cap-{uuid.uuid4().hex[:12]}"
            rpc_payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": function_name,
                "params": params,
            }
            response_data = await self._messaging.request(
                self.cmd_subject,
                json.dumps(rpc_payload).encode(),
                timeout=self._timeout,
            )
            response = json.loads(response_data.decode())
            if "error" in response:
                error = response["error"]
                raise RuntimeError(
                    f"Capability {self._capability_id}.{function_name} error: "
                    f"{error.get('message', 'unknown')} (code={error.get('code', -1)})"
                )
            return response.get("result")

        # Attach metadata expected by CapabilityDriverMixin._register_functions()
        rpc_proxy._is_device_function = True
        rpc_proxy._function_name = function_name
        rpc_proxy._description = schema.get("description", "")
        rpc_proxy.__name__ = function_name
        rpc_proxy.__qualname__ = f"{self._capability_id}.{function_name}"

        return rpc_proxy

    def _on_health_message(self, subject: str, data: bytes) -> None:
        """Handle health message from sidecar."""
        try:
            msg = json.loads(data.decode())
            self._healthy = msg.get("healthy", False)
            if self._healthy and not self._ready_event.is_set():
                self._ready_event.set()
                logger.info("Capability sidecar %s is ready", self._capability_id)
        except Exception as e:
            logger.warning("Bad health message from %s: %s", self._capability_id, e)

    async def _on_event_message(
        self, subject: str, data: bytes, event_emitter: EventEmitter,
    ) -> None:
        """Forward an event from the sidecar to the device runtime."""
        try:
            # Extract event name from subject suffix
            # subject: device-connect.{tenant}.{device_id}.cap.{cap_id}.event.{name}
            parts = subject.split(".")
            event_name = parts[-1] if parts else "unknown"
            payload = json.loads(data.decode())
            await event_emitter(event_name, payload)
        except Exception as e:
            logger.error("Error forwarding event from %s: %s", self._capability_id, e)


class ContainerCapabilityLoader:
    """Load capabilities as container sidecars or delegate to in-process loading.

    For each capability directory:
    - If manifest.json has a ``container`` key → launch as OCI sidecar,
      create a ContainerCapabilityProxy
    - If no ``container`` key → delegate to the standard CapabilityLoader
      for in-process importlib loading

    This loader implements the same public interface as CapabilityLoader
    (load_all, unload_all, get_functions, invoke, etc.) so that
    CapabilityDriverMixin works with either loader transparently.
    """

    def __init__(
        self,
        event_emitter: EventEmitter,
        capabilities_dir: Path,
        messaging: "MessagingClient",
        device_id: str,
        tenant: str = "default",
        simulation_mode: bool = False,
    ):
        self._event_emitter = event_emitter
        self._capabilities_dir = Path(capabilities_dir)
        self._messaging = messaging
        self._device_id = device_id
        self._tenant = tenant
        self._simulation_mode = simulation_mode

        # Container capabilities
        self._container_caps: Dict[str, ContainerCapabilityInfo] = {}
        self._functions: Dict[str, Callable] = {}

        # Fallback in-process loader for non-containerized capabilities
        self._inprocess_loader = None

        # Reference to driver
        self._driver: Optional["DeviceDriver"] = None

    @property
    def simulation_mode(self) -> bool:
        return self._simulation_mode

    @simulation_mode.setter
    def simulation_mode(self, enabled: bool) -> None:
        self._simulation_mode = enabled
        if self._inprocess_loader:
            self._inprocess_loader.simulation_mode = enabled

    def set_driver(self, driver: "DeviceDriver") -> None:
        """Set the driver reference for capability constructors."""
        self._driver = driver
        if self._inprocess_loader:
            self._inprocess_loader.set_driver(driver)

    def _get_inprocess_loader(self):
        """Lazily create the fallback in-process loader."""
        if self._inprocess_loader is None:
            from device_connect_edge.drivers.capability_loader import CapabilityLoader

            self._inprocess_loader = CapabilityLoader(
                event_emitter=self._event_emitter,
                capabilities_dir=self._capabilities_dir,
                tenant=self._tenant,
                simulation_mode=self._simulation_mode,
            )
            if self._driver:
                self._inprocess_loader.set_driver(self._driver)
        return self._inprocess_loader

    async def load_all(self) -> int:
        """Load all capabilities from the capabilities directory.

        Containerized capabilities get a proxy; others load in-process.

        Returns:
            Number of capabilities loaded.
        """
        if not self._capabilities_dir.exists():
            logger.debug("Capabilities directory does not exist: %s", self._capabilities_dir)
            return 0

        count = 0
        inprocess_dirs: List[Path] = []

        for cap_path in self._capabilities_dir.iterdir():
            if not cap_path.is_dir():
                continue

            manifest_file = cap_path / "manifest.json"
            if not manifest_file.exists():
                continue

            try:
                manifest = ContainerManifest.from_manifest_file(manifest_file)
            except Exception as e:
                logger.error("Failed to parse manifest %s: %s", manifest_file, e)
                continue

            if manifest.is_containerized:
                try:
                    if await self._load_containerized(cap_path, manifest):
                        count += 1
                except Exception as e:
                    logger.exception(
                        "Failed to load containerized capability from %s: %s",
                        cap_path, e,
                    )
            else:
                # Queue for in-process loading
                inprocess_dirs.append(cap_path)

        # Load non-containerized capabilities via standard loader
        if inprocess_dirs:
            loader = self._get_inprocess_loader()
            for cap_dir in inprocess_dirs:
                try:
                    if await loader._load_capability(cap_dir):
                        count += 1
                except Exception as e:
                    logger.exception(
                        "Failed to load in-process capability from %s: %s",
                        cap_dir, e,
                    )

        logger.info(
            "Loaded %d capabilities (%d containerized, %d in-process) from %s",
            count,
            len(self._container_caps),
            count - len(self._container_caps),
            self._capabilities_dir,
        )
        return count

    async def _load_containerized(
        self, cap_path: Path, manifest: ContainerManifest,
    ) -> bool:
        """Load a capability as a container sidecar.

        Creates a ContainerCapabilityProxy and registers its RPC callables.
        The actual container should already be running (started by Docker
        Compose or the ZenohRouterManager).

        Args:
            cap_path: Path to capability directory.
            manifest: Parsed manifest with container config.

        Returns:
            True if loaded successfully.
        """
        cap_id = manifest.id
        container_config = manifest.container

        # Create proxy
        proxy = ContainerCapabilityProxy(
            capability_id=cap_id,
            manifest=manifest,
            messaging=self._messaging,
            device_id=self._device_id,
            tenant=self._tenant,
        )

        # Start proxy (subscribe to health + event topics)
        await proxy.start(self._emit_event)

        # Wait for sidecar to become healthy
        sidecar_timeout = float(os.getenv("DEVICE_CONNECT_SIDECAR_TIMEOUT", "60"))
        ready = await proxy.wait_ready(timeout=sidecar_timeout)
        if not ready:
            logger.error(
                "Capability sidecar %s not ready after %.0fs, loading anyway",
                cap_id,
                sidecar_timeout,
            )

        # Query sidecar for its function schemas
        # The sidecar publishes its capabilities on the health topic
        schemas = await self._query_sidecar_schemas(proxy)

        # Create RPC callables from schemas and register them
        info = ContainerCapabilityInfo(
            id=cap_id,
            manifest=manifest,
            container_config=container_config,
            proxy=proxy,
        )

        for func_name, schema in schemas.items():
            callable_fn = proxy.create_rpc_callable(func_name, schema)
            self._functions[func_name] = callable_fn
            self._functions[f"{cap_id}.{func_name}"] = callable_fn
            info.functions.append(func_name)
            info.function_schemas[func_name] = schema

        self._container_caps[cap_id] = info
        logger.info(
            "Loaded containerized capability: %s (functions=%s)",
            cap_id,
            info.functions,
        )
        return True

    async def _query_sidecar_schemas(
        self, proxy: ContainerCapabilityProxy,
    ) -> Dict[str, dict]:
        """Query the sidecar container for its function schemas.

        Sends a special ``_describe`` JSON-RPC method to the sidecar's
        command topic.

        Args:
            proxy: The capability proxy.

        Returns:
            Dict mapping function names to their JSON schemas.
        """
        try:
            req_id = f"desc-{uuid.uuid4().hex[:8]}"
            describe_payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "_describe",
                "params": {},
            }
            response_data = await self._messaging.request(
                proxy.cmd_subject,
                json.dumps(describe_payload).encode(),
                timeout=10.0,
            )
            response = json.loads(response_data.decode())
            if "result" in response:
                return response["result"].get("functions", {})
        except Exception as e:
            logger.warning(
                "Failed to query schemas from sidecar %s: %s",
                proxy.capability_id, e,
            )
        return {}

    async def _emit_event(self, event_name: str, payload: dict) -> None:
        """Forward events from container sidecars to the device runtime."""
        if self._simulation_mode:
            payload = dict(payload)
            payload["simulated"] = True
        await self._event_emitter(event_name, payload)

    async def unload_all(self) -> None:
        """Unload all capabilities (container proxies + in-process)."""
        # Stop container proxies
        for cap_id, info in self._container_caps.items():
            try:
                await info.proxy.stop()
            except Exception as e:
                logger.error("Error stopping proxy for %s: %s", cap_id, e)
        self._container_caps.clear()
        self._functions.clear()

        # Unload in-process capabilities
        if self._inprocess_loader:
            await self._inprocess_loader.unload_all()

        logger.info("Unloaded all capabilities")

    def get_functions(self) -> Dict[str, Callable]:
        """Get all registered capability functions (container + in-process)."""
        funcs = dict(self._functions)
        if self._inprocess_loader:
            funcs.update(self._inprocess_loader.get_functions())
        return funcs

    def get_capabilities(self) -> dict:
        """Get all loaded capabilities."""
        caps = {}
        for cap_id, info in self._container_caps.items():
            caps[cap_id] = info
        if self._inprocess_loader:
            caps.update(self._inprocess_loader.get_capabilities())
        return caps

    async def invoke(self, function: str, **params) -> Any:
        """Invoke a capability function (container or in-process).

        Args:
            function: Function name (with or without capability prefix).
            **params: Function parameters.

        Returns:
            Function result.

        Raises:
            KeyError: If function not found.
        """
        if function in self._functions:
            return await self._functions[function](**params)

        if self._inprocess_loader and self._inprocess_loader.has_function(function):
            return await self._inprocess_loader.invoke(function, **params)

        raise KeyError(f"Function not found: {function}")

    def has_function(self, function: str) -> bool:
        """Check if a function is registered."""
        if function in self._functions:
            return True
        if self._inprocess_loader:
            return self._inprocess_loader.has_function(function)
        return False

    def get_subscriptions(self) -> list:
        """Get all event subscriptions."""
        subs = []
        if self._inprocess_loader:
            subs.extend(self._inprocess_loader.get_subscriptions())
        return subs

    async def start_all_routines(self) -> int:
        """Start all routines (in-process only; sidecar routines run internally)."""
        if self._inprocess_loader:
            return await self._inprocess_loader.start_all_routines()
        return 0
