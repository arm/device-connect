"""Decorators for defining device functions and events.

This module provides decorators that mark methods as device functions or
events. The decorators extract metadata from type hints and docstrings
to automatically generate JSON schemas for function parameters.

OpenTelemetry Integration:
    @rpc methods automatically create SERVER spans with metrics.
    @emit methods automatically create PRODUCER spans and inject
    W3C TraceContext into event payloads.
    Context propagation uses OTel's standard context mechanism
    via device_connect_sdk.telemetry (replacing the old custom trace_context).

Example:
    class CameraDriver(DeviceDriver):
        @rpc()
        async def capture_image(
            self,
            resolution: str = "1080p",
            format: str = "jpeg"
        ) -> dict:
            '''Capture an image from the camera.

            Args:
                resolution: Image resolution (e.g., '720p', '1080p', '4k')
                format: Output format ('jpeg', 'png', 'raw')

            Returns:
                Dictionary with base64-encoded image data
            '''
            return {"image_b64": "..."}

        @emit()  # Event name defaults to method name
        async def motion_detected(self, zone: str, confidence: float):
            '''Motion detected in camera view.

            Args:
                zone: Zone identifier
                confidence: Detection confidence (0.0 to 1.0)
            '''
            pass  # Optional pre-processing

        async def detection_loop(self):
            # Call decorated method to emit event
            await self.motion_detected(zone="A", confidence=0.95)
"""
from __future__ import annotations

import contextvars
import functools
import inspect
import logging
import re
import time
import uuid
from typing import Any, Callable, Dict, Optional, get_type_hints, get_origin, get_args

from device_connect_sdk.telemetry.tracer import get_tracer, get_current_trace_id, SpanKind, StatusCode
from device_connect_sdk.telemetry.metrics import get_metrics
from device_connect_sdk.telemetry.propagation import inject_into_payload


logger = logging.getLogger("device_connect.drivers")

# Context variable to track call origin (external, routine, internal)
# - "external": Called via messaging/RPC from outside (default)
# - "routine": Called from a @periodic routine
# - "internal": Called from another method on the same device
_call_origin: contextvars.ContextVar[str] = contextvars.ContextVar('call_origin', default='external')


class routine_context:
    """Context manager to mark calls as coming from a routine.

    Usage:
        async with routine_context():
            await some_rpc_method()  # Will log as RPC LOCAL instead of RPC EXEC
    """

    def __init__(self):
        self._token = None

    async def __aenter__(self):
        self._token = _call_origin.set("routine")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._token is not None:
            _call_origin.reset(self._token)
        return False


def set_call_origin(origin: str) -> contextvars.Token:
    """Set the call origin context and return a token to reset it.

    Args:
        origin: One of "external", "routine", "internal"

    Returns:
        Token to pass to reset_call_origin()
    """
    return _call_origin.set(origin)


def reset_call_origin(token: contextvars.Token) -> None:
    """Reset the call origin context using a token from set_call_origin()."""
    _call_origin.reset(token)


def _get_device_id(obj) -> str:
    """Get device_id from driver or capability.

    Handles both drivers and capabilities:
    - Drivers have _device (DeviceRuntime) and _device_id
    - Capabilities have .device (DeviceRuntime or driver, see capability_loader.py:230)

    Checks multiple sources in order of preference.
    """
    # 1. Direct _device_id attribute (set by DeviceRuntime._setup_agentic_driver)
    if hasattr(obj, "_device_id") and obj._device_id:
        return obj._device_id

    # 2. Through _device reference (driver -> DeviceRuntime)
    if hasattr(obj, "_device") and obj._device:
        device_id = getattr(obj._device, "device_id", None)
        if device_id:
            return device_id

    # 3. Through .device reference (capability -> DeviceRuntime or driver)
    if hasattr(obj, "device") and obj.device:
        ref = obj.device
        # If ref has device_id directly, it's a DeviceRuntime
        device_id = getattr(ref, "device_id", None)
        if device_id:
            return device_id
        # Otherwise it might be a driver - check _device
        if hasattr(ref, "_device") and ref._device:
            device_id = getattr(ref._device, "device_id", None)
            if device_id:
                return device_id

    # 4. Direct device_id attribute on obj
    if hasattr(obj, "device_id") and obj.device_id:
        return obj.device_id

    return "unknown"


