"""Authentication views: login, signup, logout."""

import html as _html
import re

import aiohttp_jinja2
from aiohttp import web

from ..app import set_session, clear_session
from ..services import users
from ..services.backend import get_backend, validate_name


def setup_routes(app: web.Application):
    app.router.add_get("/login", login_page)
    app.router.add_post("/login", login_submit)
    app.router.add_get("/signup", signup_page)
    app.router.add_post("/signup", signup_submit)
    app.router.add_post("/logout", logout)


async def login_page(request: web.Request):
    return aiohttp_jinja2.render_template("login.html", request, {"error": None})


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
    redirect_url = "/admin" if user["role"] == "admin" else "/dashboard"
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

    # Create user account
    try:
        user = users.create_user(username, password, role="user")
    except ValueError as e:
        escaped = _html.escape(str(e))
        error_html = f'<div class="mb-4 rounded-lg p-3 bg-red-50 text-red-700 text-sm border border-red-200">{escaped}</div>'
        if _is_htmx(request):
            return web.Response(text=error_html, content_type="text/html")
        return aiohttp_jinja2.render_template("signup.html", request, {"error": str(e)})

    # Create tenant namespace with initial device credentials
    backend = get_backend()
    if backend.is_bootstrapped():
        try:
            validate_name(username, "tenant")
            broker_info = backend.broker_display_info()
            await backend.create_tenant(
                username, num_devices=3,
                host=broker_info["host"], port=broker_info["port"],
            )
            await backend.reload_broker()
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "Tenant creation failed during signup for user '%s'", username,
            )

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
