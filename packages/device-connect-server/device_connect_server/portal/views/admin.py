"""Admin views: dashboard, view-as-user, health check, setup, NATS reload."""

import aiohttp_jinja2
from aiohttp import web

from .. import config
from ..services import (
    credentials,
    nats_admin,
    nsc,
    registry_client,
    users,
    verification,
)


def setup_routes(app: web.Application):
    app.router.add_get("/admin", admin_dashboard)
    app.router.add_get("/admin/tenants/{name}", admin_view_as_user)
    app.router.add_get("/admin/tenants/{name}/devices", admin_view_as_user_devices)
    app.router.add_get("/admin/health", admin_health_page)
    app.router.add_get("/admin/setup", admin_setup_page)
    app.router.add_post("/api/admin/setup", admin_setup_submit)
    app.router.add_post("/api/admin/nats/reload", admin_nats_reload)
    app.router.add_post("/api/admin/health/verify", admin_health_verify)
    app.router.add_get("/api/admin/tenants-table", admin_tenants_table_fragment)


async def admin_dashboard(request: web.Request):
    user = request["user"]
    bootstrapped = nsc.is_bootstrapped()
    tenants = _build_tenants_list()

    user_list = []
    try:
        user_list = users.list_users()
    except Exception:
        pass

    return aiohttp_jinja2.render_template("admin/dashboard.html", request, {
        "user": user,
        "nav": "admin",
        "bootstrapped": bootstrapped,
        "nats_host": config.NATS_HOST,
        "nats_port": config.NATS_PORT,
        "tenant_count": len(tenants),
        "user_count": len([u for u in user_list if u.get("role") != "admin"]),
        "tenants": tenants,
    })


def _build_tenants_list() -> list[dict]:
    """Build tenant display data for the admin dashboard."""
    tenants_summary = credentials.get_tenants_summary()
    live_counts = {}
    try:
        live_counts = registry_client.count_all_devices()
    except Exception:
        pass

    user_list = []
    try:
        user_list = users.list_users()
    except Exception:
        pass

    all_tenant_names = set(tenants_summary.keys())
    for u in user_list:
        if u["role"] != "admin":
            all_tenant_names.add(u["tenant"])

    tenants = []
    for name in sorted(all_tenant_names):
        summary = tenants_summary.get(name, {})
        live = live_counts.get(name, {})
        user_info = next((u for u in user_list if u.get("tenant") == name), None)
        tenants.append({
            "name": name,
            "cred_count": summary.get("device_count", 0),
            "total": live.get("total", 0),
            "online": live.get("online", 0),
            "created_at": user_info.get("created_at", "")[:10] if user_info else "",
        })
    return tenants


async def admin_tenants_table_fragment(request: web.Request):
    """Return the tenants table as an HTML fragment for htmx polling."""
    tenants = _build_tenants_list()
    return aiohttp_jinja2.render_template("admin/_tenants_table.html", request, {
        "tenants": tenants,
        "user": request["user"],
    })


async def admin_view_as_user(request: web.Request):
    """View a tenant's dashboard as if you were that user (read-only)."""
    user = request["user"]
    tenant_name = request.match_info["name"]

    creds = credentials.list_credentials(tenant=tenant_name)
    live_devices = []
    try:
        live_devices = registry_client.list_live_devices(tenant_name)
    except Exception:
        pass

    online_count = sum(1 for d in live_devices if d.get("status") == "available")

    return aiohttp_jinja2.render_template("admin/tenant_detail.html", request, {
        "user": user,
        "nav": "admin",
        "viewing_as": tenant_name,
        "creds_count": len(creds),
        "online_count": online_count,
        "registered_count": len(live_devices),
        "credentials": creds,
    })


async def admin_view_as_user_devices(request: web.Request):
    """View a tenant's devices page (read-only)."""
    user = request["user"]
    tenant_name = request.match_info["name"]
    creds = credentials.list_credentials(tenant=tenant_name)

    return aiohttp_jinja2.render_template("devices/list.html", request, {
        "user": user,
        "nav": "admin",
        "viewing_as": tenant_name,
        "tenant": tenant_name,
        "credentials": creds,
        "nats_host": config.NATS_HOST,
        "nats_port": config.NATS_PORT,
        "readonly": True,
    })


async def admin_health_page(request: web.Request):
    return aiohttp_jinja2.render_template("admin/health.html", request, {
        "user": request["user"],
        "nav": "health",
        "results": None,
    })


async def admin_health_verify(request: web.Request):
    """Run verification and return results as HTML fragment."""
    results = await verification.run_verification()
    return aiohttp_jinja2.render_template("admin/_health_results.html", request, {
        "results": results,
        "user": request["user"],
    })


async def admin_setup_page(request: web.Request):
    return aiohttp_jinja2.render_template("admin/setup.html", request, {
        "user": request["user"],
        "nav": "admin",
        "bootstrapped": nsc.is_bootstrapped(),
        "nats_host": config.NATS_HOST,
    })


async def admin_setup_submit(request: web.Request):
    """Run bootstrap and return result as HTML fragment."""
    data = await request.post()
    nats_host = data.get("nats_host", "").strip()
    nats_port = data.get("nats_port", "4222").strip()

    if not nats_host:
        return web.Response(
            text='<div class="bg-red-50 border border-red-200 rounded-xl p-4 text-sm text-red-700">NATS host is required</div>',
            content_type="text/html",
        )

    try:
        result = await nsc.bootstrap(nats_host, nats_port)
        html = (
            '<div class="bg-green-50 border border-green-200 rounded-xl p-5">'
            '<svg class="w-6 h-6 text-green-500 mb-2" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>'
            '<p class="text-sm font-semibold text-green-800 mb-2">Bootstrap complete!</p>'
            f'<p class="text-xs text-green-700">Operator: {result["operator"]}</p>'
            f'<p class="text-xs text-green-700">Account: {result["account"]}</p>'
            f'<p class="text-xs text-green-700">NATS: {result["nats_host"]}:{result["nats_port"]}</p>'
            f'<p class="text-xs text-green-700 mb-3">Privileged credentials: {", ".join(result["privileged_creds"])}</p>'
            '<a href="/admin" class="inline-flex items-center px-4 py-2 bg-green-600 text-white text-sm font-medium rounded-lg hover:bg-green-700 transition-colors">Go to Dashboard</a>'
            '</div>'
        )
        return web.Response(text=html, content_type="text/html")
    except Exception as e:
        return web.Response(
            text=f'<div class="bg-red-50 border border-red-200 rounded-xl p-4 text-sm text-red-700">Bootstrap failed: {e}</div>',
            content_type="text/html",
        )


async def admin_nats_reload(request: web.Request):
    """Reload NATS config. Returns status HTML fragment."""
    result = await nats_admin.reload_nats()
    if result["success"]:
        return web.Response(
            text=f'<div class="rounded-lg p-3 bg-green-50 text-green-700 text-sm border border-green-200">{result["message"]}</div>',
            content_type="text/html",
        )
    else:
        return web.Response(
            text=f'<div class="rounded-lg p-3 bg-yellow-50 text-yellow-700 text-sm border border-yellow-200">{result["message"]}</div>',
            content_type="text/html",
        )
