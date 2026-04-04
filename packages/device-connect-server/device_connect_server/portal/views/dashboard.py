"""User dashboard with live device polling."""

import aiohttp_jinja2
from aiohttp import web

from ..services import credentials, registry_client


def setup_routes(app: web.Application):
    app.router.add_get("/dashboard", dashboard_page)
    app.router.add_get("/api/devices/live", live_devices_fragment)


async def dashboard_page(request: web.Request):
    user = request["user"]
    tenant = user["tenant"]

    creds = credentials.list_credentials(tenant=tenant)
    live_devices = []
    try:
        live_devices = registry_client.list_live_devices(tenant)
    except Exception:
        pass

    online_count = sum(1 for d in live_devices if d.get("status") == "online")

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