# Type to JSON Schema mapping
_TYPE_MAP: Dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _python_type_to_json_schema(py_type: type) -> Dict[str, Any]:
    """Convert a Python type annotation to JSON Schema.

    Args:
        py_type: Python type (e.g., str, int, Optional[str], List[int])

    Returns:
        JSON Schema dictionary
    """
    origin = get_origin(py_type)

    # Handle Optional[T] (Union[T, None])
    if origin is type(None):
        return {"type": "null"}

    # Handle Optional (Union with None)
    if hasattr(py_type, "__origin__") and py_type.__origin__ is type(None):
        return {"type": "null"}

    # Check for Union types (includes Optional)
    try:
        from typing import Union
        if origin is Union:
            args = get_args(py_type)
            non_none_args = [a for a in args if a is not type(None)]
            if len(non_none_args) == 1:
                # This is Optional[T]
                schema = _python_type_to_json_schema(non_none_args[0])
                return schema
            # Multiple types - anyOf
            return {"anyOf": [_python_type_to_json_schema(a) for a in non_none_args]}
    except ImportError:
        pass

    # Handle List[T]
    if origin is list:
        args = get_args(py_type)
        if args:
            return {"type": "array", "items": _python_type_to_json_schema(args[0])}
        return {"type": "array"}

    # Handle Dict[K, V]
    if origin is dict:
        return {"type": "object"}

    # Simple types
    if py_type in _TYPE_MAP:
        return {"type": _TYPE_MAP[py_type]}

    # Fallback for unknown types
    return {"type": "string"}


def _parse_docstring(docstring: str | None) -> tuple[str, Dict[str, str]]:
    """Parse Google-style docstring to extract summary and argument descriptions.

    Args:
        docstring: The docstring to parse

    Returns:
        Tuple of (summary, {arg_name: description})
    """
    if not docstring:
        return "", {}

    lines = docstring.strip().split("\n")
    summary = lines[0].strip()
    arg_descriptions: Dict[str, str] = {}

    in_args = False
    current_arg: str | None = None

    for line in lines[1:]:
        stripped = line.strip()

        # Check for Args: section
        if stripped.lower().startswith("args:"):
            in_args = True
            continue

        # End of Args section
        if in_args and stripped.lower().startswith(("returns:", "raises:", "example:", "note:")):
            break

        if in_args:
            # Match argument definition: "arg_name: description" or "arg_name (type): description"
            match = re.match(r"^(\w+)(?:\s*\([^)]+\))?:\s*(.+)$", stripped)
            if match:
                current_arg = match.group(1)
                arg_descriptions[current_arg] = match.group(2)
            elif current_arg and stripped:
                # Continuation of previous arg description
                arg_descriptions[current_arg] += " " + stripped

    return summary, arg_descriptions


def _summarize_payload(payload: dict, max_len: int = 100) -> str:
    """Summarize payload for logging.

    - Excludes event_id and ts (already shown in log line)
    - Truncates large string values (e.g., image_b64)
    - Formats as key=value pairs for readability
    """
    parts = []
    for k, v in payload.items():
        # Skip fields already shown in log line
        if k in ("event_id", "ts"):
            continue
        # Truncate long strings
        if isinstance(v, str) and len(v) > max_len:
            v = f"{v[:20]}...({len(v)} chars)"
        # Format value
        if isinstance(v, str):
            parts.append(f"{k}={v!r}")
        else:
            parts.append(f"{k}={v}")
    return ", ".join(parts) if parts else "(no data)"


def _summarize_args(args: tuple, kwargs: dict) -> str:
    """Summarize function arguments for logging."""
    parts = [repr(a)[:50] for a in args]
    parts.extend(f"{k}={repr(v)[:50]}" for k, v in kwargs.items())
    return ", ".join(parts) if parts else "(none)"


