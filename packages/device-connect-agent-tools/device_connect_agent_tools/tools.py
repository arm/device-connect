"""Device Connect device operations — framework-agnostic tool functions.

Hierarchical discovery tools that keep LLM context small:

1. ``describe_fleet()``         — bird's-eye summary (types, locations, counts)
2. ``list_devices(...)``        — paginated compact roster (no schemas)
3. ``get_device_functions(id)`` — full schemas for ONE device
4. ``invoke_device(...)``       — call a function on a device

Plain Python functions with type hints and docstrings. Use them directly
or wrap with a framework adapter:

    # Plain Python
    from device_connect_agent_tools import connect, describe_fleet, list_devices
    connect()
    fleet = describe_fleet()
    devices = list_devices(device_type="camera")

    # Strands
    from device_connect_agent_tools.adapters.strands import (
        describe_fleet, list_devices, get_device_functions, invoke_device,
    )
    agent = Agent(tools=[describe_fleet, list_devices, get_device_functions, invoke_device])
"""

from __future__ import annotations

import logging
import os
import uuid
from collections import defaultdict
from typing import Any

from device_connect_agent_tools.connection import get_connection

logger = logging.getLogger(__name__)

# When the fleet (or filtered result set) has this many devices or fewer,
# describe_fleet() and list_devices() auto-include full function schemas
# so the agent can skip get_device_functions() and go straight to invoke.
# Set to 0 to disable auto-expansion.
SMALL_FLEET_THRESHOLD = int(os.getenv("DEVICE_CONNECT_SMALL_FLEET_THRESHOLD", "5"))


# ── Shared helpers ──────────────────────────────────────────────


def _full_device(d: dict) -> dict:
    """Build a full device dict with function schemas and events."""
    functions = d.get("functions", [])
    events = d.get("events", [])
    return {
        "device_id": d.get("device_id"),
        "device_type": d.get("device_type"),
        "location": d.get("location"),
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
    }


# ── Hierarchical discovery tools ─────────────────────────────────


def describe_fleet() -> dict[str, Any]:
    """Get a high-level summary of all available devices.

    Returns device counts grouped by type and location. Use this first
    to understand what devices are available, then call list_devices()
    to browse specific types or locations.

    For small fleets (≤ SMALL_FLEET_THRESHOLD devices), full device
    details including function schemas are included automatically — you
    can skip list_devices / get_device_functions and go straight to
    invoke_device.

    Returns:
        Dict with total_devices, total_functions, by_type, by_location.
        For small fleets, also includes "devices" with full schemas.

    Example:
        fleet = describe_fleet()
        # {"total_devices": 47, "by_type": {"camera": {"count": 12, ...}}, ...}
    """
    try:
        conn = get_connection()
        devices = conn.list_devices()

        by_type: dict[str, dict] = defaultdict(lambda: {"count": 0, "locations": set()})
        by_location: dict[str, dict] = defaultdict(lambda: {"count": 0, "types": set()})
        total_functions = 0

        for d in devices:
            dt = d.get("device_type") or "unknown"
            loc = d.get("location") or "unknown"
            funcs = d.get("functions", [])
            total_functions += len(funcs)

            by_type[dt]["count"] += 1
            by_type[dt]["locations"].add(loc)

            by_location[loc]["count"] += 1
            by_location[loc]["types"].add(dt)

        # Convert sets to sorted lists for JSON serialization
        result_by_type = {
            k: {"count": v["count"], "locations": sorted(v["locations"])}
            for k, v in sorted(by_type.items())
        }
        result_by_location = {
            k: {"count": v["count"], "types": sorted(v["types"])}
            for k, v in sorted(by_location.items())
        }

        result: dict[str, Any] = {
            "total_devices": len(devices),
            "total_functions": total_functions,
            "by_type": result_by_type,
            "by_location": result_by_location,
        }

        # Auto-expand: include full device details for small fleets
        if SMALL_FLEET_THRESHOLD > 0 and len(devices) <= SMALL_FLEET_THRESHOLD:
            result["devices"] = [_full_device(d) for d in devices]
            result["hint"] = (
                "Full device details included — skip list_devices / "
                "get_device_functions and go straight to invoke_device."
            )

        return result

    except Exception as e:
        logger.error("describe_fleet failed: %s", e)
        return {"total_devices": 0, "total_functions": 0, "by_type": {}, "by_location": {}}


