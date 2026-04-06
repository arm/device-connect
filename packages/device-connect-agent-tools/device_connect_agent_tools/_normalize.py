"""Shared device normalization helpers.

Operates on flat device dicts (already run through ``connection.flatten_device``
or equivalent). All callers flatten at the boundary so there is only one shape.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def _normalize_functions(functions: list) -> list[dict[str, Any]]:
    """Normalize a list of function defs to {name, description, parameters}."""
    result = []
    for f in functions:
        name = f.get("name") if isinstance(f, dict) else f
        if not name:
            continue
        result.append({
            "name": name,
            "description": f.get("description", "") if isinstance(f, dict) else "",
            "parameters": f.get("parameters", {}) if isinstance(f, dict) else {},
        })
    return result


def _normalize_events(events: list) -> list[str]:
    """Normalize event list to plain names."""
    return [
        name for e in events
        if (name := (e.get("name") if isinstance(e, dict) else e))
    ]


def extract_status(d: dict, default: str = "unknown") -> str:
    """Extract device availability/state as a string."""
    status = d.get("status")
    if not isinstance(status, dict):
        return default
    return status.get("availability") or status.get("state") or default


def fuzzy_filter_by_type(devices: list[dict], device_type: str) -> list[dict]:
    """Filter devices whose type contains the given query as a substring.

    Both the query and each device's ``device_type`` are normalised by
    lower-casing and stripping ``_`` and ``-`` characters before the
    substring check.  The query must be a substring of the device type,
    not the other way around (e.g. ``"sensor"`` matches
    ``"temperature_sensor"`` but ``"temperature_sensor"`` does not match
    ``"sensor"``).
    """
    t = device_type.lower().replace("_", "").replace("-", "")
    return [
        d for d in devices
        if d.get("device_type")
        and t in d["device_type"].lower().replace("_", "").replace("-", "")
    ]


def full_device(d: dict) -> dict[str, Any]:
    """Build a full device dict with function schemas and events."""
    return {
        "device_id": d.get("device_id"),
        "device_type": d.get("device_type"),
        "location": d.get("location"),
        "functions": _normalize_functions(d.get("functions", [])),
        "events": _normalize_events(d.get("events", [])),
    }


def compact_device(d: dict, expand: bool = False) -> dict[str, Any]:
    """Build a compact device summary, optionally with full function schemas."""
    funcs = d.get("functions", [])
    names = [
        name for f in funcs
        if (name := (f.get("name") if isinstance(f, dict) else f))
    ]
    result: dict[str, Any] = {
        "device_id": d.get("device_id"),
        "device_type": d.get("device_type"),
        "location": d.get("location"),
        "function_count": len(names),
        "function_names": names,
    }
    if expand:
        result["functions"] = _normalize_functions(funcs)
    return result


def aggregate_fleet(devices: list[dict]) -> dict[str, Any]:
    """Aggregate devices into by_type/by_location summary.

    Returns dict with total_devices, total_functions, by_type, by_location.
    """
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

    return {
        "total_devices": len(devices),
        "total_functions": total_functions,
        "by_type": {
            k: {"count": v["count"], "locations": sorted(v["locations"])}
            for k, v in sorted(by_type.items())
        },
        "by_location": {
            k: {"count": v["count"], "types": sorted(v["types"])}
            for k, v in sorted(by_location.items())
        },
    }


def group_devices(
    devices: list[dict],
    group_by: str,
    expand: bool,
) -> dict[str, Any]:
    """Group devices by a field, returning {groups: ..., total: ...}.

    Each device is summarized via ``compact_device`` (with status).
    """
    groups: dict[str, list] = defaultdict(list)
    for d in devices:
        summary = compact_device(d, expand)
        summary["status"] = extract_status(d)
        key = d.get(group_by) or "unknown"
        groups[key].append(summary)
    return {"groups": dict(sorted(groups.items())), "total": len(devices)}