def _summarize_result(result: Any, max_len: int = 200) -> str:
    """Summarize function result for logging.

    For dicts, shows key=value pairs with long values truncated.
    For other types, converts to string and truncates.
    """
    if isinstance(result, dict):
        parts = []
        for k, v in result.items():
            if isinstance(v, str) and len(v) > 50:
                v = f"{v[:20]}...({len(v)} chars)"
            elif isinstance(v, dict):
                # Nested dict - show keys only
                v = "{" + ", ".join(v.keys()) + "}"
            elif isinstance(v, list) and len(v) > 3:
                v = f"[{len(v)} items]"
            if isinstance(v, str):
                parts.append(f"{k}={v!r}")
            else:
                parts.append(f"{k}={v}")
        summary = ", ".join(parts)
        return summary if len(summary) <= max_len else summary[:max_len] + "..."
    else:
        s = str(result)
        return s[:max_len] + "..." if len(s) > max_len else s


def _get_integration_logger(obj: Any) -> Optional[Callable[[dict], None]]:
    """Get the log_integration method from a driver or capability.

    For integration testing in canary mode, we need to log RPC calls and
    event emissions. This helper finds the log_integration method whether
    we're in a driver (has log_integration directly) or a capability
    (has device reference that IS the driver).

    Args:
        obj: Either a driver instance or a capability instance

    Returns:
        The log_integration callable, or None if not available
    """
    # Direct driver with log_integration method
    if hasattr(obj, 'log_integration') and callable(obj.log_integration):
        return obj.log_integration

    # Capability: self.device is actually the driver reference
    # (see device_connect_server/drivers/capability_loader.py:230 - cap_class(device=device_ref))
    device = getattr(obj, 'device', None)
    if device and hasattr(device, 'log_integration') and callable(device.log_integration):
        return device.log_integration

    return None