def list_devices(
    device_type: str | None = None,
    location: str | None = None,
    status: str | None = None,
    group_by: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    """Browse available devices with filtering and pagination.

    Returns compact device summaries. When the result set is small
    (≤ SMALL_FLEET_THRESHOLD), full function schemas are included
    automatically so you can skip get_device_functions().

    Args:
        device_type: Filter by type (e.g., "camera", "robot"). Fuzzy matching.
        location: Filter by location (e.g., "lobby", "warehouse-A").
        status: Filter by availability (e.g., "online", "offline", "busy").
        group_by: Group results by "location" or "device_type". Returns grouped dict.
        offset: Skip first N devices (for pagination).
        limit: Max devices to return (default 20).

    Returns:
        Dict with devices list (or groups dict), total count, pagination info.

    Example:
        # Browse all cameras
        result = list_devices(device_type="camera")

        # Group by location
        result = list_devices(group_by="location")
    """
    try:
        conn = get_connection()
        devices = conn.list_devices(device_type=device_type, location=location)

        # Apply client-side fuzzy filtering for device_type (server may not support it yet)
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

        # Status filter
        if status:
            s = status.lower()
            devices = [
                d for d in devices
                if s in (d.get("status", {}).get("availability") or "").lower()
            ]

        total = len(devices)

        # Build device summaries — include schemas for small result sets
        def _summary(d: dict, expand: bool) -> dict:
            funcs = d.get("functions", [])
            result = {
                "device_id": d.get("device_id"),
                "device_type": d.get("device_type"),
                "location": d.get("location"),
                "status": (d.get("status", {}).get("availability") or "unknown"),
                "function_count": len(funcs),
                "function_names": [
                    f.get("name") if isinstance(f, dict) else f
                    for f in funcs
                ],
            }
            if expand:
                result["functions"] = [
                    {
                        "name": f.get("name") if isinstance(f, dict) else f,
                        "description": f.get("description", "") if isinstance(f, dict) else "",
                        "parameters": f.get("parameters", {}) if isinstance(f, dict) else {},
                    }
                    for f in funcs
                ]
            return result

        if group_by in ("location", "device_type"):
            expand = SMALL_FLEET_THRESHOLD > 0 and total <= SMALL_FLEET_THRESHOLD
            groups: dict[str, list] = defaultdict(list)
            for d in devices:
                key = d.get(group_by) or "unknown"
                groups[key].append(_summary(d, expand))
            return {"groups": dict(sorted(groups.items())), "total": total}

        # Paginate
        page = devices[offset:offset + limit]
        expand = SMALL_FLEET_THRESHOLD > 0 and len(page) <= SMALL_FLEET_THRESHOLD
        return {
            "devices": [_summary(d, expand) for d in page],
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < total,
        }

    except Exception as e:
        logger.error("list_devices failed: %s", e)
        return {"devices": [], "total": 0, "offset": 0, "limit": limit, "has_more": False}


def get_device_functions(device_id: str) -> dict[str, Any]:
    """Get full function schemas for a specific device.

    Call this after list_devices() to see what a device can do and
    what parameters each function accepts.

    Args:
        device_id: Device ID (e.g., "camera-001").

    Returns:
        Dict with device_id, device_type, location, functions (with schemas), events.

    Example:
        info = get_device_functions("camera-001")
        # {"device_id": "camera-001", "functions": [{"name": "capture_image", ...}]}
    """
    try:
        conn = get_connection()
        device = conn.get_device(device_id)
        if not device:
            return {"error": f"Device {device_id} not found"}
        return _full_device(device)
    except Exception as e:
        return {"error": str(e)}


# ── Invocation tools (unchanged) ─────────────────────────────────


def invoke_device(
    device_id: str,
    function: str,
    params: dict[str, Any] | None = None,
    llm_reasoning: str | None = None,
) -> dict[str, Any]:
    """Call a function on a Device Connect device.

    Args:
        device_id: Target device ID (e.g., "robot-001", "camera-001").
        function: Function name to call (e.g., "start_cleaning", "capture_image").
        params: Function parameters as a dictionary. Check get_device_functions() for schemas.
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


# ── Backward compatibility ───────────────────────────────────────


def discover_devices(
    device_type: str | None = None,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    """Discover available devices (deprecated — use list_devices instead).

    Returns all devices with their function schemas. For large fleets,
    prefer the hierarchical approach:
    1. describe_fleet() — see what's available
    2. list_devices(...) — browse with filters
    3. get_device_functions(id) — get schemas for one device

    Args:
        device_type: Optional filter (e.g., "robot", "camera"). Fuzzy matching.
        refresh: Force refresh from registry instead of cache.

    Returns:
        List of devices with device_id, device_type, functions, events.
    """
    try:
        conn = get_connection()
        # Invalidate cache when refresh is requested
        if refresh and conn._provider and hasattr(conn._provider, 'invalidate_cache'):
            conn._provider.invalidate_cache()
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
