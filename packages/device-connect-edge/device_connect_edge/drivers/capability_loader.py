"""In-process capability loading for Device Connect devices.

This module provides infrastructure for loading capabilities from disk
at runtime. Capabilities are Python classes with @rpc, @emit, and @periodic
decorated methods.

Key Components:
    - CapabilityLoader: Load capabilities from a directory
    - CapabilityDriverMixin: Add capability loading to any DeviceDriver
    - LoadedCapability: Information about a loaded capability
    - EventSubscription: Event subscription for agentic pattern

Example:
    from device_connect_edge.drivers import DeviceDriver
    from device_connect_edge.drivers.capability_loader import CapabilityDriverMixin

    class MyDriver(CapabilityDriverMixin, DeviceDriver):
        def __init__(self):
            super().__init__()
            self.init_capabilities(Path("./capabilities"))

        async def connect(self) -> None:
            await super().connect()
            await self.load_capabilities()

        async def disconnect(self) -> None:
            await self.unload_capabilities()
            await super().disconnect()
"""
import asyncio
import importlib
import importlib.util
import inspect
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from device_connect_edge.drivers import DeviceDriver

logger = logging.getLogger(__name__)


class EventEmitter(Protocol):
    """Protocol for event emission callback."""

    async def __call__(self, event_name: str, payload: dict) -> None:
        """Emit an event with the given name and payload."""
        ...


@dataclass
class LoadedCapability:
    """Information about a loaded capability."""

    id: str
    instance: Any
    manifest: dict
    functions: List[str] = field(default_factory=list)
    routines: List[str] = field(default_factory=list)
    function_schemas: Dict[str, dict] = field(default_factory=dict)
    routine_configs: Dict[str, dict] = field(default_factory=dict)  # name -> {callable, interval}


@dataclass
class EventSubscription:
    """Event subscription for agentic pattern."""

    capability_id: str
    subject: str
    device_type_filter: str
    handler: Callable


