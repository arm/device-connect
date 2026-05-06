# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""aiohttp application factory with session middleware and route registration."""

import base64
import logging
from pathlib import Path

import aiohttp_jinja2
import jinja2
from aiohttp import web

from . import config

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# Routes that don't require authentication
PUBLIC_ROUTES = {"/", "/login", "/signup", "/api/login", "/api/signup"}

# Agent API namespace — Bearer-token authenticated, JSON-only errors.
AGENT_API_PREFIX = "/api/agent/v1/"

# Subpaths under the agent API that are intentionally public (no Bearer required).
# These power the browser-mediated CLI login flow.
AGENT_API_PUBLIC_SUBPATHS = ("auth/cli/init", "auth/cli/poll")


def _json_error(status: int, code: str, message: str) -> web.Response:
    return web.json_response(
        {"success": False, "error": {"code": code, "message": message}},
        status=status,
    )


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Authenticate browser sessions via cookie, agent API via Bearer token."""
    path = request.path

    # Allow public routes and static files
    if path in PUBLIC_ROUTES or path.startswith("/static"):
        return await handler(request)

    # Agent API: Bearer token, JSON 401/403 — never HTML, never redirects.
    if path.startswith(AGENT_API_PREFIX):
        suffix = path[len(AGENT_API_PREFIX):]
        if any(suffix == p or suffix.startswith(p + "/") for p in AGENT_API_PUBLIC_SUBPATHS):
            return await handler(request)

        from .services import tokens as tokens_svc

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return _json_error(401, "missing_token", "Authorization: Bearer <token> required")
        token = auth_header[len("Bearer "):].strip()
        record = tokens_svc.verify_token(token)
        if not record:
            return _json_error(401, "invalid_token", "Token is invalid, revoked, or expired")
        request["token"] = record
        request["user"] = {
            "username": record["username"],
            "tenant": record["tenant"],
            "role": record["role"],
        }
        return await handler(request)

    # Browser session path
    session = await _get_session(request)
    if not session.get("username"):
        # Preserve the requested URL so post-login redirect lands the user
        # back on (e.g.) the CLI approval page.
        next_url = path
        if request.query_string:
            next_url = f"{path}?{request.query_string}"
        from urllib.parse import quote
        login_url = "/login?next=" + quote(next_url, safe="") if path != "/login" else "/login"
        if request.headers.get("HX-Request"):
            resp = web.Response(status=200)
            resp.headers["HX-Redirect"] = login_url
            return resp
        raise web.HTTPFound(login_url)

    request["user"] = session
    return await handler(request)


@web.middleware
async def admin_middleware(request: web.Request, handler):
    """Block non-admin users from /admin/* routes."""
    if request.path.startswith("/admin") or request.path.startswith("/api/admin"):
        session = await _get_session(request)
        if session.get("role") != "admin":
            raise web.HTTPForbidden(text="Admin access required")
    return await handler(request)


async def _get_session(request: web.Request) -> dict:
    """Simple cookie-based session. Stores JSON in a signed cookie."""
    import hashlib
    import hmac
    import json

    cookie = request.cookies.get("portal_session", "")
    if not cookie:
        return {}

    try:
        parts = cookie.split(".", 1)
        if len(parts) != 2:
            return {}
        payload_b64, sig = parts
        # Verify signature
        expected_sig = hmac.new(
            config.SESSION_SECRET.encode(), payload_b64.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return {}
        payload = base64.b64decode(payload_b64).decode()
        return json.loads(payload)
    except Exception:
        return {}


def set_session(response: web.Response, data: dict):
    """Set session cookie on response."""
    import hashlib
    import hmac
    import json

    payload = base64.b64encode(json.dumps(data).encode()).decode()
    sig = hmac.new(
        config.SESSION_SECRET.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    cookie_value = f"{payload}.{sig}"
    response.set_cookie(
        "portal_session", cookie_value,
        httponly=True, samesite="Lax", max_age=86400,
        secure=config.SESSION_SECURE_COOKIE or None,
    )


def clear_session(response: web.Response):
    """Clear session cookie."""
    response.del_cookie("portal_session")


def create_app() -> web.Application:
    """Create and configure the portal application."""
    app = web.Application(middlewares=[auth_middleware, admin_middleware])

    # Setup Jinja2 templates
    import json as _json
    env = aiohttp_jinja2.setup(
        app,
        loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
    )
    env.filters["tojson_pretty"] = lambda v: _json.dumps(v, indent=2, default=str)

    # Static files
    app.router.add_static("/static", STATIC_DIR, name="static")

    # Register routes
    from .views import (
        auth, dashboard, devices, admin, agent_api,
        cli_auth as cli_auth_view, coding_agents,
    )
    auth.setup_routes(app)
    dashboard.setup_routes(app)
    devices.setup_routes(app)
    admin.setup_routes(app)
    agent_api.setup_routes(app)
    cli_auth_view.setup_routes(app)
    coding_agents.setup_routes(app)

    # Seed admin on startup
    app.on_startup.append(_on_startup)

    return app


async def _on_startup(app: web.Application):
    """Seed admin account on startup."""
    try:
        from .services.users import ensure_admin
        ensure_admin()
    except Exception as e:
        logger.warning("Could not seed admin account (etcd may not be ready): %s", e)
