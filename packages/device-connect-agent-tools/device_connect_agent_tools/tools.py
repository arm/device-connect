"""Device Connect device operations — framework-agnostic tool functions.

Plain Python functions with type hints and docstrings. Use them directly
or wrap with a framework adapter:

    # Plain Python
    from device_connect_agent_tools import connect, discover_devices, invoke_device
    connect()
    devices = discover_devices()

    # Strands
    from device_connect_agent_tools.adapters.strands import discover_devices, invoke_device
    agent = Agent(tools=[discover_devices, invoke_device])

    # LangChain
    from device_connect_agent_tools.adapters.langchain import discover_devices, invoke_device
    agent = create_react_agent(model, [discover_devices, invoke_device])
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from device_connect_agent_tools.connection import get_connection

logger = logging.getLogger(__name__)


def discover_devices(
    device_type: str | None = None,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    """Discover available Device Connect devices on the network.

    Returns all devices with their function schemas so you know what
    you can call on each device.

    Args:
        device_type: Optional filter (e.g., "robot", "camera"). Fuzzy matching.
        refresh: Force refresh from registry instead of cache.

    Returns:
        List of devices with device_id, device_type, functions, events.

    Example:
        devices = discover_devices()
        robots = discover_devices(device_type="robot")
    """
    try:
        conn = get_connection()
        devices = conn.list_devices(device_type=device_type)

        if device_type:
            t = device_type.lower().replace("_", "").replace("-", "")
            filtered = [
                d for d in devices
                if d.get("device_type")
                and (
                    t in d["device_type"].lower().replace("_", "").replace("-", "")
                    or d["device_type"].lower().replace("_", "").replace("-", "") in t
                )
            ]
            if filtered:
                devices = filtered
            else:
                devices = []

        results = []
        for d in devices:
            functions = d.get("functions", [])
            events = d.get("events", [])

            results.append({
                "device_id": d.get("device_id"),
                "device_type": d.get("device_type"),
                "location": d.get("location"),
                "status": d.get("status", {}),
                "functions": [
                    {
                        "name": f.get("name") if isinstance(f, dict) else f,
                        "description": f.get("description", "") if isinstance(f, dict) else "",
                        "parameters": f.get("parameters", {}) if isinstance(f, dict) else {},
                    }
                    for f in functions
                ],
                "events": [
                    e.get("name") if isinstance(e, dict) else e
                    for e in events
                ],
            })
        return results

    except Exception as e:
        logger.error("Discovery failed: %s", e)
        return []


def invoke_device(
    device_id: str,
    function: str,
    params: dict[str, Any] | None = None,
    llm_reasoning: str | None = None,
) -> dict[str, Any]:
    """Call a function on an Device Connect device.

    Args:
        device_id: Target device ID (e.g., "robot-001", "camera-001").
        function: Function name to call (e.g., "start_cleaning", "capture_image").
        params: Function parameters as a dictionary. Check discover_devices for schemas.
            Do NOT put llm_reasoning inside params.
        llm_reasoning: Why you're calling this function — for observability.

    Example:
        result = invoke_device(
            device_id="robot-001", function="start_cleaning",
            params={"zone": "zone-A"},
            llm_reasoning="Camera detected spill in zone-A"
        )
    """
    trace_id = f"trace-{uuid.uuid4().hex[:12]}"
    if llm_reasoning:
        truncated = llm_reasoning[:200] + "..." if len(llm_reasoning) > 200 else llm_reasoning
        logger.info("[%s] [%s::%s] Reason: %s", trace_id, device_id, function, truncated)

    try:
        conn = get_connection()
        clean = {k: v for k, v in (params or {}).items() if k != "llm_reasoning"}
        response = conn.invoke(device_id, function, params=clean)

        if "error" in response:
            error = response["error"]
            return {"success": False, "error": error.get("message", str(error))}
        return {"success": True, "result": response.get("result", {})}

    except Exception as e:
        logger.error("[%s] %s::%s -> ERROR: %s", trace_id, device_id, function, e)
        return {"success": False, "error": str(e)}


def invoke_device_with_fallback(
    device_ids: list[str],
    function: str,
    params: dict[str, Any] | None = None,
    llm_reasoning: str | None = None,
) -> dict[str, Any]:
    """Call a function with automatic fallback to other devices.

    Tries each device in order until one succeeds.

    Args:
        device_ids: List of device IDs to try, in order of preference.
        function: Function name to call.
        params: Function parameters dict. Do NOT put llm_reasoning inside params.
        llm_reasoning: Decision rationale for observability.
    """
    if llm_reasoning:
        logger.info("[fallback::%s] Reason: %s", function, llm_reasoning[:200])

    errors = []
    clean = {k: v for k, v in (params or {}).items() if k != "llm_reasoning"}

    for device_id in device_ids:
        try:
            conn = get_connection()
            response = conn.invoke(device_id, function, params=clean)
            if "error" not in response:
                return {
                    "success": True,
                    "device_id": device_id,
                    "result": response.get("result", {}),
                }
            errors.append({"device_id": device_id, "error": str(response["error"])})
        except Exception as e:
            logger.warning("Device %s failed: %s", device_id, e)
            errors.append({"device_id": device_id, "error": str(e)})

    return {"success": False, "error": "All devices failed", "failed_devices": errors}


def get_device_status(device_id: str) -> dict[str, Any]:
    """Get detailed status of a specific device.

    Args:
        device_id: Device ID to query (e.g., "robot-001").
    """
    try:
        conn = get_connection()
        device = conn.get_device(device_id)
        if not device:
            return {"error": f"Device {device_id} not found"}

        return {
            "device_id": device.get("device_id"),
            "device_type": device.get("device_type"),
            "location": device.get("location"),
            "status": device.get("status", {}),
            "functions": [
                f.get("name") if isinstance(f, dict) else f
                for f in device.get("functions", [])
            ],
        }
    except Exception as e:
        return {"error": str(e)}
