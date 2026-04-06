"""Capability sidecar runtime — entry point inside each capability container.

This module runs inside an OCI container and:
1. Connects to the per-device Zenoh router
2. Loads the single capability class from the container's Python module
3. Subscribes to command topic, responds with JSON-RPC 2.0
4. Publishes events on capability event topic
5. Runs @periodic routines internally
6. Publishes health status on health topic

Usage:
    # As container entrypoint:
    python -m device_connect_container.sidecar_runtime

Environment variables:
    ZENOH_ROUTER_ENDPOINT   — Local Zenoh router address (default: tcp/localhost:7447)
    CAPABILITY_DIR          — Path to capability directory with manifest.json
    DEVICE_ID               — Parent device ID
    TENANT                  — Tenant namespace (default: "default")
    MTE_ENABLED             — Enable Arm MTE for memory safety (default: false)
"""

import asyncio
import importlib
import importlib.util
import inspect
import json
import logging
import os
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class CapabilitySidecarRuntime:
    """Runtime that runs a single capability inside a container.

    Connects to the per-device Zenoh router and handles JSON-RPC commands
    targeting this capability, forwarding results back over Zenoh.
    """

    def __init__(
        self,
        capability_dir: Path,
        device_id: str,
        tenant: str = "default",
        zenoh_endpoint: str = "tcp/localhost:7447",
    ):
        self._capability_dir = Path(capability_dir)
        self._device_id = device_id
        self._tenant = tenant
        self._zenoh_endpoint = zenoh_endpoint

        self._manifest: Optional[dict] = None
        self._capability_id: Optional[str] = None
        self._cap_instance: Optional[Any] = None
        self._functions: Dict[str, Callable] = {}
        self._function_schemas: Dict[str, dict] = {}
        self._routines: Dict[str, asyncio.Task] = {}
        self._messaging = None
        self._cmd_subscription = None
        self._health_task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()

    @property
    def cmd_subject(self) -> str:
        return f"device-connect.{self._tenant}.{self._device_id}.cap.{self._capability_id}.cmd"

    @property
    def event_subject_prefix(self) -> str:
        return f"device-connect.{self._tenant}.{self._device_id}.cap.{self._capability_id}.event"

    @property
    def health_subject(self) -> str:
        return f"device-connect.{self._tenant}.{self._device_id}.cap.{self._capability_id}.health"

    async def run(self) -> None:
        """Main entry point: load capability, connect, serve forever."""
        # Load manifest and capability class
        self._load_capability()

        # Connect to local Zenoh router
        await self._connect_messaging()

        # Subscribe to command topic
        self._cmd_subscription = await self._messaging.subscribe(
            self.cmd_subject,
            self._on_command,
        )

        # Start @periodic routines
        self._start_routines()

        # Start health publisher
        self._health_task = asyncio.create_task(self._health_loop())

        logger.info(
            "Sidecar ready: capability=%s device=%s cmd=%s",
            self._capability_id,
            self._device_id,
            self.cmd_subject,
        )

        # Run until stopped
        try:
            await self._stopped.wait()
        finally:
            await self._shutdown()

    def _load_capability(self) -> None:
        """Load the capability class from the capability directory."""
        manifest_file = self._capability_dir / "manifest.json"
        if not manifest_file.exists():
            raise FileNotFoundError(f"No manifest.json in {self._capability_dir}")

        with open(manifest_file) as f:
            self._manifest = json.load(f)

        self._capability_id = self._manifest["id"]
        class_name = self._manifest["class_name"]
        entry_point = self._manifest.get("entry_point", "capability.py")

        # Load the Python module
        cap_file = self._capability_dir / entry_point
        if not cap_file.exists():
            raise FileNotFoundError(f"Entry point not found: {cap_file}")

        spec = importlib.util.spec_from_file_location(
            f"capability_{self._capability_id}", cap_file,
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not create module spec for {cap_file}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        # Instantiate capability (pass None for device — sidecars use messaging)
        cap_class = getattr(module, class_name)
        self._cap_instance = cap_class(device=None)

        # Collect @rpc functions
        self._collect_functions()

        # Wire @emit methods
        self._setup_events()

        # Call start() lifecycle hook
        if hasattr(self._cap_instance, "start"):
            if inspect.iscoroutinefunction(self._cap_instance.start):
                asyncio.get_event_loop().run_until_complete(self._cap_instance.start())
            else:
                self._cap_instance.start()

        logger.info(
            "Loaded capability: %s (functions=%s)",
            self._capability_id,
            list(self._functions.keys()),
        )

    def _collect_functions(self) -> None:
        """Scan the capability instance for @rpc decorated methods."""
        from device_connect_edge.drivers.decorators import build_function_schema

        for attr_name in dir(self._cap_instance):
            if attr_name.startswith("_"):
                continue
            try:
                attr = getattr(self._cap_instance, attr_name)
                if callable(attr) and getattr(attr, "_is_device_function", False):
                    func_name = getattr(attr, "_function_name", attr_name)
                    self._functions[func_name] = attr

                    # Build schema for _describe response
                    description = getattr(attr, "_description", "") or ""
                    if not description and attr.__doc__:
                        description = attr.__doc__.strip().split("\n")[0]
                    parameters = build_function_schema(attr)
                    self._function_schemas[func_name] = {
                        "description": description,
                        "parameters": parameters,
                    }
            except Exception as e:
                logger.warning("Error inspecting %s: %s", attr_name, e)

    def _setup_events(self) -> None:
        """Wire @emit methods to publish events over Zenoh."""
        cap = self._cap_instance

        async def dispatch_internal_event(event_name: str, payload: dict):
            return (True, payload)

        async def emit_event_internal(event_name: str, payload: dict):
            """Publish event to the capability's event topic."""
            if self._messaging:
                subject = f"{self.event_subject_prefix}.{event_name}"
                await self._messaging.publish(
                    subject, json.dumps(payload).encode(),
                )

        cap._dispatch_internal_event = dispatch_internal_event
        cap._emit_event_internal = emit_event_internal

        if hasattr(cap, "set_event_callback"):
            cap.set_event_callback(
                lambda name, payload: asyncio.create_task(emit_event_internal(name, payload))
            )
        elif hasattr(cap, "_event_callback"):
            cap._event_callback = (
                lambda name, payload: asyncio.create_task(emit_event_internal(name, payload))
            )

    async def _connect_messaging(self) -> None:
        """Connect to the per-device Zenoh router."""
        from device_connect_edge.messaging import create_client

        self._messaging = create_client("zenoh")
        await self._messaging.connect(
            servers=[self._zenoh_endpoint],
        )
        logger.info("Connected to Zenoh router: %s", self._zenoh_endpoint)

    def _on_command(self, data: bytes, reply_subject: Optional[str]) -> None:
        """Handle incoming JSON-RPC command from the device runtime.

        Args:
            data: JSON-RPC request bytes.
            reply_subject: Zenoh reply subject for sending the response.
                This matches the ZenohAdapter callback signature:
                callback(data: bytes, reply: Optional[str]).
        """
        asyncio.create_task(self._handle_command(data, reply_subject))

    async def _handle_command(self, data: bytes, reply_subject: Optional[str] = None) -> None:
        """Process a JSON-RPC request and send the response.

        Args:
            data: JSON-RPC request bytes.
            reply_subject: Where to send the response. If None, publishes
                back to the command subject (fallback).
        """
        response_topic = reply_subject or self.cmd_subject

        try:
            request = json.loads(data.decode())
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON in command: %s", e)
            return

        req_id = request.get("id", "unknown")
        method = request.get("method", "")
        params = request.get("params", {})

        # Handle built-in _describe method
        if method == "_describe":
            response = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "capability_id": self._capability_id,
                    "functions": self._function_schemas,
                },
            }
            await self._messaging.publish(response_topic, json.dumps(response).encode())
            return

        # Dispatch to capability function
        if method not in self._functions:
            error_response = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {method}",
                },
            }
            await self._messaging.publish(response_topic, json.dumps(error_response).encode())
            return

        try:
            # Remove internal metadata from params before calling
            clean_params = {
                k: v for k, v in params.items() if not k.startswith("_dc_")
            }
            func = self._functions[method]
            if inspect.iscoroutinefunction(func):
                result = await func(**clean_params)
            else:
                result = func(**clean_params)

            response = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": result,
            }
        except Exception as e:
            logger.error("Error executing %s: %s", method, e, exc_info=True)
            response = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(e)},
            }

        await self._messaging.publish(response_topic, json.dumps(response).encode())

    def _start_routines(self) -> None:
        """Start @periodic routines for the capability."""
        cap = self._cap_instance
        for attr_name in dir(cap):
            if attr_name.startswith("_"):
                continue
            try:
                attr = getattr(cap, attr_name)
                if callable(attr) and getattr(attr, "_is_device_routine", False):
                    interval = getattr(attr, "_routine_interval", 1.0)
                    wait_for_completion = getattr(attr, "_routine_wait_for_completion", True)
                    task = asyncio.create_task(
                        self._run_routine(attr_name, attr, interval, wait_for_completion)
                    )
                    self._routines[attr_name] = task
                    logger.debug("Started routine: %s (interval=%ss)", attr_name, interval)
            except Exception as e:
                logger.warning("Error collecting routine %s: %s", attr_name, e)

    async def _run_routine(
        self,
        name: str,
        routine: Callable,
        interval: float,
        wait_for_completion: bool = True,
    ) -> None:
        """Run a periodic routine."""
        while True:
            start_time = asyncio.get_running_loop().time()
            try:
                if inspect.iscoroutinefunction(routine):
                    await routine()
                else:
                    routine()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Routine error in %s: %s", name, e)

            if wait_for_completion:
                elapsed = asyncio.get_running_loop().time() - start_time
                await asyncio.sleep(max(0, interval - elapsed))
            else:
                await asyncio.sleep(interval)

    async def _health_loop(self) -> None:
        """Periodically publish health status."""
        while True:
            try:
                health = {
                    "capability_id": self._capability_id,
                    "healthy": True,
                    "functions": list(self._functions.keys()),
                    "ts": time.time(),
                }
                await self._messaging.publish(
                    self.health_subject,
                    json.dumps(health).encode(),
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Health publish failed: %s", e)
            await asyncio.sleep(5.0)

    async def _shutdown(self) -> None:
        """Clean shutdown."""
        # Cancel routines
        for name, task in self._routines.items():
            task.cancel()
        if self._routines:
            await asyncio.gather(*self._routines.values(), return_exceptions=True)
        self._routines.clear()

        # Cancel health task
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        # Unsubscribe
        if self._cmd_subscription:
            await self._cmd_subscription.unsubscribe()

        # Stop capability
        if self._cap_instance and hasattr(self._cap_instance, "stop"):
            try:
                if inspect.iscoroutinefunction(self._cap_instance.stop):
                    await self._cap_instance.stop()
                else:
                    self._cap_instance.stop()
            except Exception as e:
                logger.error("Error stopping capability: %s", e)

        # Close messaging
        if self._messaging:
            await self._messaging.close()

        logger.info("Sidecar shutdown complete: %s", self._capability_id)

    def stop(self) -> None:
        """Signal the runtime to stop."""
        self._stopped.set()


async def _main() -> None:
    """Entry point for the sidecar container."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    )

    # Enable MTE if requested (Phase 3)
    if os.getenv("MTE_ENABLED", "false").lower() in ("1", "true", "yes"):
        try:
            from device_connect_container.security.mte import enable_mte_for_process
            enable_mte_for_process()
            logger.info("Arm MTE enabled for this process")
        except Exception as e:
            logger.warning("Failed to enable MTE: %s", e)

    capability_dir = Path(os.getenv("CAPABILITY_DIR", "/app/capability"))
    device_id = os.getenv("DEVICE_ID", "unknown")
    tenant = os.getenv("TENANT", "default")
    zenoh_endpoint = os.getenv("ZENOH_ROUTER_ENDPOINT", "tcp/localhost:7447")

    runtime = CapabilitySidecarRuntime(
        capability_dir=capability_dir,
        device_id=device_id,
        tenant=tenant,
        zenoh_endpoint=zenoh_endpoint,
    )

    # Handle SIGTERM for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, runtime.stop)

    await runtime.run()


def main() -> None:
    """Sync entry point."""
    asyncio.run(_main())


if __name__ == "__main__":
    main()
