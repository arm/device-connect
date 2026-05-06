# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Coding Agents tab: AGENTS.md handoff + per-user API token CRUD."""

from __future__ import annotations

import html as _html
import logging

import aiohttp_jinja2
from aiohttp import web

from ..services import tokens as tokens_svc

logger = logging.getLogger(__name__)


# Scopes a non-admin user may grant on a token they mint themselves.
USER_SCOPES = [
    "devices:read",
    "devices:provision",
    "devices:credentials",
    "devices:invoke",
    "events:read",
]

# Additional scopes available only to admins.
ADMIN_SCOPES = ["admin:tenants", "admin:*"]


def setup_routes(app: web.Application):
    app.router.add_get("/coding-agents", coding_agents_page)
    app.router.add_get("/coding-agents/AGENTS.md", download_agents_md)
    app.router.add_get("/coding-agents/tokens", tokens_list_fragment)
    app.router.add_post("/coding-agents/tokens", create_token)
    app.router.add_post(
        "/coding-agents/tokens/{token_id}/revoke", revoke_token,
    )


def _public_host(request: web.Request) -> str:
    """Extract the public hostname/IP from the request (strip port)."""
    return request.host.rsplit(":", 1)[0]


def _portal_url(request: web.Request) -> str:
    return f"{request.scheme}://{request.host}"


def _allowed_scopes(role: str) -> list[str]:
    if role == "admin":
        return USER_SCOPES + ADMIN_SCOPES
    return list(USER_SCOPES)


def _list_user_tokens(user: dict) -> list[dict]:
    return tokens_svc.list_tokens(
        username=user["username"], tenant=user["tenant"],
    )


async def coding_agents_page(request: web.Request) -> web.Response:
    user = request["user"]
    tenant = user["tenant"]
    portal_url = _portal_url(request)
    public_host = _public_host(request)
    tokens = _list_user_tokens(user)

    return aiohttp_jinja2.render_template(
        "coding_agents/page.html", request, {
            "user": user,
            "nav": "coding-agents",
            "tenant": tenant,
            "portal_url": portal_url,
            "public_host": public_host,
            "tokens": tokens,
            "allowed_scopes": _allowed_scopes(user.get("role", "user")),
            "default_scopes": list(USER_SCOPES),
        },
    )


async def download_agents_md(request: web.Request) -> web.Response:
    """Render AGENTS.md with the user's tenant + portal URL pre-filled."""
    user = request["user"]
    tenant = user["tenant"]
    text = aiohttp_jinja2.render_string(
        "coding_agents/AGENTS.md.j2", request, {
            "tenant": tenant,
            "portal_url": _portal_url(request),
            "public_host": _public_host(request),
            "example_device_id": f"{tenant}-cam-001",
        },
    )
    return web.Response(
        text=text,
        content_type="text/markdown",
        charset="utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="AGENTS.md"',
        },
    )


async def tokens_list_fragment(request: web.Request) -> web.Response:
    """HTMX fragment: render the rows of the active-tokens table."""
    user = request["user"]
    tokens = _list_user_tokens(user)
    return aiohttp_jinja2.render_template(
        "coding_agents/_tokens_table.html", request, {
            "tokens": tokens, "user": user,
        },
    )


def _error_fragment(message: str) -> web.Response:
    return web.Response(
        text=(
            '<div class="rounded-lg bg-red-50 border border-red-200 '
            'px-4 py-3 text-sm text-red-700">'
            f"{_html.escape(message)}</div>"
        ),
        content_type="text/html",
    )


async def create_token(request: web.Request) -> web.Response:
    """Mint a token for the current user. HTMX returns the one-time secret panel."""
    user = request["user"]
    role = user.get("role", "user")
    data = await request.post()

    label = (data.get("label") or "").strip()
    if len(label) > 80:
        return _error_fragment("Label must be 80 characters or fewer.")

    requested_scopes = data.getall("scopes") if hasattr(data, "getall") else []
    # aiohttp's MultiDict supports getall; fall back to single value if not.
    if not requested_scopes:
        single = data.get("scopes")
        requested_scopes = [single] if single else []

    allowed = set(_allowed_scopes(role))
    filtered = [s for s in requested_scopes if s in allowed]
    if not filtered:
        return _error_fragment("Pick at least one scope.")

    try:
        record = tokens_svc.create_token(
            username=user["username"],
            tenant=user["tenant"],
            role=role,
            scopes=filtered,
            label=label,
        )
    except ValueError as e:
        return _error_fragment(str(e))
    except Exception as e:  # pragma: no cover - defensive: etcd outage etc.
        logger.exception("Failed to create token")
        return _error_fragment(f"Failed to create token: {e}")

    return aiohttp_jinja2.render_template(
        "coding_agents/_token_secret.html", request, {
            "record": record,
        },
    )


async def revoke_token(request: web.Request) -> web.Response:
    """Revoke a token owned by the current user. Returns the refreshed table."""
    user = request["user"]
    token_id = request.match_info["token_id"]

    record = tokens_svc.get_token_record(token_id)
    if not record:
        raise web.HTTPNotFound(text="Token not found")
    # IDOR guard: only the owner (or an admin) can revoke.
    if record["username"] != user["username"] and user.get("role") != "admin":
        raise web.HTTPForbidden(text="Cannot revoke another user's token")

    tokens_svc.revoke_token(token_id)

    tokens = _list_user_tokens(user)
    return aiohttp_jinja2.render_template(
        "coding_agents/_tokens_table.html", request, {
            "tokens": tokens, "user": user,
        },
    )
