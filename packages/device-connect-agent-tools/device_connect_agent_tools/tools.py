# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Device Connect device operations — framework-agnostic tool functions.

Discovery is selector-driven. ``discover()`` and ``discover_labels()`` cover
both fleet-wide and entity-scoped queries; the older ``describe_fleet`` /
``list_devices`` / ``get_device_functions`` trio remains as advisory-deprecated
wrappers for one release while callers migrate.

    from device_connect_agent_tools import connect, discover, discover_labels
    connect()
    cams = discover("device(category:camera)")
    rgb_writes = discover("device(*).function(direction:write, modality:rgb)")
    vocab = discover_labels()
"""

from __future__ import annotations

import logging
import os
import time
import uuid
import warnings
from typing import Any

from device_connect_edge.selector import (
    Scope,
    Selector,
    SelectorParseError,
    parse_selector,
)
from device_connect_agent_tools.connection import get_connection
from device_connect_agent_tools._normalize import (
    full_device, compact_device, fuzzy_filter_by_type, extract_status,
    aggregate_fleet, group_devices,
    label_histogram,
)

logger = logging.getLogger(__name__)

# When the fleet (or filtered result set) has this many devices or fewer,
# describe_fleet() and list_devices() auto-include full function schemas
# so the agent can skip get_device_functions() and go straight to invoke.
# Set to 0 to disable auto-expansion.
try:
    SMALL_FLEET_THRESHOLD = min(max(int(os.getenv("DEVICE_CONNECT_SMALL_FLEET_THRESHOLD", "5")), 0), 100)
except (ValueError, TypeError):
    logger.warning(
        "Invalid DEVICE_CONNECT_SMALL_FLEET_THRESHOLD value %r, defaulting to 5",
        os.getenv("DEVICE_CONNECT_SMALL_FLEET_THRESHOLD"),
    )
    SMALL_FLEET_THRESHOLD = 5

# When ``discover()`` resolves a selector to this many functions or events
# or fewer, the response includes full schemas inline. Above the threshold
# it returns a compact ``(device_id, name, labels)`` summary so the agent
# can narrow further via ``discover_labels()`` or a tighter selector.
try:
    DC_FUNCTION_THRESHOLD = min(max(int(os.getenv("DEVICE_CONNECT_FUNCTION_THRESHOLD", "20")), 0), 200)
except (ValueError, TypeError):
    logger.warning(
        "Invalid DEVICE_CONNECT_FUNCTION_THRESHOLD value %r, defaulting to 20",
        os.getenv("DEVICE_CONNECT_FUNCTION_THRESHOLD"),
    )
    DC_FUNCTION_THRESHOLD = 20

# Multi-axis ``discover_labels()`` and ``discover()`` ``label_histogram``
# crop each key's values to this many entries (the highest-frequency
# values), advertising the cropped count via a sibling ``more`` field.
# Per the discovery design doc §3 "Scale handling": the multi-axis form
# is meant to be self-limiting; agents needing the full value list use
# ``discover_labels(key=...)`` which paginates instead.
#
# Stored unclamped so test patches see what they set; ``_format_label_histogram``
# clamps to ``[1, 200]`` at use to keep invariants regardless of override path.
try:
    LABEL_VALUES_TOP_N = int(os.getenv("DEVICE_CONNECT_LABEL_VALUES_TOP_N", "20"))
except (ValueError, TypeError):
    logger.warning(
        "Invalid DEVICE_CONNECT_LABEL_VALUES_TOP_N value %r, defaulting to 20",
        os.getenv("DEVICE_CONNECT_LABEL_VALUES_TOP_N"),
    )
    LABEL_VALUES_TOP_N = 20

# Hard ceiling on per-call ``limit`` to prevent runaway responses in large
# fleets. A caller asking for limit=100000 still gets at most this many
# rows per page (with ``next_offset`` to continue).
DISCOVER_HARD_LIMIT = 1000

# Default limits per the discovery design (different defaults for the two
# tools because they answer different questions: ``discover`` returns rows,
# ``discover_labels`` returns vocabulary).
DEFAULT_DISCOVER_LIMIT = 200
DEFAULT_DISCOVER_LABELS_LIMIT = 50


# ── Shared helpers ──────────────────────────────────────────────


def _normalize_pagination(offset: int, limit: int, default_limit: int) -> tuple[int, int]:
    """Clamp offset and limit to safe ranges.

    Negative offset rounds to 0, non-positive limit falls back to the default,
    and limit is capped at ``DISCOVER_HARD_LIMIT``.
    """
    safe_offset = max(0, int(offset or 0))
    if not limit or limit <= 0:
        safe_limit = default_limit
    else:
        safe_limit = min(int(limit), DISCOVER_HARD_LIMIT)
    return safe_offset, safe_limit


def _error(code: str, message: str) -> dict[str, str]:
    """Build the canonical structured error object.

    Errors are returned as data (not raised) inside the response envelope.
    The ``code`` is a stable, machine-readable string callers may switch on;
    ``message`` is human-readable and may include positional detail (parse
    caret, axis name, etc.) suitable for logging or surfacing to the user.

    Codes currently emitted:
        - ``selector_parse_error``    selector string is malformed
        - ``invalid_selector``        selector is not a usable input
                                      (None, non-string, etc.)
        - ``connection_error``        registry / messaging unavailable
        - ``key_not_axis_qualified``  discover_labels key missing axis prefix
        - ``unknown_axis``            discover_labels axis not in
                                      {device, function, event}
    """
    return {"code": code, "message": message}


def _empty_envelope(
    scope: str | None = None, error: dict[str, str] | None = None
) -> dict[str, Any]:
    """Build the canonical zero-result response envelope."""
    out: dict[str, Any] = {
        "matched": 0,
        "returned": 0,
        "offset": 0,
        "next_offset": None,
        "results": [],
    }
    if scope is not None:
        out["scope"] = scope
    if error is not None:
        out["error"] = error
    return out


def _paginate(items: list, offset: int, limit: int) -> tuple[list, int | None]:
    """Slice ``items`` to one page; return ``(page, next_offset)``."""
    end = offset + limit
    page = items[offset:end]
    next_offset = end if end < len(items) else None
    return page, next_offset


def _device_summary_for_discover(d: dict, expand: bool) -> dict[str, Any]:
    """Compact device row for ``discover()``, with labels surfaced."""
    summary = compact_device(d, expand)
    summary["status"] = extract_status(d)
    summary["labels"] = d.get("labels")
    return summary


def _function_row(d: dict, fn: dict, expand: bool) -> dict[str, Any]:
    """Build one row for a function-scoped discover result.

    Below the threshold, ``expand`` is True and the row includes the full
    JSON Schema. Above threshold, only name + labels travel back so the
    agent can narrow without paying for parameter schemas.
    """
    name = fn.get("name") if isinstance(fn, dict) else fn
    labels = fn.get("labels") if isinstance(fn, dict) else None
    if expand and isinstance(fn, dict):
        return {
            "device_id": d.get("device_id"),
            "name": name,
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
            "labels": labels,
        }
    return {
        "device_id": d.get("device_id"),
        "name": name,
        "labels": labels,
    }


def _event_row(d: dict, ev: dict, expand: bool) -> dict[str, Any]:
    """Build one row for an event-scoped discover result."""
    name = ev.get("name") if isinstance(ev, dict) else ev
    labels = ev.get("labels") if isinstance(ev, dict) else None
    if expand and isinstance(ev, dict):
        return {
            "device_id": d.get("device_id"),
            "name": name,
            "description": ev.get("description", ""),
            "payload_schema": ev.get("payload_schema"),
            "labels": labels,
        }
    return {
        "device_id": d.get("device_id"),
        "name": name,
        "labels": labels,
    }


# ── Selector-driven discovery (preferred) ────────────────────────


def discover(
    selector: str,
    offset: int = 0,
    limit: int = DEFAULT_DISCOVER_LIMIT,
) -> dict[str, Any]:
    """Resolve a selector to matched devices, functions, or events.

    The selector DSL supports five scope shapes:

        device(<filters>)                        all matching devices
        device(<filters>).function(<filters>)    RPCs on a device subset
        device(<filters>).event(<filters>)       events on a device subset
        function(<filters>)                      all RPCs across the fleet
        event(<filters>)                         all events across the fleet

    Inside ``(...)``: ``key:value``, ``key:[v1,v2]`` (OR within a key),
    ``key:pattern*`` (glob), ``k1:v1,k2:v2`` (AND across keys), bare-string
    id/name match, or ``*`` to match all.

    Matching is case-sensitive on both keys and values
    (``category:Camera`` and ``category:camera`` are not equivalent). Use
    lowercase by convention.

    Args:
        selector: A selector expression string.
        offset: Pagination offset (rows skipped).
        limit: Max rows per page (capped at DISCOVER_HARD_LIMIT).

    Returns:
        A response envelope:
        ``{"scope", "matched", "returned", "offset", "next_offset", "results",
        "label_histogram"}``.
        ``label_histogram`` is the per-key vocabulary across the **matched**
        set (pre-pagination), not the returned page; on the device axis it
        tracks unique device counts per key (``unique_devices``), on
        function/event axes it counts occurrences (a function appearing on N
        devices contributes N entries).
        For function- and event-scoped selectors, ``results`` rows include
        full schemas when the matched count is at or below
        ``DC_FUNCTION_THRESHOLD``; otherwise rows are name-and-labels summaries.

    Example:
        >>> discover("device(category:camera, location:zone-A/*)")
        {"scope": "device_only", "matched": 4, ...}
        >>> discover("device(*).function(direction:write, modality:rgb)")
        {"scope": "device_function", "matched": 8, ...}
    """
    safe_offset, safe_limit = _normalize_pagination(offset, limit, DEFAULT_DISCOVER_LIMIT)

    # Parse the selector at the system boundary; surface a clean error to
    # the caller rather than raising into agent code.
    if not isinstance(selector, str):
        return _empty_envelope(
            error=_error(
                "invalid_selector",
                f"Selector must be a string, got {type(selector).__name__}",
            )
        )
    try:
        sel: Selector = parse_selector(selector)
    except SelectorParseError as e:
        return _empty_envelope(error=_error("selector_parse_error", str(e)))

    try:
        conn = get_connection()
        devices = conn.list_devices()
    except Exception as e:
        logger.error("discover(%r) failed loading fleet: %s", selector, e)
        return _empty_envelope(
            scope=sel.scope.value, error=_error("connection_error", str(e))
        )

    # Apply the device-axis filter (vacuously True when sel.device is None).
    matched_devices = [
        d for d in devices
        if sel.device is None
        or sel.device.matches(d.get("device_id") or "", d.get("labels"))
    ]

    # Branch on scope. Each branch produces (results_full, page, histogram, total).
    if sel.scope == Scope.DEVICE_ONLY:
        total = len(matched_devices)
        page_devices, next_offset = _paginate(matched_devices, safe_offset, safe_limit)
        expand = SMALL_FLEET_THRESHOLD > 0 and total <= SMALL_FLEET_THRESHOLD
        results = [_device_summary_for_discover(d, expand) for d in page_devices]
        histogram, multivalued, unique = label_histogram(matched_devices, count_unique=True)
        formatted_histogram = _format_label_histogram(histogram, multivalued, unique)
        return {
            "scope": sel.scope.value,
            "matched": total,
            "returned": len(results),
            "offset": safe_offset,
            "next_offset": next_offset,
            "results": results,
            "label_histogram": formatted_histogram,
        }

    # Function- or event-scoped selectors enumerate (device, entity) tuples.
    is_function_scope = sel.scope in (Scope.DEVICE_FUNCTION, Scope.FUNCTION_ONLY)
    entity_filter = sel.function if is_function_scope else sel.event

    matched_rows: list[tuple[dict, dict]] = []
    for d in matched_devices:
        entities = d.get("functions" if is_function_scope else "events", [])
        for entity in entities:
            if not isinstance(entity, dict):
                # Best-effort: lift bare-name list items into a stub dict so the
                # filter can still match by name.
                entity = {"name": str(entity), "labels": None}
            if entity_filter is None or entity_filter.matches(
                entity.get("name") or "", entity.get("labels")
            ):
                matched_rows.append((d, entity))

    total = len(matched_rows)
    page_rows, next_offset = _paginate(matched_rows, safe_offset, safe_limit)
    expand = DC_FUNCTION_THRESHOLD > 0 and total <= DC_FUNCTION_THRESHOLD
    if is_function_scope:
        results = [_function_row(d, fn, expand) for d, fn in page_rows]
    else:
        results = [_event_row(d, ev, expand) for d, ev in page_rows]

    matched_entities = [entity for _, entity in matched_rows]
    histogram, multivalued = label_histogram(matched_entities)
    formatted_histogram = _format_label_histogram(histogram, multivalued)

    return {
        "scope": sel.scope.value,
        "matched": total,
        "returned": len(results),
        "offset": safe_offset,
        "next_offset": next_offset,
        "results": results,
        "label_histogram": formatted_histogram,
    }


def _format_label_histogram(
    histogram: dict,
    multivalued: set,
    unique: dict | None = None,
) -> dict[str, Any]:
    """Format a histogram for response, annotating multi-valued keys.

    Multi-valued keys are flagged so an agent reading
    ``{camera: 312, inference: 200}`` knows the counts overlap. When
    ``unique`` is supplied (device axis only), the per-key unique device
    count is exposed as ``unique_devices`` so the agent can reconcile
    histogram totals with the underlying device cardinality.

    Long-tail keys are cropped to the top ``LABEL_VALUES_TOP_N`` values
    by frequency; the dropped count is reported via a sibling ``more``
    field so agents can choose to switch to the paginated per-key form.
    """
    # Read + clamp at call time: env var / ``patch.object`` overrides take
    # effect, and the [1, 200] bound holds regardless of how the override
    # was set.
    top_n = max(1, min(LABEL_VALUES_TOP_N, 200))
    out: dict[str, Any] = {}
    for key, counts in histogram.items():
        # Sort values most-frequent first; alphabetical tie-break for stability.
        sorted_values = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        entry: dict[str, Any] = {"values": dict(sorted_values[:top_n])}
        if len(sorted_values) > top_n:
            entry["more"] = len(sorted_values) - top_n
        if key in multivalued:
            entry["multivalued"] = True
            if unique is not None and key in unique:
                entry["unique_devices"] = unique[key]
        out[key] = entry
    return out


def discover_labels(
    key: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_DISCOVER_LABELS_LIMIT,
) -> dict[str, Any]:
    """Return the fleet's label vocabulary.

    Without ``key``: returns one entry per axis (``device_keys``,
    ``function_keys``, ``event_keys``) with all keys and their top values.
    With ``key`` (e.g. ``"device.location"``, ``"function.direction"``):
    paginates the full value list for that one key.

    The ``key`` axis prefix and label key are case-sensitive
    (``"Device.Location"`` does not resolve), matching the case-sensitive
    matching ``discover()`` uses on label values. Use lowercase by
    convention.

    Args:
        key: Optional dotted axis.key (``device.<k>``, ``function.<k>``,
            ``event.<k>``). When given, the response paginates that one key's
            values rather than returning a multi-axis vocabulary.
        offset: Pagination offset for the per-key value list.
        limit: Max values per page when ``key`` is given (capped at
            ``DISCOVER_HARD_LIMIT``).

    Returns:
        Multi-axis form (no ``key``):
            ``{"total_devices", "total_functions", "total_events",
              "device_keys": {key: {"values": {...}, "multivalued"?: True,
                                    "unique_devices"?: N}},
              "function_keys": {...}, "event_keys": {...}}``
        Per-key form (``key`` provided):
            ``{"axis", "key", "matched", "returned", "offset", "next_offset",
              "values", "multivalued"?: True}``
    """
    safe_offset, safe_limit = _normalize_pagination(offset, limit, DEFAULT_DISCOVER_LABELS_LIMIT)

    try:
        conn = get_connection()
        devices = conn.list_devices()
    except Exception as e:
        logger.error("discover_labels failed loading fleet: %s", e)
        return _empty_envelope(error=_error("connection_error", str(e)))

    # Aggregate function and event entities once.
    functions: list[dict] = []
    events: list[dict] = []
    for d in devices:
        for fn in d.get("functions", []) or []:
            if isinstance(fn, dict):
                functions.append(fn)
        for ev in d.get("events", []) or []:
            if isinstance(ev, dict):
                events.append(ev)

    dev_hist, dev_mv, dev_unique = label_histogram(devices, count_unique=True)
    fn_hist, fn_mv = label_histogram(functions)
    ev_hist, ev_mv = label_histogram(events)

    if key is None:
        return {
            "total_devices": len(devices),
            "total_functions": len(functions),
            "total_events": len(events),
            "device_keys": _format_label_histogram(dev_hist, dev_mv, dev_unique),
            "function_keys": _format_label_histogram(fn_hist, fn_mv),
            "event_keys": _format_label_histogram(ev_hist, ev_mv),
        }

    # Per-key form: split on the first dot to pick an axis.
    if "." not in key:
        return _empty_envelope(
            error=_error(
                "key_not_axis_qualified",
                f"Key must be axis-qualified (device.<k>, function.<k>, event.<k>): {key!r}",
            )
        )
    axis, label_key = key.split(".", 1)
    if axis == "device":
        source, multivalued = dev_hist, dev_mv
        total = len(devices)
    elif axis == "function":
        source, multivalued = fn_hist, fn_mv
        total = len(functions)
    elif axis == "event":
        source, multivalued = ev_hist, ev_mv
        total = len(events)
    else:
        return _empty_envelope(
            error=_error(
                "unknown_axis",
                f"Unknown axis {axis!r} (expected device|function|event)",
            )
        )

    counts = source.get(label_key, {})
    sorted_values = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    page = sorted_values[safe_offset:safe_offset + safe_limit]
    next_offset = safe_offset + safe_limit if safe_offset + safe_limit < len(sorted_values) else None
    out: dict[str, Any] = {
        "axis": axis,
        "key": label_key,
        "matched": len(sorted_values),
        "returned": len(page),
        "offset": safe_offset,
        "next_offset": next_offset,
        "values": dict(page),
        "axis_total": total,
    }
    if label_key in multivalued:
        out["multivalued"] = True
    return out


# ── Selector-driven operations ───────────────────────────────────


# Default per-target timeout for invoke_many fan-out. Configurable per call.
DEFAULT_INVOKE_TIMEOUT = 30.0

# Cap on parallel worker threads for invoke_many fan-out. Larger fleets can
# raise this via the ``max_concurrency`` argument; the default keeps thread
# overhead bounded while still parallelising typical 10-100 device fan-outs.
DEFAULT_INVOKE_CONCURRENCY = 32


def _resolve_function_tuples(
    selector: str,
) -> tuple[list[dict] | None, dict[str, Any] | None]:
    """Resolve a selector to (device_id, function_name) tuples for invocation.

    Walks pagination so callers do not have to. Returns ``(rows, None)`` on
    success or ``(None, error_envelope)`` if the selector failed to parse,
    used a non-function scope, or the registry was unreachable.
    """
    rows: list[dict] = []
    offset = 0
    while True:
        page = discover(selector, offset=offset, limit=DISCOVER_HARD_LIMIT)
        if "error" in page:
            return None, page
        if page["scope"] not in (
            Scope.DEVICE_FUNCTION.value, Scope.FUNCTION_ONLY.value,
        ):
            return None, _empty_envelope(
                scope=page["scope"],
                error=_error(
                    "invalid_invoke_scope",
                    "invoke/invoke_many require a function-scoped selector "
                    "(device(...).function(...) or function(...)); got "
                    f"scope={page['scope']!r}",
                ),
            )
        rows.extend(page["results"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    return rows, None


def _shape_invoke_response(
    response: dict[str, Any],
    device_id: str,
    function_name: str,
) -> dict[str, Any]:
    """Normalize a JSON-RPC response into a {success, result|error} envelope.

    JSON-RPC error objects arrive as ``{"code": int, "message": str}`` from
    the wire; this maps them to the structured ``{code: str, message: str}``
    error shape that the rest of the agent surface uses.
    """
    if "error" in response:
        err = response["error"]
        if isinstance(err, dict):
            code = str(err.get("code", "invoke_failed"))
            message = str(err.get("message", err))
        else:
            code, message = "invoke_failed", str(err)
        return {
            "success": False,
            "device_id": device_id,
            "function": function_name,
            "error": {"code": code, "message": message},
        }
    return {
        "success": True,
        "device_id": device_id,
        "function": function_name,
        "result": response.get("result", {}),
    }


def invoke(
    selector: str,
    params: dict[str, Any] | None = None,
    llm_reasoning: str | None = None,
) -> dict[str, Any]:
    """Resolve a selector to one (device, function) tuple and invoke it.

    Use this when the call is unambiguous -- one device, one function.
    The selector must use ``device(<id>).function(<name>)`` or
    ``function(<name>)`` scope.

    Args:
        selector: Selector expression resolving to exactly one function.
        params: Function parameters dict. Do NOT put ``llm_reasoning``
            inside ``params``.
        llm_reasoning: Decision rationale for observability.

    Returns:
        On success: ``{"success": True, "device_id": ..., "function": ...,
        "result": ...}``.
        On failure: ``{"success": False, "error": {"code": ...,
        "message": ...}}``. Codes include the discover() codes plus
        ``no_match`` (zero matches), ``ambiguous_match`` (multiple
        matches), ``invalid_invoke_scope`` (selector did not target
        functions), and ``invoke_failed`` (the device returned an error).
    """
    rows, error_envelope = _resolve_function_tuples(selector)
    if error_envelope is not None:
        return {"success": False, "error": error_envelope["error"]}

    if not rows:
        return {
            "success": False,
            "error": _error(
                "no_match",
                f"selector matched 0 functions: {selector!r}",
            ),
        }
    if len(rows) > 1:
        return {
            "success": False,
            "error": _error(
                "ambiguous_match",
                f"selector matched {len(rows)} functions, expected exactly 1: "
                f"{selector!r}",
            ),
            "candidates": [
                {"device_id": r.get("device_id"), "function": r.get("name")}
                for r in rows[:10]
            ],
        }

    row = rows[0]
    device_id = row.get("device_id") or ""
    function_name = row.get("name") or ""

    trace_id = f"trace-{uuid.uuid4().hex[:12]}"
    if llm_reasoning:
        truncated = (
            llm_reasoning[:200] + "..."
            if len(llm_reasoning) > 200 else llm_reasoning
        )
        logger.info(
            "[%s] [%s::%s] Reason: %s",
            trace_id, device_id, function_name, truncated,
        )

    try:
        conn = get_connection()
        clean = {k: v for k, v in (params or {}).items() if k != "llm_reasoning"}
        response = conn.invoke(device_id, function_name, params=clean)
    except Exception as e:
        logger.error(
            "[%s] %s::%s -> ERROR: %s",
            trace_id, device_id, function_name, e,
        )
        return {
            "success": False,
            "device_id": device_id,
            "function": function_name,
            "error": _error("invoke_failed", str(e)),
        }
    return _shape_invoke_response(response, device_id, function_name)


def invoke_many(
    selector: str,
    params: dict[str, Any] | None = None,
    timeout: float = DEFAULT_INVOKE_TIMEOUT,
    max_concurrency: int = DEFAULT_INVOKE_CONCURRENCY,
    llm_reasoning: str | None = None,
) -> dict[str, Any]:
    """Resolve a selector to (device, function) tuples and invoke each in parallel.

    Returns aggregated results with partial-failure semantics: a single
    target's failure does not abort the rest. Each target gets ``timeout``
    seconds; the overall call returns once every target has finished or
    timed out.

    Args:
        selector: Function-scoped selector
            (``device(...).function(...)`` or ``function(...)``).
        params: Function parameters dict applied to every target.
        timeout: Per-target timeout in seconds.
        max_concurrency: Cap on parallel worker threads.
        llm_reasoning: Decision rationale for observability.

    Returns:
        ``{"candidates": N, "matched": N, "succeeded": S, "failed": F,
           "results": [{device_id, function, result}, ...],
           "errors":  [{device_id, function, error}, ...]}``.

        ``candidates`` is the count returned by the selector resolver.
        ``matched`` is the same value in this release; once edge-side
        ``where`` predicates land, ``matched`` will narrow below
        ``candidates`` to reflect post-predicate self-election.

        On selector parse / connection failure the envelope is returned
        with all counts at zero plus a top-level ``error`` field.
    """
    import concurrent.futures

    rows, error_envelope = _resolve_function_tuples(selector)
    if error_envelope is not None:
        return {
            "candidates": 0, "matched": 0, "succeeded": 0, "failed": 0,
            "results": [], "errors": [], "error": error_envelope["error"],
        }

    out: dict[str, Any] = {
        "candidates": len(rows),
        "matched": len(rows),
        "succeeded": 0,
        "failed": 0,
        "results": [],
        "errors": [],
    }
    if not rows:
        return out

    workers = max(1, min(max_concurrency, len(rows)))
    clean = {k: v for k, v in (params or {}).items() if k != "llm_reasoning"}

    def call_one(row: dict) -> dict[str, Any]:
        device_id = row.get("device_id") or ""
        function_name = row.get("name") or ""
        try:
            conn = get_connection()
            response = conn.invoke(
                device_id, function_name, params=clean, timeout=timeout,
            )
        except Exception as e:
            response = {"error": {"code": "invoke_failed", "message": str(e)}}
        return _shape_invoke_response(response, device_id, function_name)

    if llm_reasoning:
        truncated = (
            llm_reasoning[:200] + "..."
            if len(llm_reasoning) > 200 else llm_reasoning
        )
        logger.info(
            "[invoke_many::%d targets] Reason: %s", len(rows), truncated,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as exe:
        futures = [exe.submit(call_one, row) for row in rows]
        for future in concurrent.futures.as_completed(futures):
            shaped = future.result()
            if shaped["success"]:
                out["results"].append({
                    "device_id": shaped["device_id"],
                    "function": shaped["function"],
                    "result": shaped["result"],
                })
                out["succeeded"] += 1
            else:
                out["errors"].append({
                    "device_id": shaped["device_id"],
                    "function": shaped["function"],
                    "error": shaped["error"],
                })
                out["failed"] += 1
    return out


def broadcast(
    selector: str,
    params: dict[str, Any] | None = None,
    where: str | None = None,
    bindings: dict[str, Any] | None = None,
    fire_at: float | None = None,
    on_late: str = "skip",
    llm_reasoning: str | None = None,
) -> dict[str, Any]:
    """Async selector-driven fan-out. Returns immediately with a correlation id.

    Use ``broadcast`` when the caller does not want to block on the slowest
    device. Each candidate self-elects via the optional ``where`` predicate
    (CEL, evaluated at the edge against the device's identity, labels, live
    status, and the shared ``bindings``) and emits its reply as an event on
    a per-device subject keyed by ``correlation_id``::

        device-connect.<zone>.<device_id>.event.async_reply.<correlation_id>

    Subscribe to those replies via ``subscribe('correlation:<id>')`` or wait
    for them with ``await_replies(correlation_id, timeout=...)``.

    Args:
        selector: Function-scoped selector. The selector must resolve to a
            single function name across the matched devices; if multiple
            functions match, an ``ambiguous_function`` error is returned.
        params: Function parameters dict applied to every target.
        where: Optional CEL predicate evaluated at the edge per candidate
            (e.g. ``"status.battery > 50"``, ``"mask[row][col] == 1"``).
            Validated at the dispatcher before publication so syntax
            errors return immediately rather than reaching the wire.
        bindings: Shared payload merged into the predicate context as
            ``bindings.<key>``. Keep small (selection masks, thresholds,
            top-K rankings); the same bytes ship to every device.
        fire_at: Optional wall-clock epoch seconds. Each device holds the
            message and fires its function from its own clock at
            ``fire_at`` for synchronized fan-out.
        on_late: Policy when a device receives a ``fire_at`` message after
            the deadline. ``"skip"`` (default) drops the call; ``"fire"``
            executes immediately.
        llm_reasoning: Decision rationale for observability.

    Returns:
        On success: ``{"correlation_id": "br-...", "candidates": N,
        "selector": ..., "function": ...}``.
        On failure: ``{"candidates": 0, "error": {"code", "message"}}``
        with codes including the discover() codes,
        ``invalid_invoke_scope``, ``ambiguous_function``,
        ``invalid_predicate``, and ``invalid_on_late``.
    """
    if on_late not in ("skip", "fire"):
        return {
            "candidates": 0,
            "error": _error(
                "invalid_on_late",
                f"on_late must be 'skip' or 'fire', got {on_late!r}",
            ),
        }

    rows, error_envelope = _resolve_function_tuples(selector)
    if error_envelope is not None:
        return {"candidates": 0, "error": error_envelope["error"]}

    if not rows:
        # Empty fan-out: still mint a correlation id so callers waiting on
        # replies see a clean "no candidates" rather than a hang.
        return {
            "correlation_id": f"br-{uuid.uuid4().hex[:12]}",
            "candidates": 0,
            "selector": selector,
        }

    # Broadcast assumes one function per call. If the selector resolves to
    # multiple distinct functions, surface that as a structured error so
    # the caller can either narrow the selector or split into multiple
    # broadcasts.
    function_names = {row.get("name") for row in rows if row.get("name")}
    if len(function_names) != 1:
        return {
            "candidates": len(rows),
            "error": _error(
                "ambiguous_function",
                f"selector resolved to {len(function_names)} distinct "
                "functions; broadcast requires exactly one function per call: "
                f"{sorted(function_names)!r}",
            ),
        }
    function_name = next(iter(function_names))

    # Compile-validate the where predicate before going to the wire so a
    # syntax error short-circuits without bothering devices.
    if where is not None:
        try:
            from device_connect_edge.predicate import compile_where
            compile_where(where)
        except Exception as e:
            return {
                "candidates": len(rows),
                "error": _error("invalid_predicate", str(e)),
            }

    correlation_id = f"br-{uuid.uuid4().hex[:12]}"
    targets = sorted({
        row.get("device_id") for row in rows if row.get("device_id")
    })
    clean_params = {
        k: v for k, v in (params or {}).items() if k != "llm_reasoning"
    }

    # Advisory only: WARN (don't block) when the matched set includes a
    # safety:critical RPC, so typo'd selectors that sweep across critical
    # functions are operator-visible. Broadcast is the ESTOP path.
    critical_rows = [
        row for row in rows
        if (row.get("labels") or {}).get("safety") == "critical"
    ]
    if critical_rows:
        # Dedupe device_ids before slicing so the sample reflects distinct
        # devices, not the first 3 (possibly same-device) critical rows.
        sample_ids = sorted({
            row["device_id"] for row in critical_rows if row.get("device_id")
        })[:3]
        if where:
            shown = where if len(where) <= 80 else where[:77] + "..."
            where_snippet = f" where={shown!r}"
        else:
            where_snippet = ""
        logger.warning(
            "[broadcast::%s::%s] matched %d row(s) labeled safety:critical "
            "(sample devices: %s);%s proceeding (advisory only)",
            function_name, correlation_id, len(critical_rows), sample_ids,
            where_snippet,
        )

    envelope: dict[str, Any] = {
        "correlation_id": correlation_id,
        "function": function_name,
        "params": clean_params,
        "targets": targets,
    }
    if where:
        envelope["where"] = where
    if bindings:
        envelope["bindings"] = bindings
    if fire_at is not None:
        envelope["fire_at"] = float(fire_at)
        envelope["on_late"] = on_late

    if llm_reasoning:
        truncated = (
            llm_reasoning[:200] + "..."
            if len(llm_reasoning) > 200 else llm_reasoning
        )
        logger.info(
            "[broadcast::%s::%d targets] Reason: %s",
            correlation_id, len(targets), truncated,
        )

    try:
        conn = get_connection()
        conn.publish_broadcast(envelope)
    except Exception as e:
        logger.error("broadcast publish failed: %s", e)
        return {
            "candidates": len(targets),
            "error": _error("connection_error", str(e)),
        }

    return {
        "correlation_id": correlation_id,
        "candidates": len(targets),
        "selector": selector,
        "function": function_name,
    }


# ── Selector-driven subscription ─────────────────────────────────


# Sentinel used to recognise the broadcast-reply form of a subscribe
# selector (``correlation:<id>``). Kept short so the selector reads
# naturally; the parser matches an exact prefix.
_CORRELATION_PREFIX = "correlation:"


class Subscription:
    """A live subscription handle returned by :func:`subscribe`.

    Two selector forms produce a subscription:

    * ``"correlation:<id>"`` -- replies from a prior :func:`broadcast` call,
      keyed by ``correlation_id`` and routed across all devices that fired.
    * Event-scoped selectors (``event(<name>)`` or
      ``device(...).event(<name>)``) -- a multiplex of matching events
      across the resolved candidate set.

    The handle exposes a sync ``read`` API that drains buffered messages.
    Use as a context manager (or call :meth:`close`) to tear the
    underlying messaging subscription down deterministically::

        with subscribe("correlation:" + cid) as sub:
            for reply in sub.iter(timeout=5.0):
                process(reply)
    """

    def __init__(self, conn: Any, inbox_names: list[str]):
        self._conn = conn
        self._inbox_names = list(inbox_names)
        self._closed = False
        self._cursor = 0  # index into the concatenated message stream

    def read(self, max_messages: int | None = None) -> list[dict[str, Any]]:
        """Drain currently buffered messages without blocking.

        Returns parsed payload dicts (already JSON-decoded by the
        connection's buffered subscription path). Subsequent calls return
        only messages that arrived after the previous call.

        Race-safe against the messaging callback that appends to the same
        inbox: each inbox is read by snapshotting its current length and
        truncating only that prefix, so a message that arrives during
        iteration stays buffered for the next ``read``.
        """
        if self._closed:
            return []
        out: list[dict[str, Any]] = []
        for name in self._inbox_names:
            buf = self._conn._inbox.get(name) or []
            # Snapshot the consumed prefix length BEFORE iterating, then
            # truncate by exactly that many items. Any message appended by
            # the messaging callback between the snapshot and the truncation
            # remains buffered for a subsequent ``read``.
            n = len(buf)
            for subject, payload in buf[:n]:
                if not isinstance(payload, dict):
                    payload = {"raw": payload}
                out.append({**payload, "_subject": subject})
            self._conn._inbox[name] = buf[n:]
        if max_messages is not None:
            out = out[:max_messages]
        return out

    def iter(self, timeout: float = 5.0, poll_interval: float = 0.05):
        """Yield messages until ``timeout`` elapses with no new arrivals.

        ``timeout`` resets each time at least one message is yielded, so
        callers can drain a steady stream without re-parameterising the
        wait. Use ``read`` instead for one-shot draining.
        """
        deadline = time.monotonic() + timeout
        while not self._closed:
            new = self.read()
            if new:
                for msg in new:
                    yield msg
                deadline = time.monotonic() + timeout
                continue
            if time.monotonic() >= deadline:
                return
            time.sleep(poll_interval)

    def __iter__(self):
        """Allow ``for msg in sub:`` with a default 30-second idle timeout.

        Delegates to :meth:`iter` with sensible defaults so the idiomatic
        Python iteration form works. Use ``sub.iter(timeout=...)`` directly
        when the default does not fit.
        """
        return self.iter(timeout=30.0, poll_interval=0.05)

    def close(self) -> None:
        """Tear down the underlying messaging subscriptions."""
        if self._closed:
            return
        self._closed = True
        for name in self._inbox_names:
            try:
                self._conn.unsubscribe_buffered(name)
            except Exception:  # pragma: no cover - cleanup best effort
                logger.debug("close: unsubscribe %s failed", name, exc_info=True)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _correlation_subjects(conn: Any, correlation_id: str) -> list[str]:
    """Build the per-device wildcard reply subjects for a correlation id.

    The reply template is ``device-connect.<tenant>.<device_id>.event
    .async_reply.<correlation_id>``; ``<device_id>`` is single-token wildcarded
    so a subscription receives replies from any device that fires the
    broadcast without having to enumerate them up-front.
    """
    return [
        f"device-connect.{conn.zone}.*.event.async_reply.{correlation_id}",
    ]


def _event_names_for_filter(selector: str) -> tuple[list[str] | None, dict[str, Any] | None]:
    """Resolve top-level ``event(...)`` to event names for live wildcard subs."""
    try:
        sel = parse_selector(selector)
    except SelectorParseError as e:
        return None, _empty_envelope(error=_error("selector_parse_error", str(e)))
    if sel.scope != Scope.EVENT_ONLY:
        return None, _empty_envelope(
            scope=sel.scope.value,
            error=_error(
                "invalid_subscribe_scope",
                "top-level live event subscriptions require event(...) scope; "
                f"got scope={sel.scope.value!r}",
            ),
        )

    rows: list[dict] = []
    offset = 0
    while True:
        page = discover(selector, offset=offset, limit=DISCOVER_HARD_LIMIT)
        if "error" in page:
            return None, page
        rows.extend(page["results"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]

    names = sorted({
        row.get("name") for row in rows
        if row.get("name")
    })
    return names, None


def _event_subjects_for_selector(selector: str) -> tuple[list[str] | None, dict[str, Any] | None]:
    """Resolve an event-scoped selector to per-device subjects.

    Returns ``(subjects, None)`` on success or ``(None, error_envelope)``
    if the selector failed to parse or used a non-event scope.
    """
    try:
        sel = parse_selector(selector)
    except SelectorParseError as e:
        return None, _empty_envelope(error=_error("selector_parse_error", str(e)))

    if sel.scope == Scope.EVENT_ONLY:
        names, error = _event_names_for_filter(selector)
        if error is not None:
            return None, error
        conn = get_connection()
        return [
            f"device-connect.{conn.zone}.*.event.{name}"
            for name in (names or [])
        ], None

    rows: list[dict] = []
    offset = 0
    while True:
        page = discover(selector, offset=offset, limit=DISCOVER_HARD_LIMIT)
        if "error" in page:
            return None, page
        if page["scope"] not in (Scope.DEVICE_EVENT.value, Scope.EVENT_ONLY.value):
            return None, _empty_envelope(
                scope=page["scope"],
                error=_error(
                    "invalid_subscribe_scope",
                    "subscribe requires an event-scoped selector "
                    "(device(...).event(...) or event(...)) or "
                    "'correlation:<id>'; got "
                    f"scope={page['scope']!r}",
                ),
            )
        rows.extend(page["results"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]

    conn = get_connection()
    subjects: list[str] = []
    seen: set[str] = set()
    for row in rows:
        device_id = row.get("device_id") or ""
        event_name = row.get("name") or ""
        if not device_id or not event_name:
            continue
        subj = f"device-connect.{conn.zone}.{device_id}.event.{event_name}"
        if subj not in seen:
            seen.add(subj)
            subjects.append(subj)
    return subjects, None


def subscribe(selector: str) -> Subscription:
    """Subscribe to events or broadcast replies matching a selector.

    Args:
        selector: One of:
            - ``"correlation:<id>"`` for broadcast replies of a prior call.
            - ``event(...)`` for live event streams. Matching event names are
              resolved once, then subscribed with a device wildcard so devices
              that join later and emit those event names are included.
            - ``device(...).event(...)`` for a snapshot event stream over the
              devices resolved when the subscription is created.

    Returns:
        A :class:`Subscription` handle. Iterate with ``sub.iter(timeout)``
        or drain currently-buffered messages with ``sub.read()``. Always
        close (or use ``with``) to tear the underlying subscription down.

    Raises:
        ValueError on selector errors. The selector string is checked at
        the boundary; downstream subscribe calls are not retried, so a
        parse error fails fast.
    """
    if not isinstance(selector, str) or not selector.strip():
        raise ValueError("subscribe selector must be a non-empty string")

    conn = get_connection()
    if selector.startswith(_CORRELATION_PREFIX):
        correlation_id = selector[len(_CORRELATION_PREFIX):].strip()
        if not correlation_id:
            raise ValueError(
                "correlation form must be 'correlation:<id>' with non-empty id"
            )
        subjects = _correlation_subjects(conn, correlation_id)
        inbox_prefix = f"sub-corr-{correlation_id}-{uuid.uuid4().hex[:8]}"
    else:
        subjects, error_envelope = _event_subjects_for_selector(selector)
        if error_envelope is not None:
            err = error_envelope.get("error")
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise ValueError(msg)
        if not subjects:
            # Nothing to subscribe to. Return an idle Subscription so the
            # caller's ``with subscribe(...) as sub: ...`` pattern still
            # works without raising; ``read``/``iter`` will yield nothing.
            return Subscription(conn, inbox_names=[])
        inbox_prefix = f"sub-evt-{uuid.uuid4().hex[:8]}"

    inbox_names: list[str] = []
    for i, subj in enumerate(subjects):
        name = f"{inbox_prefix}-{i}"
        conn.subscribe_buffered(subj, name=name)
        inbox_names.append(name)
    return Subscription(conn, inbox_names=inbox_names)


def await_replies(
    correlation_id: str,
    timeout: float = 10.0,
    until: int | None = None,
    poll_interval: float = 0.05,
) -> list[dict[str, Any]]:
    """Block until ``timeout`` elapses or ``until`` replies have arrived.

    A sync helper for the common broadcast pattern: caller fires a
    :func:`broadcast`, then waits for some replies. Builds a one-shot
    subscription on the correlation reply subject, drains it, and tears
    down before returning.

    Args:
        correlation_id: The id returned by :func:`broadcast`.
        timeout: Overall wall-clock limit in seconds.
        until: Stop early once this many replies have been collected.
        poll_interval: How often the helper polls the subscription buffer.

    Returns:
        A list of reply payload dicts, each with at least
        ``{correlation_id, device_id, success, result|error,
        actually_fired_at}``.
    """
    if not correlation_id:
        return []
    sub = subscribe(f"{_CORRELATION_PREFIX}{correlation_id}")
    try:
        replies: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout
        while True:
            new = sub.read()
            replies.extend(new)
            if until is not None and len(replies) >= until:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(poll_interval)
        return replies
    finally:
        sub.close()


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

    .. deprecated::
        Prefer ``discover_labels()`` (vocabulary) and
        ``discover("device(*)")`` (roster). This wrapper will be removed in
        a future release.
    """
    warnings.warn(
        "describe_fleet() is deprecated; use discover_labels() for vocabulary "
        "or discover('device(*)') for the roster.",
        DeprecationWarning,
        stacklevel=2,
    )
    try:
        conn = get_connection()
        devices = conn.list_devices()

        result: dict[str, Any] = aggregate_fleet(devices)

        # Auto-expand: include full device details for small fleets
        if SMALL_FLEET_THRESHOLD > 0 and len(devices) <= SMALL_FLEET_THRESHOLD:
            result["devices"] = [full_device(d) for d in devices]
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

    .. deprecated::
        Prefer ``discover("device(category:camera, location:zone-A/*)")`` --
        the selector DSL covers type/location/group-by uniformly. This
        wrapper will be removed in a future release.
    """
    warnings.warn(
        "list_devices() is deprecated; use discover() with a selector "
        "(e.g. discover('device(category:camera)')).",
        DeprecationWarning,
        stacklevel=2,
    )
    try:
        conn = get_connection()
        devices = conn.list_devices(location=location)

        # Client-side fuzzy type filter (provider filter is stricter and
        # can drop valid fuzzy matches like "environmentsensor" vs
        # "environment_sensor", so we skip provider-level type filtering
        # and let fuzzy_filter_by_type be the sole filter).
        if device_type:
            devices = fuzzy_filter_by_type(devices, device_type)

        # Status filter
        if status:
            s = status.lower()
            devices = [d for d in devices if s in extract_status(d).lower()]

        total = len(devices)

        # Build device summaries — include schemas for small result sets
        def _summary(d: dict, expand: bool) -> dict:
            result = compact_device(d, expand)
            result["status"] = extract_status(d)
            return result

        if group_by in ("location", "device_type"):
            expand = SMALL_FLEET_THRESHOLD > 0 and total <= SMALL_FLEET_THRESHOLD
            return group_devices(devices, group_by, expand)

        # Paginate
        page = devices[offset:offset + limit]
        expand = SMALL_FLEET_THRESHOLD > 0 and total <= SMALL_FLEET_THRESHOLD
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

    .. deprecated::
        Prefer ``discover("device(<device_id>).function(*)")``. This wrapper
        will be removed in a future release.
    """
    warnings.warn(
        "get_device_functions() is deprecated; use "
        "discover('device(<id>).function(*)').",
        DeprecationWarning,
        stacklevel=2,
    )
    try:
        conn = get_connection()
        device = conn.get_device(device_id)
        if not device:
            return {"error": f"Device {device_id} not found"}
        return full_device(device)
    except Exception as e:
        return {"error": str(e)}


# ── Invocation tools (unchanged) ─────────────────────────────────


def invoke_device(
    device_id: str,
    function: str,
    params: dict[str, Any] | None = None,
    llm_reasoning: str | None = None,
) -> dict[str, Any]:
    """Call a function on a Device Connect device (deprecated; use invoke()).

    Args:
        device_id: Target device ID (e.g., "robot-001", "camera-001").
        function: Function name to call.
        params: Function parameters as a dictionary.
        llm_reasoning: Why you are calling this function (for observability).
    """
    warnings.warn(
        "invoke_device(device_id, function, ...) is deprecated; use "
        "invoke('device(<id>).function(<name>)', params) instead.",
        DeprecationWarning,
        stacklevel=2,
    )
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
    """Discover available devices (deprecated; use discover() instead).

    Args:
        device_type: Optional filter (e.g., "robot", "camera"). Fuzzy matching.
        refresh: Force refresh from registry instead of cache.

    Returns:
        List of devices with device_id, device_type, functions, events.
    """
    warnings.warn(
        "discover_devices() is deprecated; use discover() with a selector "
        "(e.g. discover('device(*)') or discover('device(category:camera)')).",
        DeprecationWarning,
        stacklevel=2,
    )
    try:
        conn = get_connection()
        # Invalidate cache when refresh is requested
        if refresh:
            conn.invalidate_cache()
        devices = conn.list_devices()

        if device_type:
            devices = fuzzy_filter_by_type(devices, device_type)

        results = []
        for d in devices:
            entry = full_device(d)
            entry["status"] = d.get("status", {})
            results.append(entry)
        return results

    except Exception as e:
        logger.error("Discovery failed: %s", e)
        return []