def rpc(
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> Callable:
    """Decorator to expose a method as an RPC-callable function.

    The decorator extracts metadata from the method signature and docstring
    to generate a function definition with JSON Schema for parameters.
    It also wraps the method to provide automatic logging.

    Args:
        name: Override function name (default: method __name__)
        description: Override description (default: first line of docstring)

    Returns:
        Decorated method with function metadata attached

    Example:
        @rpc()
        async def my_function(self, param: str = "default") -> dict:
            '''Does something useful.

            Args:
                param: A parameter description
            '''
            return {"result": param}

        @rpc(name="customName", description="Custom description")
        async def another_function(self, x: int) -> dict:
            return {"x": x}
    """
    def decorator(func: Callable) -> Callable:
        func_name = name or func.__name__

        # Parse docstring for summary and arg descriptions
        summary, arg_docs = _parse_docstring(func.__doc__)
        func._description = description or summary
        func._arg_descriptions = arg_docs

        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs):
            tracer = get_tracer()
            metrics = get_metrics()
            trace_id = get_current_trace_id()[:12]
            device_id = _get_device_id(self)

            # Extract source_device from kwargs (injected by DeviceRuntime)
            source_device = kwargs.pop("source_device", None)

            # Build args summary (source_device already removed)
            args_summary = _summarize_args(args, kwargs)

            # Check call origin for logging
            origin = _call_origin.get()

            # OTel span attributes
            span_attrs = {
                "rpc.method": func_name,
                "device_connect.device.id": device_id,
                "device_connect.call_origin": origin,
            }
            if source_device:
                span_attrs["device_connect.source_device"] = source_device

            with tracer.start_as_current_span(
                f"rpc/{func_name}",
                kind=SpanKind.SERVER,
                attributes=span_attrs,
            ) as span:
                t0 = time.monotonic()
                metrics.rpc_active.add(1, {"device_connect.device.id": device_id})

                # Log entry
                if origin == "routine":
                    logger.info("RPC LOCAL [%s] %s::%s (from routine) args=%s", trace_id, device_id, func_name, args_summary)
                elif source_device:
                    logger.info("=" * 60)
                    logger.info(">>> RPC EXEC [%s] %s -> %s::%s", trace_id, source_device, device_id, func_name)
                    logger.info("    args: %s", args_summary)
                else:
                    logger.info("=" * 60)
                    logger.info(">>> RPC EXEC [%s] %s::%s", trace_id, device_id, func_name)
                    logger.info("    args: %s", args_summary)

                # Auto-log RPC call for integration testing
                integration_log = _get_integration_logger(self)
                if integration_log:
                    sig = inspect.signature(func)
                    params = [p for p in sig.parameters.keys() if p != "self"]
                    log_params = {}
                    for i, arg in enumerate(args):
                        if i < len(params):
                            log_params[params[i]] = arg
                    log_params.update(kwargs)
                    integration_log({
                        "type": "rpc_called",
                        "function": func_name,
                        "params": log_params
                    })

                status = "ok"
                try:
                    result = await func(self, *args, **kwargs)
                    result_summary = _summarize_result(result)

                    needs_border = origin != "routine"
                    if origin == "routine":
                        log_target = f"RPC LOCAL [{trace_id}] {device_id}::{func_name}"
                    elif source_device:
                        log_target = f"<<< RPC EXEC [{trace_id}] {source_device} -> {device_id}::{func_name}"
                    else:
                        log_target = f"<<< RPC EXEC [{trace_id}] {device_id}::{func_name}"

                    if isinstance(result, dict) and "error" in result:
                        logger.warning("%s -> PARTIAL: %s", log_target, result_summary)
                        status = "partial"
                    else:
                        logger.info("%s -> OK: %s", log_target, result_summary)

                    if needs_border:
                        logger.info("=" * 60)

                    span.set_status(StatusCode.OK)
                    return result
                except Exception as e:
                    status = "error"
                    needs_border = origin != "routine"
                    if origin == "routine":
                        log_target = f"RPC LOCAL [{trace_id}] {device_id}::{func_name}"
                    elif source_device:
                        log_target = f"<<< RPC EXEC [{trace_id}] {source_device} -> {device_id}::{func_name}"
                    else:
                        log_target = f"<<< RPC EXEC [{trace_id}] {device_id}::{func_name}"
                    logger.warning("%s -> FAILED: %s", log_target, e)
                    if needs_border:
                        logger.info("=" * 60)
                    if integration_log:
                        integration_log({
                            "type": "rpc_error",
                            "function": func_name,
                            "error": str(e)
                        })
                    span.record_exception(e)
                    span.set_status(StatusCode.ERROR, str(e))
                    raise
                finally:
                    duration_ms = (time.monotonic() - t0) * 1000
                    metric_attrs = {"rpc.method": func_name, "device_connect.device.id": device_id, "status": status}
                    metrics.rpc_duration.record(duration_ms, metric_attrs)
                    metrics.rpc_count.add(1, metric_attrs)
                    metrics.rpc_active.add(-1, {"device_connect.device.id": device_id})

        # Mark as device function
        wrapper._is_device_function = True
        wrapper._function_name = func_name
        wrapper._description = func._description
        wrapper._arg_descriptions = func._arg_descriptions
        wrapper._original_func = func  # For schema extraction

        return wrapper

    return decorator