class CapabilityLoader:
    """Load and wire capabilities in-process.

    This class handles loading capabilities from a directory into the current
    Python process, wiring up @rpc, @emit, and @periodic decorated methods.

    Attributes:
        capabilities_dir: Directory containing capability subdirectories
        simulation_mode: When True, all emitted events have simulated=True
    """

    def __init__(
        self,
        event_emitter: EventEmitter,
        capabilities_dir: Path,
        tenant: str = "default",
        simulation_mode: bool = False,
    ):
        """Initialize the capability loader.

        Args:
            event_emitter: Async callable for emitting events
            capabilities_dir: Directory containing capability subdirectories
            tenant: Tenant identifier for event subscriptions
            simulation_mode: Tag all events with simulated=True
        """
        self._event_emitter = event_emitter
        self._capabilities_dir = Path(capabilities_dir)
        self._tenant = tenant
        self._simulation_mode = simulation_mode

        # Loaded state
        self._capabilities: Dict[str, LoadedCapability] = {}
        self._functions: Dict[str, Callable] = {}
        self._routines: Dict[str, asyncio.Task] = {}
        self._subscriptions: List[EventSubscription] = []
        self._spawned_tasks: set[asyncio.Task] = set()
        self._module_names: Dict[str, str] = {}  # cap_name -> sys.modules key

        # Reference to driver (set externally if needed for capability constructor)
        self._driver: Optional["DeviceDriver"] = None

    @property
    def simulation_mode(self) -> bool:
        """Get simulation mode status."""
        return self._simulation_mode

    @simulation_mode.setter
    def simulation_mode(self, enabled: bool) -> None:
        """Set simulation mode."""
        self._simulation_mode = enabled

    def set_driver(self, driver: "DeviceDriver") -> None:
        """Set the driver reference for capability constructors.

        Args:
            driver: DeviceDriver instance to pass to capabilities
        """
        self._driver = driver

    # Mapping of pip package names to their Python import names
    # for packages where these differ
    _PIP_TO_IMPORT = {
        "opencv-python": "cv2",
        "opencv-python-headless": "cv2",
        "Pillow": "PIL",
        "pyserial": "serial",
        "pyyaml": "yaml",
        "scikit-learn": "sklearn",
        "bosdyn-client": "bosdyn.client",
        "bosdyn-core": "bosdyn",
    }

    def _check_dependencies(self, cap_id: str, manifest: dict) -> List[str]:
        """Check if declared Python dependencies are importable.

        Logs warnings for missing packages with install instructions.
        Does NOT install anything or block capability loading.

        Args:
            cap_id: Capability identifier for log messages
            manifest: Parsed manifest.json dict

        Returns:
            List of missing dependency specs (e.g. ["pyserial", "numpy"])
        """
        deps = manifest.get("dependencies", {}).get("python", [])
        if not deps:
            return []

        missing = []
        for dep_spec in deps:
            # Extract package name from version spec (e.g., "openai>=1.0.0" -> "openai")
            pkg_name = dep_spec.split(">=")[0].split("<=")[0].split("==")[0].split(">")[0].split("<")[0].split("!=")[0].strip()

            import_name = self._PIP_TO_IMPORT.get(pkg_name, pkg_name.replace("-", "_"))

            try:
                importlib.import_module(import_name)
            except ImportError:
                missing.append(dep_spec)

        if missing:
            install_cmd = " ".join(missing)
            logger.warning(
                f"[{cap_id}] Missing {len(missing)} declared dependencies: {missing}. "
                f"Install with: pip install {install_cmd}"
            )
        else:
            logger.info(f"[{cap_id}] All {len(deps)} declared dependencies are available")

        return missing

    async def load_all(self) -> int:
        """Load all capabilities from the capabilities directory.

        Returns:
            Number of capabilities loaded

        Raises:
            FileNotFoundError: If capabilities_dir doesn't exist
        """
        if not self._capabilities_dir.exists():
            logger.debug(f"Capabilities directory does not exist: {self._capabilities_dir}")
            return 0

        count = 0
        for cap_path in self._capabilities_dir.iterdir():
            if not cap_path.is_dir():
                continue

            try:
                if await self._load_capability(cap_path):
                    count += 1
            except Exception as e:
                logger.exception("Failed to load capability from %s: %s", cap_path, e)

        logger.info(f"Loaded {count} capabilities from {self._capabilities_dir}")
        return count

    async def load_one(self, capability_id: str) -> bool:
        """Load a single capability by ID.

        Args:
            capability_id: The capability directory name or manifest ID

        Returns:
            True if loaded successfully
        """
        cap_path = self._capabilities_dir / capability_id
        if not cap_path.exists():
            logger.error(f"Capability not found: {capability_id}")
            return False

        return await self._load_capability(cap_path)

    async def _load_capability(self, cap_path: Path) -> bool:
        """Load a capability from a directory.

        Args:
            cap_path: Path to capability directory

        Returns:
            True if loaded successfully
        """
        manifest_file = cap_path / "manifest.json"
        if not manifest_file.exists():
            logger.warning(f"No manifest.json in {cap_path}")
            return False

        with open(manifest_file) as f:
            manifest = json.load(f)

        cap_id = manifest.get("id", cap_path.name)
        entry_point = manifest.get("entry_point", "capability.py")
        class_name = manifest.get("class_name")

        # Check declared dependencies (warn only, don't block loading)
        self._check_dependencies(cap_id, manifest)

        if not class_name:
            logger.warning(
                f"No class_name in manifest for {cap_id}. "
                f"Add '\"class_name\": \"YourClassName\"' to {manifest_file}"
            )
            return False

        # Load the capability module
        cap_file = cap_path / entry_point
        if not cap_file.exists():
            logger.error(f"Entry point not found: {cap_file}")
            return False

        spec = importlib.util.spec_from_file_location(
            f"capability_{cap_id}", cap_file
        )
        if spec is None or spec.loader is None:
            logger.error(f"Could not create module spec for {cap_file}")
            return False

        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self._module_names[cap_id] = spec.name
        spec.loader.exec_module(module)

        # Instantiate the capability
        # Pass DeviceRuntime (driver._device) for D2D invoke support
        # Falls back to driver if _device not set (shouldn't happen in normal flow)
        cap_class = getattr(module, class_name)
        device_ref = getattr(self._driver, '_device', self._driver)
        cap_instance = cap_class(device=device_ref)

        # Create LoadedCapability record
        loaded = LoadedCapability(
            id=cap_id,
            instance=cap_instance,
            manifest=manifest,
        )

        # Wire @rpc methods
        self._register_functions(loaded)

        # Wire @emit methods
        self._setup_events(cap_instance)

        # Collect @periodic routines (but don't start them yet)
        self._collect_routines(loaded)

        # Set up event subscriptions (agentic pattern)
        await self._setup_subscriptions(loaded)

        # Call start() lifecycle method
        if hasattr(cap_instance, "start"):
            if inspect.iscoroutinefunction(cap_instance.start):
                await cap_instance.start()
            else:
                cap_instance.start()

        self._capabilities[cap_id] = loaded
        logger.info(f"Loaded capability: {cap_id} "
                    f"(functions={loaded.functions}, routines={loaded.routines})")
        return True

    def _track_task(self, task: asyncio.Task) -> asyncio.Task:
        """Register a dynamically-spawned task for lifecycle tracking."""
        self._spawned_tasks.add(task)
        task.add_done_callback(self._spawned_tasks.discard)
        return task

    async def unload_all(self) -> None:
        """Unload all capabilities, cancel routines, and cleanup."""
        # Cancel all routines
        for task_name, task in self._routines.items():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._routines.clear()

        # Cancel all tracked spawned tasks
        for task in list(self._spawned_tasks):
            task.cancel()
        self._spawned_tasks.clear()

        # Clear function registrations
        self._functions.clear()

        # Clear subscriptions
        self._subscriptions.clear()

        # Stop capabilities (call stop() lifecycle method)
        for cap_id, loaded in self._capabilities.items():
            try:
                if hasattr(loaded.instance, "stop"):
                    if inspect.iscoroutinefunction(loaded.instance.stop):
                        await loaded.instance.stop()
                    else:
                        loaded.instance.stop()
            except Exception as e:
                logger.error(f"Error stopping capability {cap_id}: {e}")

        # Clean up sys.modules entries for loaded capability modules
        for mod_name in self._module_names.values():
            sys.modules.pop(mod_name, None)
        self._module_names.clear()

        self._capabilities.clear()
        logger.info("Unloaded all capabilities")

    async def unload_one(self, capability_id: str) -> bool:
        """Unload a single capability.

        Args:
            capability_id: The capability ID to unload

        Returns:
            True if unloaded successfully
        """
        if capability_id not in self._capabilities:
            return False

        loaded = self._capabilities[capability_id]

        # Cancel routines for this capability
        for routine_name in loaded.routines:
            key = f"{capability_id}.{routine_name}"
            if key in self._routines:
                self._routines[key].cancel()
                try:
                    await self._routines[key]
                except asyncio.CancelledError:
                    pass
                del self._routines[key]

        # Remove functions
        for func_name in loaded.functions:
            self._functions.pop(func_name, None)
            self._functions.pop(f"{capability_id}.{func_name}", None)

        # Remove subscriptions
        self._subscriptions = [
            s for s in self._subscriptions if s.capability_id != capability_id
        ]

        # Stop capability
        try:
            if hasattr(loaded.instance, "stop"):
                if inspect.iscoroutinefunction(loaded.instance.stop):
                    await loaded.instance.stop()
                else:
                    loaded.instance.stop()
        except Exception as e:
            logger.error(f"Error stopping capability {capability_id}: {e}")

        # Clean up sys.modules entry for this capability's module
        mod_name = self._module_names.pop(capability_id, None)
        if mod_name:
            sys.modules.pop(mod_name, None)

        del self._capabilities[capability_id]
        logger.info(f"Unloaded capability: {capability_id}")
        return True

    def get_functions(self) -> Dict[str, Callable]:
        """Get all registered capability functions.

        Returns:
            Dict mapping function names to callables
        """
        return dict(self._functions)

    def get_subscriptions(self) -> List[EventSubscription]:
        """Get all event subscriptions.

        Returns:
            List of EventSubscription objects
        """
        return list(self._subscriptions)

    def get_capabilities(self) -> Dict[str, LoadedCapability]:
        """Get all loaded capabilities.

        Returns:
            Dict mapping capability IDs to LoadedCapability objects
        """
        return dict(self._capabilities)

    async def invoke(self, function: str, **params) -> Any:
        """Invoke a capability function.

        Args:
            function: Function name (with or without capability prefix)
            **params: Function parameters

        Returns:
            Function result

        Raises:
            KeyError: If function not found
        """
        if function not in self._functions:
            raise KeyError(f"Function not found: {function}")

        method = self._functions[function]
        if inspect.iscoroutinefunction(method):
            return await method(**params)
        else:
            return method(**params)

    def has_function(self, function: str) -> bool:
        """Check if a function is registered.

        Args:
            function: Function name

        Returns:
            True if function exists
        """
        return function in self._functions

    def _register_functions(self, loaded: LoadedCapability) -> None:
        """Register @rpc decorated methods from a capability.

        Args:
            loaded: LoadedCapability object
        """
        from device_connect_edge.drivers.decorators import build_function_schema

        cap_instance = loaded.instance
        cap_id = loaded.id

        for attr_name in dir(cap_instance):
            if attr_name.startswith("_"):
                continue
            try:
                attr = getattr(cap_instance, attr_name)
                if callable(attr) and getattr(attr, "_is_device_function", False):
                    func_name = getattr(attr, "_function_name", attr_name)

                    # Extract full schema for registration
                    description = getattr(attr, "_description", "") or ""
                    if not description and attr.__doc__:
                        # Use first line of docstring
                        description = attr.__doc__.strip().split("\n")[0]
                    parameters = build_function_schema(attr)

                    # Store schema in loaded capability
                    loaded.function_schemas[func_name] = {
                        "description": description,
                        "parameters": parameters,
                    }

                    # Register with namespace prefix
                    self._functions[f"{cap_id}.{func_name}"] = attr
                    # Also register without prefix for direct invocation
                    if func_name in self._functions:
                        logger.warning(
                            "Capability function %s from %s shadows existing registration",
                            func_name, cap_id,
                        )
                    self._functions[func_name] = attr
                    loaded.functions.append(func_name)
                    logger.debug(f"Registered function: {func_name} from {cap_id}")
            except Exception as e:
                logger.warning(f"Error inspecting {attr_name}: {e}")

    def _setup_events(self, cap_instance: Any) -> None:
        """Set up event emission for capability's @emit methods.

        The @emit decorator expects _dispatch_internal_event and _emit_event_internal
        methods on the instance. We inject these to forward to the loader's emitter.

        Args:
            cap_instance: Capability instance
        """
        # Create bound methods that forward to the event emitter
        async def dispatch_internal_event(event_name: str, payload: dict):
            """Pass through - no internal handlers in capabilities."""
            return (True, payload)

        async def emit_event_internal(event_name: str, payload: dict):
            """Forward to loader's event emitter, adding simulation flag if needed."""
            if self._simulation_mode:
                payload = dict(payload)  # Copy to avoid mutating original
                payload["simulated"] = True
            await self._event_emitter(event_name, payload)

        # Inject methods into capability instance
        cap_instance._dispatch_internal_event = dispatch_internal_event
        cap_instance._emit_event_internal = emit_event_internal

        # Also set callback if the capability uses an older pattern
        if hasattr(cap_instance, "set_event_callback"):
            cap_instance.set_event_callback(
                lambda name, payload: self._track_task(asyncio.create_task(emit_event_internal(name, payload)))
            )
        elif hasattr(cap_instance, "_event_callback"):
            cap_instance._event_callback = (
                lambda name, payload: self._track_task(asyncio.create_task(emit_event_internal(name, payload)))
            )

    def _collect_routines(self, loaded: LoadedCapability) -> None:
        """Collect @periodic routine metadata for a capability (without starting them).

        Args:
            loaded: LoadedCapability object
        """
        cap_instance = loaded.instance

        for attr_name in dir(cap_instance):
            if attr_name.startswith("_"):
                continue
            try:
                attr = getattr(cap_instance, attr_name)
                if callable(attr) and getattr(attr, "_is_device_routine", False):
                    interval = getattr(attr, "_routine_interval", 1.0)
                    wait_for_completion = getattr(attr, "_routine_wait_for_completion", True)
                    loaded.routines.append(attr_name)
                    loaded.routine_configs[attr_name] = {
                        "callable": attr,
                        "interval": interval,
                        "wait_for_completion": wait_for_completion,
                    }
                    logger.debug(f"Collected routine: {attr_name} (interval={interval}s)")
            except Exception as e:
                logger.warning(f"Error collecting routine {attr_name}: {e}")

    async def start_all_routines(self) -> int:
        """Start all collected routines for all loaded capabilities.

        This should be called after the device is fully set up and registered.

        Returns:
            Number of routines started
        """
        count = 0
        for cap_id, loaded in self._capabilities.items():
            started = await self._start_capability_routines(loaded)
            count += started
        if count > 0:
            logger.info(f"Started {count} capability routines")
        return count

    async def _start_capability_routines(self, loaded: LoadedCapability) -> int:
        """Start @periodic routines for a capability.

        Args:
            loaded: LoadedCapability object

        Returns:
            Number of routines started
        """
        cap_id = loaded.id
        count = 0

        for routine_name, config in loaded.routine_configs.items():
            key = f"{cap_id}.{routine_name}"
            # Skip if already running
            if key in self._routines and not self._routines[key].done():
                continue

            routine = config["callable"]
            interval = config["interval"]
            wait_for_completion = config.get("wait_for_completion", True)
            task = asyncio.create_task(
                self._run_routine(cap_id, routine_name, routine, interval, wait_for_completion)
            )
            self._routines[key] = task
            count += 1
            logger.debug(f"Started routine: {routine_name} from {cap_id} (interval={interval}s)")

        return count

    async def _run_routine(
        self, cap_id: str, routine_name: str, routine: Callable, interval: float,
        wait_for_completion: bool = True,
    ) -> None:
        """Run a capability's periodic routine.

        Args:
            cap_id: Capability identifier
            routine_name: Name of the routine
            routine: The routine callable
            interval: Interval in seconds
            wait_for_completion: If True, subtract elapsed time from sleep interval
        """
        from device_connect_edge.drivers.decorators import set_call_origin, reset_call_origin

        logger.debug(f"Starting routine {cap_id}.{routine_name} with interval {interval}s")
        while cap_id in self._capabilities:
            start_time = asyncio.get_running_loop().time()
            # Set call origin to "routine" so RPC logs show LOCAL instead of EXEC
            token = set_call_origin("routine")
            try:
                if inspect.iscoroutinefunction(routine):
                    await routine()
                else:
                    routine()
            except asyncio.CancelledError:
                logger.debug(f"Routine {cap_id}.{routine_name} cancelled")
                break
            except Exception as e:
                logger.error(f"Routine error in {cap_id}.{routine_name}: {e}")
            finally:
                reset_call_origin(token)

            if wait_for_completion:
                elapsed = asyncio.get_running_loop().time() - start_time
                sleep_time = max(0, interval - elapsed)
                await asyncio.sleep(sleep_time)
            else:
                await asyncio.sleep(interval)

    async def _setup_subscriptions(self, loaded: LoadedCapability) -> None:
        """Set up event subscriptions for a capability's agentic pattern.

        Args:
            loaded: LoadedCapability object
        """
        cap_instance = loaded.instance
        cap_id = loaded.id

        if not hasattr(cap_instance, "get_event_subscriptions"):
            return

        try:
            subscriptions = cap_instance.get_event_subscriptions()
            for sub in subscriptions:
                device_type = sub.get("device_type", "*")
                event_name = sub.get("event", "*")
                handler = sub.get("handler")

                if not handler:
                    logger.warning(f"No handler for subscription in {cap_id}")
                    continue

                # Subject pattern: device-connect.{tenant}.*.event.{event_name}
                subject = f"device-connect.{self._tenant}.*.event.{event_name}"
                subscription = EventSubscription(
                    capability_id=cap_id,
                    subject=subject,
                    device_type_filter=device_type,
                    handler=handler,
                )
                self._subscriptions.append(subscription)
                logger.debug(f"Subscribed {cap_id} to {subject} (device_type={device_type})")
        except Exception as e:
            logger.error(f"Error setting up subscriptions for {cap_id}: {e}")


