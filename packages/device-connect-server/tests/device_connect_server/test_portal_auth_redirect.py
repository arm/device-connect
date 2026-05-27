# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Browser-session auth redirect tests.

After a portal restart the dashboard's 10s htmx poll on
``/api/devices/live`` fires without a session cookie. The auth
middleware used to capture that fragment URL as the post-login
``next`` target, so re-logging in dropped the user onto the bare
fragment instead of ``/dashboard``. These tests pin the fix: fragment
endpoints (htmx requests, and anything under ``/api/``) must not become
post-login destinations.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from device_connect_server.portal.app import auth_middleware
from device_connect_server.portal.views.auth import _safe_next


def _next_param(location: str) -> str | None:
    """Extract the ``next`` query param from a Location header.

    aiohttp normalizes percent-encoding in Location, so we compare the
    decoded value rather than the raw header.
    """
    qs = parse_qs(urlsplit(location).query)
    values = qs.get("next")
    return values[0] if values else None


# ── _safe_next ────────────────────────────────────────────────────


class TestSafeNext:
    def test_full_page_path_accepted(self):
        assert _safe_next("/dashboard") == "/dashboard"
        assert _safe_next("/cli/approval/xyz") == "/cli/approval/xyz"

    def test_external_url_rejected(self):
        assert _safe_next("https://evil.example.com/") is None

    def test_protocol_relative_rejected(self):
        assert _safe_next("//evil.example.com/path") is None

    def test_crlf_rejected(self):
        assert _safe_next("/dashboard\r\nSet-Cookie: evil=1") is None
        assert _safe_next("/dashboard\nfoo") is None

    def test_api_path_rejected(self):
        """``/api/`` returns JSON/HTML fragments, not full pages. Even if
        a stale or malicious link arrives with ``?next=/api/...``, login
        must not land the user there."""
        assert _safe_next("/api/devices/live?tenant=alpha") is None
        assert _safe_next("/api/devices/cam-001/live-detail") is None
        assert _safe_next("/api/admin/tenants-table") is None

    def test_static_path_rejected(self):
        assert _safe_next("/static/css/app.css") is None

    def test_empty_or_none(self):
        assert _safe_next(None) is None
        assert _safe_next("") is None


# ── auth_middleware redirect target ──────────────────────────────


async def _stub(_request):
    return web.Response(text="ok")


def _build_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    # Routes that the auth middleware will gate. We don't actually
    # reach the handler since the middleware short-circuits with a
    # redirect when there's no session cookie.
    app.router.add_get("/dashboard", _stub)
    app.router.add_get("/api/devices/live", _stub)
    app.router.add_get("/cli/approval/{token}", _stub)
    return app


class TestAuthMiddlewareRedirect:
    """The unauthenticated browser-session path."""

    async def test_htmx_poll_does_not_capture_fragment_as_next(self):
        """The original repro: an htmx poll on a fragment endpoint
        triggers an HX-Redirect to /login *without* ``?next=`` so the
        post-login destination falls back to /dashboard."""
        app = _build_app()
        async with TestServer(app) as server:
            async with TestClient(server) as cli:
                r = await cli.get(
                    "/api/devices/live?tenant=alpha",
                    headers={"HX-Request": "true"},
                    allow_redirects=False,
                )
                assert r.status == 200
                redirect = r.headers.get("HX-Redirect", "")
                assert redirect == "/login", (
                    f"htmx fragment poll must redirect to /login with no "
                    f"``next`` capture; got {redirect!r}"
                )

    async def test_api_request_does_not_capture_next(self):
        """Even a non-htmx GET on an /api/ path (e.g. a script or stale
        link) must not become a post-login destination."""
        app = _build_app()
        async with TestServer(app) as server:
            async with TestClient(server) as cli:
                r = await cli.get(
                    "/api/devices/live?tenant=alpha",
                    allow_redirects=False,
                )
                assert r.status == 302
                assert r.headers["Location"] == "/login"

    async def test_top_level_navigation_captures_next(self):
        """Top-level HTML navigation to an authenticated page still
        gets a ``next`` capture so the CLI-approval flow works."""
        app = _build_app()
        async with TestServer(app) as server:
            async with TestClient(server) as cli:
                r = await cli.get(
                    "/cli/approval/abc123",
                    allow_redirects=False,
                )
                assert r.status == 302
                location = r.headers["Location"]
                assert location.startswith("/login?next=")
                assert _next_param(location) == "/cli/approval/abc123"

    async def test_top_level_dashboard_captures_next(self):
        """Sanity check: regular page navigation still captures the
        page URL as ``next`` so login lands the user back where they
        were."""
        app = _build_app()
        async with TestServer(app) as server:
            async with TestClient(server) as cli:
                r = await cli.get("/dashboard", allow_redirects=False)
                assert r.status == 302
                location = r.headers["Location"]
                assert location.startswith("/login?next=")
                assert _next_param(location) == "/dashboard"