def emit(
    name: Optional[str] = None,
    description: Optional[str] = None
) -> Callable:
    """Decorator to declare an event this driver can emit.

    The decorated method becomes callable - calling it emits the event.
    Event name defaults to the method name. The method body is executed
    before emission (allowing pre-processing), then the event is emitted
    with payload built from method arguments.

    Auto-added fields:
        - event_id: Unique 8-character hex ID
        - ts: ISO 8601 timestamp (UTC)

    Args:
        name: Override event name (default: method __name__)
        description: Event description (default: first line of docstring)

    Returns:
        Decorated method that emits event when called

    Example:
        @emit()
        async def state_change_detected(self, zone_id: str, state_class: str):
            '''State change detected in camera view.

            Args:
                zone_id: Zone where change was detected
                state_class: State class (mess, clean, etc.)
            '''
            pass  # Optional pre-processing

        # Later - call method to emit:
        await self.state_change_detected(zone_id="A", state_class="mess")
        # Emits: {"zone_id": "A", "state_class": "mess", "event_id": "abc123", "ts": "..."}
    """
    def decorator(func: Callable) -> Callable:
        event_name = name or func.__name__

        # Parse docstring for summary and arg descriptions
        summary, arg_docs = _parse_docstring(func.__doc__)
        func._event_description = description or summary
        func._payload_descriptions = arg_docs

        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs):
            tracer = get_tracer()
            metrics = get_metrics()
            device_id = _get_device_id(self)

            # Build payload from arguments
            sig = inspect.signature(func)
            params = [p for p in sig.parameters.keys() if p != "self"]
            payload = {}
            for i, arg in enumerate(args):
                if i < len(params):
                    payload[params[i]] = arg
            payload.update(kwargs)

            # Auto-add standard fields
            payload.setdefault("event_id", uuid.uuid4().hex[:8])
            payload.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

            with tracer.start_as_current_span(
                f"event/{event_name}",
                kind=SpanKind.PRODUCER,
                attributes={
                    "device_connect.event.name": event_name,
                    "device_connect.device.id": device_id,
                },
            ) as span:
                # Execute original method body (pre-processing)
                result = await func(self, *args, **kwargs)

                # Dispatch to internal handlers BEFORE pubsub emission
                # Handlers can suppress or modify the payload
                should_propagate, final_payload = await self._dispatch_internal_event(
                    event_name, payload
                )

                # Only emit to pubsub if not suppressed by internal handlers
                if should_propagate:
                    event_id = final_payload.get("event_id", "unknown")
                    payload_summary = _summarize_payload(final_payload)
                    logger.info("~" * 60)
                    logger.info("*** EVENT %s::%s [%s]", device_id, event_name, event_id)
                    logger.info("    payload: %s", payload_summary)
                    logger.info("~" * 60)

                    # Auto-log event emission for integration testing
                    integration_log = _get_integration_logger(self)
                    if integration_log:
                        # Create a copy without internal fields for cleaner logs
                        log_payload = {k: v for k, v in final_payload.items()
                                       if k not in ("event_id", "ts")}
                        integration_log({
                            "type": "event_emitted",
                            "event": event_name,
                            **log_payload
                        })

                    # Inject W3C TraceContext into event payload for propagation
                    inject_into_payload(final_payload)

                    await self._emit_event_internal(event_name, final_payload)
                    span.set_status(StatusCode.OK)
                    metrics.event_count.add(1, {"device_connect.event.name": event_name, "device_connect.device.id": device_id})
                else:
                    logger.debug("EVENT %s::%s suppressed by internal handler", device_id, event_name)

                return result

        # Mark as device event and preserve metadata
        wrapper._is_device_event = True
        wrapper._event_name = event_name
        wrapper._event_description = func._event_description
        wrapper._payload_descriptions = func._payload_descriptions
        wrapper._original_func = func  # For schema extraction

        return wrapper

    return decorator


def build_function_schema(func: Callable) -> Dict[str, Any]:
    """Build JSON Schema for a device function's parameters.

    Extracts type hints and default values from the function signature
    to generate a complete JSON Schema.

    Args:
        func: A function decorated with @rpc

    Returns:
        JSON Schema dictionary for the function parameters
    """
    # Use original function if available (wrapper stores it)
    original = getattr(func, "_original_func", func)
    sig = inspect.signature(original)
    hints = {}

    try:
        hints = get_type_hints(original)
    except Exception:
        # Type hints may not be resolvable
        pass

    properties: Dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        # Skip 'self' parameter
        if param_name == "self":
            continue

        prop: Dict[str, Any] = {}

        # Get type from hints
        if param_name in hints:
            prop.update(_python_type_to_json_schema(hints[param_name]))
        else:
            prop["type"] = "string"  # Default to string

        # Get description from docstring
        arg_docs = getattr(func, "_arg_descriptions", {})
        if param_name in arg_docs:
            prop["description"] = arg_docs[param_name]

        # Handle default value
        if param.default is not inspect.Parameter.empty:
            prop["default"] = param.default
        else:
            required.append(param_name)

        properties[param_name] = prop

    return {
        "type": "object",
        "properties": properties,
        "required": required
    }


