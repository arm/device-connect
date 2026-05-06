# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Browser-side CLI auth approval pages.

The CLI hands the user a URL like /auth/cli/<request_id>. If the user is not
logged in, the global auth middleware sends them to /login?next=<this URL>;
once logged in they land on the approval page, click Approve, and the
portal mints a token bound to their account that the CLI's poll receives.
"""

from __future__ import annotations

import logging

import aiohttp_jinja2
from aiohttp import web

from ..services import cli_auth as cli_auth_svc

logger = logging.getLogger(__name__)


def setup_routes(app: web.Application):
    app.router.add_get("/auth/cli/{request_id}", cli_approve_page)
    app.router.add_post("/auth/cli/{request_id}", cli_approve_submit)


def _safe_request_id(request_id: str) -> str | None:
    """Reject anything that isn't 32 hex chars to keep this tight."""
    if len(request_id) != 32:
        return None
    if not all(c in "0123456789abcdef" for c in request_id.lower()):
        return None
    return request_id


async def cli_approve_page(request: web.Request) -> web.Response:
    request_id = _safe_request_id(request.match_info["request_id"])
    user = request.get("user") or {}
    record = cli_auth_svc.get(request_id) if request_id else None

    error = None
    if record is None:
        error = "This CLI login link is invalid, expired, or already consumed."
    elif record["status"] != cli_auth_svc.PENDING:
        error = f"This CLI login is already {record['status']}."
    elif cli_auth_svc._is_expired(record):
        error = "This CLI login link has expired. Please re-run `dc-portalctl auth login`."

    return aiohttp_jinja2.render_template(
        "auth/cli_approve.html", request,
        {"user": user, "record": record, "request_id": request_id, "error": error},
    )


async def cli_approve_submit(request: web.Request) -> web.Response:
    request_id = _safe_request_id(request.match_info["request_id"])
    user = request.get("user") or {}
    if request_id is None:
        return aiohttp_jinja2.render_template(
            "auth/cli_approve.html", request,
            {"user": user, "record": None, "request_id": None,
             "error": "Invalid request id."},
        )

    data = await request.post()
    action = (data.get("action") or "").strip()

    if action == "deny":
        try:
            cli_auth_svc.deny(request_id=request_id, user=user)
        except cli_auth_svc.CliAuthError as e:
            return aiohttp_jinja2.render_template(
                "auth/cli_approve.html", request,
                {"user": user, "record": None, "request_id": request_id, "error": str(e)},
            )
        return aiohttp_jinja2.render_template(
            "auth/cli_approve.html", request,
            {"user": user, "record": None, "request_id": request_id,
             "denied": True, "error": None},
        )

    if action == "approve":
        try:
            updated = cli_auth_svc.approve(request_id=request_id, user=user)
        except cli_auth_svc.CliAuthError as e:
            return aiohttp_jinja2.render_template(
                "auth/cli_approve.html", request,
                {"user": user, "record": None, "request_id": request_id, "error": str(e)},
            )
        return aiohttp_jinja2.render_template(
            "auth/cli_approve.html", request,
            {"user": user, "record": updated, "request_id": request_id,
             "approved": True, "error": None},
        )

    return aiohttp_jinja2.render_template(
        "auth/cli_approve.html", request,
        {"user": user, "record": cli_auth_svc.get(request_id), "request_id": request_id,
         "error": "Unknown action."},
    )
