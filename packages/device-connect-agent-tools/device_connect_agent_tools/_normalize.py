"""Shared device normalization helpers.

Operates on flat device dicts (already run through ``connection._flatten_device``
or equivalent). All callers flatten at the boundary so there is only one shape.
"""
from __future__ import annotations

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
    result: dict[str, Any] = {
        "device_id": d.get("device_id"),
        "device_type": d.get("device_type"),
        "location": d.get("location"),
        "function_count": len(funcs),
        "function_names": [
            name for f in funcs
            if (name := (f.get("name") if isinstance(f, dict) else f))
        ],
    }
    if expand:
        result["functions"] = _normalize_functions(funcs)
    return result