def build_event_schema(func: Callable) -> Dict[str, Any]:
    """Build JSON Schema for a device event's payload.

    Extracts type hints from the event method's parameters to
    generate a schema for the event payload.

    Args:
        func: A function decorated with @emit

    Returns:
        JSON Schema dictionary for the event payload
    """
    # Use original function if available (wrapper stores it)
    original = getattr(func, "_original_func", func)
    sig = inspect.signature(original)
    hints = {}

    try:
        hints = get_type_hints(original)
    except Exception:
        pass

    properties: Dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name == "self":
            continue

        prop: Dict[str, Any] = {}

        if param_name in hints:
            prop.update(_python_type_to_json_schema(hints[param_name]))
        else:
            prop["type"] = "string"

        # Get description from docstring
        payload_docs = getattr(func, "_payload_descriptions", {})
        if param_name in payload_docs:
            prop["description"] = payload_docs[param_name]

        if param.default is not inspect.Parameter.empty:
            prop["default"] = param.default
        else:
            required.append(param_name)

        properties[param_name] = prop

    return {
        "type": "object",
        "properties": properties,
        "required": required
    }


def before_emit(
    event_name: str,
    suppress_propagation: bool = False,
) -> Callable:
    """Decorator to intercept events before they are emitted to pubsub.

    The handler is called BEFORE the event is sent to pubsub, allowing:
    - Local reaction without network roundtrip
    - Optional suppression of pubsub emission
    - Modification of payload before emission

    Handler return values:
    - None: Propagate with original payload
    - False: Suppress pubsub emission
    - dict: Propagate with modified payload

    Args:
        event_name: Name of the event to handle (matches @emit name)
        suppress_propagation: If True, event won't be emitted to pubsub

    Returns:
        Decorated method

    Example:
        @emit()
        async def mess_detected(self, zone: str, severity: str):
            pass

        @before_emit("mess_detected")
        async def on_mess_detected(self, zone: str, severity: str, **kwargs):
            '''React locally before pubsub emission.'''
            if severity in ("medium", "high"):
                await self.dispatch_cleaner(zone)

            # Suppress low severity from pubsub
            if severity == "low":
                return False

            # Optionally modify payload
            return {"zone": zone, "severity": severity, "handled": True, **kwargs}
    """
    def decorator(func: Callable) -> Callable:
        func._is_internal_handler = True
        func._internal_event_name = event_name
        func._suppress_propagation = suppress_propagation
        return func

    return decorator


def periodic(
    interval: float = 1.0,
    wait_for_completion: bool = True,
    start_on_connect: bool = True,
    name: Optional[str] = None,
) -> Callable:
    """Decorator to declare a periodic routine that runs on this device.

    The routine:
    - Auto-starts on connect() and auto-stops on disconnect()
    - Handles errors gracefully (logs, continues)
    - Can emit events, call other devices, etc.
    - Is discoverable for self-programming

    Args:
        interval: Seconds between invocations
        wait_for_completion: If True, waits for previous run to complete before
                            scheduling next (prevents overlap). If False, runs
                            strictly every `interval` seconds.
        start_on_connect: If True, auto-starts when device connects
        name: Optional routine name (defaults to function name)

    Returns:
        Decorated method

    Example:
        @periodic(interval=5.0, wait_for_completion=True)
        async def detection_loop(self) -> None:
            '''Analyze frame every 5s (waits if VLM takes longer).'''
            detection = await self._analyze_frame()
            if detection:
                await self.mess_detected(
                    zone=detection["zone"],
                    severity=detection["severity"]
                )

        @periodic(interval=60.0)
        async def health_check(self) -> None:
            '''Check device health every minute.'''
            if not self._is_healthy():
                await self.health_error(error="Device unhealthy")
    """
    def decorator(func: Callable) -> Callable:
        func._is_device_routine = True
        func._routine_interval = interval
        func._routine_wait_for_completion = wait_for_completion
        func._routine_start_on_connect = start_on_connect
        func._routine_name = name or func.__name__
        return func

    return decorator
