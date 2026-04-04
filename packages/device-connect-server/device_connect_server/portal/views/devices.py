"""Device management views: create, list, download credentials and bundles."""

import aiohttp_jinja2
from aiohttp import web

from .. import config
from ..services import credentials, bundles, nsc, nats_admin, registry_client


def setup_routes(app: web.Application):
    app.router.add_get("/devices", devices_page)
    app.router.add_get("/devices/{name}", device_detail_page)
    app.router.add_post("/api/devices", create_device)
    app.router.add_get("/api/devices/{name}/creds", download_credential)
    app.router.add_get("/api/devices/bundle", download_bundle)


async def devices_page(request: web.Request):
    user = request["user"]
    tenant = user["tenant"]
    creds = credentials.list_credentials(tenant=tenant)

    return aiohttp_jinja2.render_template("devices/list.html", request, {
        "user": user,
        "nav": "devices",
        "tenant": tenant,
        "credentials": creds,
        "nats_host": config.NATS_HOST,
        "nats_port": config.NATS_PORT,
        "readonly": False,
    })


async def device_detail_page(request: web.Request):
    user = request["user"]
    tenant = user["tenant"]
    device_name = request.match_info["name"]

    # Try to get live data from registry
    device = None
    try:
        device = registry_client.get_device(tenant, device_name)
    except Exception:
        pass

    if not device:
        # Fallback to credential data
        cred_data = credentials.get_credential_data(f"{device_name}.creds.json")
        device = {
            "device_id": device_name,
            "device_type": "",
            "status": "unknown",
            "location": "",
            "last_seen": "",
            "capabilities": [],
            "tenant": cred_data.get("tenant", tenant) if cred_data else tenant,
        }

    cred_file = credentials.get_credential(f"{device_name}.creds.json")

    return aiohttp_jinja2.render_template("devices/detail.html", request, {
        "user": user,
        "nav": "devices",
        "device": device,
        "cred_filename": cred_file.name if cred_file else None,
        "nats_host": config.NATS_HOST,
        "nats_port": config.NATS_PORT,
    })


async def create_device(request: web.Request):
    """Create a new device credential. Returns HTML fragment for htmx."""
    user = request["user"]
    tenant = user["tenant"]
    data = await request.post()
    device_name = data.get("device_name", "").strip()

    if not device_name:
        return web.Response(
            text='<div class="px-5 py-3 text-sm text-red-600">Device name is required</div>',
            content_type="text/html",
        )

    # Prefix with tenant name for uniqueness
    full_name = f"{tenant}-{device_name}"

    if not nsc.is_bootstrapped():
        return web.Response(
            text='<div class="px-5 py-3 text-sm text-red-600">System not bootstrapped — ask admin to run setup first</div>',
            content_type="text/html",
        )

    try:
        await nsc.add_device(
            tenant, full_name,
            nats_host=config.NATS_HOST, nats_port=config.NATS_PORT,
        )
        await nats_admin.reload_nats()
    except Exception as e:
        return web.Response(
            text=f'<div class="px-5 py-3 text-sm text-red-600">Failed to create device: {e}</div>',
            content_type="text/html",
        )

    # Return the new row as HTML fragment
    cred = {
        "device_id": full_name,
        "filename": f"{full_name}.creds.json",
    }
    return aiohttp_jinja2.render_template("devices/_device_row.html", request, {
        "cred": cred,
        "user": user,
    })


async def download_credential(request: web.Request):
    """Download a single credential file."""
    device_name = request.match_info["name"]
    filename = f"{device_name}.creds.json"
    cred_path = credentials.get_credential(filename)

    if not cred_path:
        raise web.HTTPNotFound(text=f"Credential file not found: {filename}")

    return web.FileResponse(
        cred_path,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


async def download_bundle(request: web.Request):
    """Download a tenant credential bundle as .zip."""
    tenant = request.query.get("tenant")
    if not tenant:
        user = request["user"]
        tenant = user["tenant"]

    bundle_bytes = bundles.create_bundle(tenant)
    return web.Response(
        body=bundle_bytes,
        content_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{tenant}-credentials.zip"',
        },
    )
