# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""User dashboard with live device polling, RPC invocation, and event streaming."""

import asyncio
import hashlib
import json
import logging
import time

import aiohttp_jinja2
from aiohttp import web

from ..services import credentials, registry_client
from ..services.backend import get_backend

logger = logging.getLogger(__name__)


def setup_routes(app: web.Application):
    app.router.add_get("/dashboard", dashboard_page)
    app.router.add_get("/api/devices/live", live_devices_fragment)
    app.router.add_get("/api/devices/live.json", live_devices_json)
    app.router.add_get("/api/devices/{device_id}/row-html", device_row_html_fragment)
    app.router.add_get("/api/devices/{device_id}/live-detail", live_device_detail_fragment)
    app.router.add_post("/api/devices/{device_id}/invoke", invoke_device_rpc)
    app.router.add_get("/api/devices/{device_id}/events/{event_name}/stream", event_stream)


def _capabilities_hash(caps) -> str:
    """Stable short hash of a device's capabilities for change detection.

    The dashboard's JSON poll uses this to decide whether to refresh an
    already-expanded detail panel in place. We hash the canonical
    JSON form so reordering keys doesn't trigger spurious refreshes.
    """
    payload = json.dumps(caps or {}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


async def dashboard_page(request: web.Request):
    user = request["user"]
    tenant = user["tenant"]

    creds = credentials.list_credentials(tenant=tenant)
    live_devices = []
    try:
        live_devices = registry_client.list_live_devices(tenant)
    except Exception:
        pass

    online_count = sum(1 for d in live_devices if d.get("status") == "available")

    return aiohttp_jinja2.render_template("dashboard.html", request, {
        "user": user,
        "nav": "dashboard",
        "tenant": tenant,
        "creds_count": len(creds),
        "online_count": online_count,
        "registered_count": len(live_devices),
        "readonly": False,
    })


async def live_devices_fragment(request: web.Request):
    """Return the live devices table as an HTML fragment for htmx polling.

    Only summary rows are rendered here; the per-device detail markup is
    lazy-loaded from ``/api/devices/{device_id}/live-detail`` when a row
    is expanded. At fleet scale the detail blocks dominate response size,
    so deferring them keeps each poll cheap regardless of fleet size.
    """
    tenant = _resolve_tenant(request)

    # etcd get_prefix + JSON-decode scales with fleet size; run it off
    # the event loop so other portal requests aren't blocked on the poll.
    devices = []
    try:
        devices = await asyncio.to_thread(
            registry_client.list_live_devices, tenant,
        )
    except Exception:
        pass

    # Header card counts are refreshed by piggybacking on this poll via
    # hx-swap-oob in the template. Without that, dashboard_page renders
    # them once at first load and they freeze — at fleet scale the user
    # then sees "1401 online" long after the swarm has shut down.
    online_count = sum(1 for d in devices if d.get("status") == "available")
    try:
        creds_count = len(
            await asyncio.to_thread(credentials.list_credentials, tenant=tenant),
        )
    except Exception:
        creds_count = 0

    return aiohttp_jinja2.render_template("devices/_live_table.html", request, {
        "devices": devices,
        "tenant": tenant,
        "online_count": online_count,
        "registered_count": len(devices),
        "creds_count": creds_count,
        "user": request.get("user", {}),
    })


async def live_devices_json(request: web.Request):
    """JSON snapshot for the dashboard's in-place poll.

    Replaces the table-wide htmx swap. The client merges these values
    into existing rows (status pill, location, last-seen) without
    touching DOM structure, so scroll position and expand/event-log
    state survive. ``capabilities_hash`` lets the client decide whether
    an already-expanded detail panel needs re-fetching.

    Cost: ``registry_client.list_live_devices`` paginates through the
    full tenant fleet via the registry RPC; the registry, in turn,
    does a full ``etcd get_prefix`` + JSON-decode per page (see
    ``DeviceRegistry.list_devices_page``). At ~1400 devices with the
    default 100-device page that's ~14 etcd scans per JSON-poll tick,
    per dashboard. The pagination fix bounds NATS *payload* size but
    NOT registry CPU; if the portal is being polled by many concurrent
    operators, raise ``DEVICE_CONNECT_LIST_PAGE_SIZE`` (with the
    matching ``DC_LIST_DEVICES_MAX_LIMIT`` and NATS ``max_payload``)
    or lengthen the client poll interval. Selector pushdown / keyset
    pagination is the documented next iteration.
    """
    tenant = _resolve_tenant(request)
    devices = []
    try:
        devices = await asyncio.to_thread(registry_client.list_live_devices, tenant)
    except Exception:
        pass
    try:
        creds_count = len(
            await asyncio.to_thread(credentials.list_credentials, tenant=tenant),
        )
    except Exception:
        creds_count = 0

    return web.json_response({
        "devices": [
            {
                "device_id": d.get("device_id"),
                "device_type": d.get("device_type"),
                "status": d.get("status"),
                "location": d.get("location"),
                "last_seen": d.get("last_seen"),
                "capabilities_hash": _capabilities_hash(d.get("capabilities")),
            }
            for d in devices
        ],
        "counts": {
            "online": sum(1 for d in devices if d.get("status") == "available"),
            "registered": len(devices),
            "creds": creds_count,
        },
    })


async def device_row_html_fragment(request: web.Request):
    """Render the summary+detail row pair for one device.

    Called by the dashboard JSON poll when it sees a device_id that
    isn't yet in the table — the JS appends the returned HTML to
    ``<tbody>`` instead of triggering a full page reload. The same
    Jinja partial powers the initial server-side table render, so an
    appended row is structurally identical to one rendered at page
    load (same id, same cell classes, same chevron, same lazy-detail
    URL).
    """
    tenant = _resolve_tenant(request)
    device_id = request.match_info["device_id"]

    raw = await asyncio.to_thread(registry_client.get_device, tenant, device_id)
    if not raw:
        return web.Response(status=404, text="", content_type="text/html")

    device = registry_client.format_live_device(raw)
    return aiohttp_jinja2.render_template(
        "devices/_device_row_pair.html", request,
        {"device": device, "tenant": tenant},
    )


async def live_device_detail_fragment(request: web.Request):
    """Return the per-device detail fragment (functions, events, raw JSON).

    Loaded lazily when a row is expanded — keeps the main polling
    response O(summary) rather than O(summary + every-device-detail).
    """
    tenant = _resolve_tenant(request)
    device_id = request.match_info["device_id"]

    raw = await asyncio.to_thread(registry_client.get_device, tenant, device_id)
    if not raw:
        return web.Response(
            status=404,
            text='<p class="text-xs text-red-500">Device not found.</p>',
            content_type="text/html",
        )

    device = {
        "device_id": raw.get("device_id", device_id),
        "capabilities": raw.get("capabilities") or {},
        "_raw": raw,
    }
    return aiohttp_jinja2.render_template(
        "devices/_live_detail.html", request, {"device": device},
    )


def _resolve_tenant(request: web.Request) -> str:
    """Get tenant from query param (admin override) or session."""
    from ..services.backend import validate_name

    user = request["user"]
    tenant_override = request.query.get("tenant")
    if tenant_override and user.get("role") == "admin":
        validate_name(tenant_override, "tenant")
        return tenant_override
    return user["tenant"]


async def invoke_device_rpc(request: web.Request):
    """Invoke an RPC function on a device via the active messaging backend."""
    t0 = time.monotonic()
    tenant = _resolve_tenant(request)
    device_id = request.match_info["device_id"]

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": {"message": "Invalid JSON"}}, status=400)

    function = body.get("function", "")
    params = body.get("params", {})

    if not function:
        return web.json_response({"error": {"message": "function is required"}}, status=400)

    backend = get_backend()
    t_pre_rpc = time.monotonic()
    result = await backend.rpc_invoke(tenant, device_id, function, params)
    t_post_rpc = time.monotonic()
    logger.info(
        "invoke %s/%s.%s handler=%.1fms (pre-rpc=%.1fms rpc=%.1fms)",
        tenant, device_id, function,
        (t_post_rpc - t0) * 1000,
        (t_pre_rpc - t0) * 1000,
        (t_post_rpc - t_pre_rpc) * 1000,
    )
    return web.json_response(result)


async def event_stream(request: web.Request):
    """SSE endpoint: stream device events in real-time via the active backend."""
    tenant = _resolve_tenant(request)
    device_id = request.match_info["device_id"]
    event_name = request.match_info["event_name"]

    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)

    backend = get_backend()
    client = None
    sub = None
    try:
        client = await backend.rpc_connect()
        subject = f"device-connect.{tenant}.{device_id}.event.{event_name}"
        queue = asyncio.Queue(maxsize=256)

        async def on_msg(msg_data, _subject=None):
            await queue.put(msg_data)

        # For NATS: msg is a nats.Msg with .data attribute
        # For Zenoh: callback receives (bytes, subject) directly
        if backend.backend_name() == "nats":
            async def _nats_cb(msg):
                await queue.put(msg.data)
            sub = await backend.subscribe_events(client, subject, _nats_cb)
        else:
            sub = await backend.subscribe_events(client, subject, on_msg)

        # Send initial keepalive
        await response.write(b": connected\n\n")

        while True:
            try:
                raw = await asyncio.wait_for(queue.get(), timeout=15)
                try:
                    payload = json.loads(raw if isinstance(raw, (str, bytes)) else raw)
                except (json.JSONDecodeError, TypeError):
                    payload = {"raw": raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)}

                event_data = {
                    "ts": time.strftime("%H:%M:%S"),
                    "params": payload.get("params", payload),
                }
                line = f"data: {json.dumps(event_data)}\n\n"
                await response.write(line.encode())
            except asyncio.TimeoutError:
                # Send keepalive comment to prevent browser timeout
                await response.write(b": keepalive\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        await backend.unsubscribe_events(client, sub)

    return response
