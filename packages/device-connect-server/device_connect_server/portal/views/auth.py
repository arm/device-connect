# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Authentication views: login, signup, logout."""

import html as _html
import re

import aiohttp_jinja2
from aiohttp import web

from ..app import set_session, clear_session, _get_session
from ..services import users
from ..services.backend import get_backend, validate_name


def setup_routes(app: web.Application):
    app.router.add_get("/", root_page)
    app.router.add_get("/login", login_page)
    app.router.add_post("/login", login_submit)
    app.router.add_get("/signup", signup_page)
    app.router.add_post("/signup", signup_submit)
    app.router.add_post("/logout", logout)


async def root_page(request: web.Request):
    session = await _get_session(request)
    if session.get("username"):
        target = "/admin" if session.get("role") == "admin" else "/dashboard"
        raise web.HTTPFound(target)
    raise web.HTTPFound("/login")


def _safe_next(value: str | None) -> str | None:
    """Accept only relative paths so an attacker can't bounce login to an external site."""
    if not value:
        return None
    if not value.startswith("/") or value.startswith("//"):
        return None
    if "\n" in value or "\r" in value:
        return None
    return value


async def login_page(request: web.Request):
    session = await _get_session(request)
    next_url = _safe_next(request.query.get("next"))
    if session.get("username"):
        target = next_url or ("/admin" if session.get("role") == "admin" else "/dashboard")
        raise web.HTTPFound(target)
    return aiohttp_jinja2.render_template("login.html", request, {"error": None, "next": next_url})


async def login_submit(request: web.Request):
    data = await request.post()
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        error_html = '<div class="mb-4 rounded-lg p-3 bg-red-50 text-red-700 text-sm border border-red-200">Username and password are required</div>'
        if _is_htmx(request):
            return web.Response(text=error_html, content_type="text/html")
        return aiohttp_jinja2.render_template("login.html", request, {"error": "Username and password are required"})

    user = users.authenticate(username, password)
    if not user:
        error_html = '<div class="mb-4 rounded-lg p-3 bg-red-50 text-red-700 text-sm border border-red-200">Invalid username or password</div>'
        if _is_htmx(request):
            return web.Response(text=error_html, content_type="text/html")
        return aiohttp_jinja2.render_template("login.html", request, {"error": "Invalid username or password"})

    # Set session and redirect
    next_url = _safe_next(data.get("next"))
    redirect_url = next_url or ("/admin" if user["role"] == "admin" else "/dashboard")
    if _is_htmx(request):
        response = web.Response(status=200)
        response.headers["HX-Redirect"] = redirect_url
        set_session(response, {
            "username": user["username"],
            "role": user["role"],
            "tenant": user["tenant"],
        })
        return response
    response = web.HTTPFound(redirect_url)
    set_session(response, {
        "username": user["username"],
        "role": user["role"],
        "tenant": user["tenant"],
    })
    raise response


async def signup_page(request: web.Request):
    session = await _get_session(request)
    if session.get("username"):
        target = "/admin" if session.get("role") == "admin" else "/dashboard"
        raise web.HTTPFound(target)
    return aiohttp_jinja2.render_template("signup.html", request, {"error": None})


async def signup_submit(request: web.Request):
    data = await request.post()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    confirm = data.get("confirm", "")

    # Validation
    error = None
    if not username or not password:
        error = "All fields are required"
    elif not re.match(r'^[a-zA-Z0-9_-]+$', username):
        error = "Username: letters, numbers, hyphens, underscores only"
    elif len(username) < 2 or len(username) > 32:
        error = "Username must be 2-32 characters"
    elif len(password) < 4:
        error = "Password must be at least 4 characters"
    elif password != confirm:
        error = "Passwords don't match"
    elif username in ("admin", "default", "system", "portal"):
        error = "This username is reserved"

    if error:
        error_html = f'<div class="mb-4 rounded-lg p-3 bg-red-50 text-red-700 text-sm border border-red-200">{error}</div>'
        if _is_htmx(request):
            return web.Response(text=error_html, content_type="text/html")
        return aiohttp_jinja2.render_template("signup.html", request, {"error": error})

    # Validate tenant name before creating anything
    try:
        validate_name(username, "tenant")
    except ValueError as e:
        escaped = _html.escape(str(e))
        error_html = f'<div class="mb-4 rounded-lg p-3 bg-red-50 text-red-700 text-sm border border-red-200">{escaped}</div>'
        if _is_htmx(request):
            return web.Response(text=error_html, content_type="text/html")
        return aiohttp_jinja2.render_template("signup.html", request, {"error": escaped})

    # Fail fast if user already exists — avoid expensive tenant provisioning
    if users.get_user(username):
        error = _html.escape(f"User '{username}' already exists")
        error_html = f'<div class="mb-4 rounded-lg p-3 bg-red-50 text-red-700 text-sm border border-red-200">{error}</div>'
        if _is_htmx(request):
            return web.Response(text=error_html, content_type="text/html")
        return aiohttp_jinja2.render_template("signup.html", request, {"error": error})

    # Create tenant namespace first — if this fails, don't create the user
    backend = get_backend()
    if backend.is_bootstrapped():
        try:
            broker_info = backend.broker_display_info()
            await backend.create_tenant(
                username, num_devices=3,
                host=broker_info["host"], port=broker_info["port"],
            )
            await backend.reload_broker()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception(
                "Tenant creation failed during signup for user '%s'", username,
            )
            detail = _html.escape(f"{type(exc).__name__}: {exc}")
            error_msg = f"Signup failed: could not provision tenant ({detail})"
            error_html = f'<div class="mb-4 rounded-lg p-3 bg-red-50 text-red-700 text-sm border border-red-200">{error_msg}</div>'
            if _is_htmx(request):
                return web.Response(text=error_html, content_type="text/html")
            return aiohttp_jinja2.render_template("signup.html", request, {"error": error_msg})

    # Create user account only after tenant is provisioned
    try:
        user = users.create_user(username, password, role="user")
    except ValueError as e:
        escaped = _html.escape(str(e))
        error_html = f'<div class="mb-4 rounded-lg p-3 bg-red-50 text-red-700 text-sm border border-red-200">{escaped}</div>'
        if _is_htmx(request):
            return web.Response(text=error_html, content_type="text/html")
        return aiohttp_jinja2.render_template("signup.html", request, {"error": escaped})

    # Log in
    if _is_htmx(request):
        response = web.Response(status=200)
        response.headers["HX-Redirect"] = "/dashboard"
        set_session(response, {
            "username": user["username"],
            "role": user["role"],
            "tenant": user["tenant"],
        })
        return response
    response = web.HTTPFound("/dashboard")
    set_session(response, {
        "username": user["username"],
        "role": user["role"],
        "tenant": user["tenant"],
    })
    raise response


async def logout(request: web.Request):
    response = web.HTTPFound("/login")
    clear_session(response)
    raise response


def _is_htmx(request: web.Request) -> bool:
    return request.headers.get("HX-Request") == "true"
