"""User dashboard with live device polling, RPC invocation, and event streaming."""

import asyncio
import json
import time

import aiohttp_jinja2
from aiohttp import web

from ..services import credentials, registry_client, nats_rpc


def setup_routes(app: web.Application):
    app.router.add_get("/dashboard", dashboard_page)
    app.router.add_get("/api/devices/live", live_devices_fragment)
    app.router.add_post("/api/devices/{device_id}/invoke", invoke_device_rpc)
    app.router.add_get("/api/devices/{device_id}/events/{event_name}/stream", event_stream)


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
    """Return the live devices table as an HTML fragment for htmx polling."""
    # Allow tenant override for admin view-as-user
    tenant = request.query.get("tenant")
    if not tenant:
        user = request["user"]
        tenant = user["tenant"]

    devices = []
    try:
        devices = registry_client.list_live_devices(tenant)
    except Exception:
        pass

    return aiohttp_jinja2.render_template("devices/_live_table.html", request, {
        "devices": devices,
        "user": request.get("user", {}),
    })


def _resolve_tenant(request: web.Request) -> str:
    """Get tenant from query param (admin override) or session."""
    user = request["user"]
    tenant_override = request.query.get("tenant")
    if tenant_override and user.get("role") == "admin":
        return tenant_override
    return user["tenant"]


async def invoke_device_rpc(request: web.Request):
    """Invoke an RPC function on a device via NATS."""
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

    result = await nats_rpc.invoke(tenant, device_id, function, params)
    return web.json_response(result)


async def event_stream(request: web.Request):
    """SSE endpoint: stream device events in real-time via NATS subscription."""
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

    nc = None
    sub = None
    try:
        nc = await nats_rpc.connect()
        subject = f"device-connect.{tenant}.{device_id}.event.{event_name}"
        queue = asyncio.Queue()

        async def on_msg(msg):
            await queue.put(msg)

        sub = await nc.subscribe(subject, cb=on_msg)

        # Send initial keepalive
        await response.write(b": connected\n\n")

        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=15)
                try:
                    payload = json.loads(msg.data)
                except (json.JSONDecodeError, TypeError):
                    payload = {"raw": msg.data.decode(errors="replace")}

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
        if sub:
            await sub.unsubscribe()
        if nc:
            await nc.close()

    return response