# =============================================================================
# CapabilityDriverMixin - Add capability loading to any DeviceDriver
# =============================================================================


class CapabilityDriverMixin:
    """Mixin to add in-process capability loading to drivers.

    This mixin integrates CapabilityLoader with a DeviceDriver, providing:
    - Automatic function registration via _get_functions()
    - Automatic invoke() routing to capability functions
    - Access to event subscriptions for agentic pattern
    - Simulation mode control

    The mixin expects the driver to have:
    - _emit_event_internal(event_name, payload) method for event emission

    Usage:
        class MyDriver(CapabilityDriverMixin, DeviceDriver):
            def __init__(self):
                super().__init__()
                self.init_capabilities(Path("./capabilities"))
    """

    _capability_loader: Optional[CapabilityLoader] = None

    def init_capabilities(
        self,
        capabilities_dir: Path,
        tenant: Optional[str] = None,
        simulation_mode: bool = False,
    ) -> None:
        """Initialize capability loading.

        Call this in __init__ after super().__init__().

        Args:
            capabilities_dir: Directory containing capability subdirectories
            tenant: Tenant identifier (default: from TENANT env or "default")
            simulation_mode: Tag all events with simulated=True
        """
        if tenant is None:
            tenant = os.getenv("TENANT", "default")

        self._capability_loader = CapabilityLoader(
            event_emitter=self._emit_capability_event,
            capabilities_dir=capabilities_dir,
            tenant=tenant,
            simulation_mode=simulation_mode,
        )
        # Set driver reference for capability constructors
        self._capability_loader.set_driver(self)

    async def _emit_capability_event(self, event_name: str, payload: dict) -> None:
        """Forward capability events to the driver's event emission.

        Args:
            event_name: Name of the event
            payload: Event payload
        """
        # This method is called by CapabilityLoader when a capability emits an event
        # Forward to driver's internal event emission
        await self._emit_event_internal(event_name, payload)

    async def load_capabilities(self) -> int:
        """Load all capabilities from the capabilities directory.

        Returns:
            Number of capabilities loaded
        """
        if self._capability_loader:
            return await self._capability_loader.load_all()
        return 0

    async def start_capability_routines(self) -> int:
        """Start all capability routines.

        This should be called after the device is fully set up and registered.
        Capabilities are loaded during connect(), but routines are deferred
        until this method is called to ensure events don't fire before
        registration completes.

        Returns:
            Number of routines started
        """
        if self._capability_loader:
            return await self._capability_loader.start_all_routines()
        return 0

    async def unload_capabilities(self) -> None:
        """Unload all capabilities."""
        if self._capability_loader:
            await self._capability_loader.unload_all()

    async def load_capability(self, capability_id: str) -> bool:
        """Load a single capability.

        Args:
            capability_id: The capability ID to load

        Returns:
            True if loaded successfully
        """
        if self._capability_loader:
            result = await self._capability_loader.load_one(capability_id)
            if result:
                self._invalidate_caches()
            return result
        return False

    async def unload_capability(self, capability_id: str) -> bool:
        """Unload a single capability.

        Args:
            capability_id: The capability ID to unload

        Returns:
            True if unloaded successfully
        """
        if self._capability_loader:
            result = await self._capability_loader.unload_one(capability_id)
            if result:
                self._invalidate_caches()
            return result
        return False

    def _get_functions(self) -> Dict[str, Callable]:
        """Override to include capability functions.

        Returns:
            Dict mapping function names to callables
        """
        # Get base class functions
        funcs = super()._get_functions()

        # Add capability functions
        if self._capability_loader:
            funcs.update(self._capability_loader.get_functions())

        return funcs

    async def invoke(self, function_name: str, **params) -> Any:
        """Override to route to capability functions.

        Args:
            function_name: Function name to invoke
            **params: Function parameters

        Returns:
            Function result
        """
        # Check if it's a capability function
        if self._capability_loader and self._capability_loader.has_function(function_name):
            return await self._capability_loader.invoke(function_name, **params)

        # Fall back to base class
        return await super().invoke(function_name, **params)

    def get_capability_subscriptions(self) -> List[EventSubscription]:
        """Get event subscriptions from all capabilities.

        Returns:
            List of EventSubscription objects
        """
        if self._capability_loader:
            return self._capability_loader.get_subscriptions()
        return []

    def get_loaded_capabilities(self) -> Dict[str, LoadedCapability]:
        """Get all loaded capabilities.

        Returns:
            Dict mapping capability IDs to LoadedCapability objects
        """
        if self._capability_loader:
            return self._capability_loader.get_capabilities()
        return {}

    def set_simulation_mode(self, enabled: bool) -> None:
        """Enable or disable simulation mode.

        When enabled, all events emitted by capabilities will have
        simulated=True in the payload.

        Args:
            enabled: True to enable simulation mode
        """
        if self._capability_loader:
            self._capability_loader.simulation_mode = enabled

    @property
    def simulation_mode(self) -> bool:
        """Get simulation mode status."""
        if self._capability_loader:
            return self._capability_loader.simulation_mode
        return False
