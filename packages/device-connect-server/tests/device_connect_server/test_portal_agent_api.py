# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the /api/agent/v1/* agent API.

Covers auth middleware behavior, scope enforcement, and the read endpoints
that must return whole sub-objects (status, identity, capabilities) byte-equal
with the registry document.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from device_connect_server.portal.app import auth_middleware
from device_connect_server.portal.services import tokens as tokens_svc
from device_connect_server.portal.views import agent_api


# A registry doc with extra fields the API must surface untouched.
SAMPLE_DEVICE = {
    "device_id": "acme-cam-001",
    "tenant": "acme",
    "identity": {
        "device_type": "camera",
        "manufacturer": "Acme",
        "model": "C-1",
        "firmware_version": "1.2.3",
        "description": "lobby cam",
        "x_custom_extra_field": "registry-added-later",
    },
    "status": {
        "ts": 1746480000.123,
        "availability": "available",
        "location": "warehouse1/loading-dock",
        "busy_score": 0.12,
        "battery": 87,
        "online": True,
        "error_state": None,
        "x_some_new_status_field": "added-by-newer-driver",
    },
    "capabilities": {
        "description": "Camera with motion detection",
        "functions": [
            {"name": "capture_frame", "description": "Take a picture",
             "parameters": {"type": "object"}, "tags": []},
        ],
        "events": [
            {"name": "motion_detected", "description": "...",
             "payload_schema": {"type": "object"}, "tags": []},
        ],
    },
    "registry": {"registered_at": "2026-05-01T12:00:00+00:00"},
}


@pytest.fixture
def fake_record():
    """A fake (verified) token record used by the auth middleware."""
    return {
        "token_id": "abc123",
        "username": "alice",
        "tenant": "acme",
        "role": "user",
        "scopes": ["devices:read"],
        "created_at": "2026-05-01T00:00:00+00:00",
    }


@pytest.fixture
def admin_record():
    return {
        "token_id": "admin0",
        "username": "root",
        "tenant": "acme",
        "role": "admin",
        "scopes": ["devices:read", "devices:provision", "devices:credentials",
                   "devices:invoke", "events:read", "admin:*"],
        "created_at": "2026-05-01T00:00:00+00:00",
    }


def _build_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    agent_api.setup_routes(app)
    return app


@pytest.fixture
async def client(fake_record):
    app = _build_app()
    server = TestServer(app)
    async with server:
        async with TestClient(server) as cli:
            with patch.object(tokens_svc, "verify_token", return_value=fake_record):
                yield cli


@pytest.fixture
async def admin_client(admin_record):
    app = _build_app()
    server = TestServer(app)
    async with server:
        async with TestClient(server) as cli:
            with patch.object(tokens_svc, "verify_token", return_value=admin_record):
                yield cli


def H(token: str = "dcp_x_y") -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── auth middleware ───────────────────────────────────────────────


class TestAgentAuth:
    async def test_no_token_returns_json_401(self):
        app = _build_app()
        server = TestServer(app)
        async with server:
            async with TestClient(server) as cli:
                r = await cli.get("/api/agent/v1/me")
                assert r.status == 401
                assert r.headers["Content-Type"].startswith("application/json")
                body = await r.json()
                assert body["success"] is False
                assert body["error"]["code"] == "missing_token"

    async def test_bad_token_returns_json_401(self):
        app = _build_app()
        server = TestServer(app)
        async with server:
            async with TestClient(server) as cli:
                with patch.object(tokens_svc, "verify_token", return_value=None):
                    r = await cli.get("/api/agent/v1/me", headers=H("dcp_bad"))
                    assert r.status == 401
                    body = await r.json()
                    assert body["error"]["code"] == "invalid_token"


# ── /me ───────────────────────────────────────────────────────────


class TestMe:
    async def test_me_echoes_token_record(self, client, fake_record):
        r = await client.get("/api/agent/v1/me", headers=H())
        assert r.status == 200
        body = await r.json()
        assert body["username"] == fake_record["username"]
        assert body["tenant"] == fake_record["tenant"]
        assert body["scopes"] == fake_record["scopes"]


# ── status / identity / capabilities (whole-object contract) ──────


class TestDeviceReadEndpoints:
    async def test_status_returns_entire_status_subobject(self, client):
        with patch("device_connect_server.portal.views.agent_api.registry_client.get_device",
                   return_value=SAMPLE_DEVICE):
            r = await client.get("/api/agent/v1/devices/acme-cam-001/status", headers=H())
            assert r.status == 200
            body = await r.json()
            # KEY ASSERTION: status field is byte-equal with the registry doc.
            assert body["status"] == SAMPLE_DEVICE["status"]
            # Includes the extra field a future driver added:
            assert body["status"]["x_some_new_status_field"] == "added-by-newer-driver"
            assert body["device_id"] == "acme-cam-001"

    async def test_identity_returns_entire_identity_subobject(self, client):
        with patch("device_connect_server.portal.views.agent_api.registry_client.get_device",
                   return_value=SAMPLE_DEVICE):
            r = await client.get("/api/agent/v1/devices/acme-cam-001/identity", headers=H())
            assert r.status == 200
            body = await r.json()
            assert body["identity"] == SAMPLE_DEVICE["identity"]
            assert body["identity"]["x_custom_extra_field"] == "registry-added-later"

    async def test_capabilities_returns_functions_and_events(self, client):
        with patch("device_connect_server.portal.views.agent_api.registry_client.get_device",
                   return_value=SAMPLE_DEVICE):
            r = await client.get(
                "/api/agent/v1/devices/acme-cam-001/capabilities", headers=H())
            assert r.status == 200
            body = await r.json()
            assert body["capabilities"]["functions"] == SAMPLE_DEVICE["capabilities"]["functions"]
            assert body["capabilities"]["events"] == SAMPLE_DEVICE["capabilities"]["events"]

    async def test_functions_endpoint_byte_equal_with_capabilities_subarray(self, client):
        with patch("device_connect_server.portal.views.agent_api.registry_client.get_device",
                   return_value=SAMPLE_DEVICE):
            r = await client.get(
                "/api/agent/v1/devices/acme-cam-001/functions", headers=H())
            body = await r.json()
            assert body["functions"] == SAMPLE_DEVICE["capabilities"]["functions"]

    async def test_events_endpoint_byte_equal_with_capabilities_subarray(self, client):
        with patch("device_connect_server.portal.views.agent_api.registry_client.get_device",
                   return_value=SAMPLE_DEVICE):
            r = await client.get(
                "/api/agent/v1/devices/acme-cam-001/events", headers=H())
            body = await r.json()
            assert body["events"] == SAMPLE_DEVICE["capabilities"]["events"]

    async def test_get_returns_whole_doc(self, client):
        with patch("device_connect_server.portal.views.agent_api.registry_client.get_device",
                   return_value=SAMPLE_DEVICE):
            r = await client.get("/api/agent/v1/devices/acme-cam-001", headers=H())
            assert r.status == 200
            body = await r.json()
            assert body == SAMPLE_DEVICE

    async def test_not_found_is_json_404(self, client):
        with patch("device_connect_server.portal.views.agent_api.registry_client.get_device",
                   return_value=None):
            r = await client.get("/api/agent/v1/devices/missing/status", headers=H())
            assert r.status == 404
            body = await r.json()
            assert body["error"]["code"] == "not_found"


# ── scope enforcement ─────────────────────────────────────────────


class TestScopeEnforcement:
    async def test_invoke_requires_devices_invoke(self, client):
        # fake_record has only devices:read
        r = await client.post(
            "/api/agent/v1/devices/cam-001/invoke", headers=H(),
            json={"function": "capture"})
        assert r.status == 403
        body = await r.json()
        assert body["error"]["code"] == "missing_scope"

    async def test_provision_requires_devices_provision(self, client):
        r = await client.post(
            "/api/agent/v1/devices", headers=H(), json={"device_name": "cam-002"})
        assert r.status == 403
        body = await r.json()
        assert body["error"]["code"] == "missing_scope"

    async def test_stream_requires_events_read(self, client):
        r = await client.get(
            "/api/agent/v1/devices/cam-001/events/motion/stream?duration=1",
            headers=H())
        assert r.status == 403


# ── tenant override ───────────────────────────────────────────────


class TestTenantOverride:
    async def test_non_admin_tenant_override_forbidden(self, client):
        r = await client.get("/api/agent/v1/fleet?tenant=other", headers=H())
        assert r.status == 403
        body = await r.json()
        assert body["error"]["code"] == "tenant_override_forbidden"

    async def test_admin_tenant_override_allowed(self, admin_client):
        with patch(
            "device_connect_server.portal.views.agent_api.registry_client.list_live_devices",
            return_value=[],
        ), patch(
            "device_connect_server.portal.views.agent_api.credentials_svc.list_credentials",
            return_value=[],
        ):
            r = await admin_client.get("/api/agent/v1/fleet?tenant=other", headers=H())
            assert r.status == 200
            body = await r.json()
            assert body["tenant"] == "other"


# ── stream argument validation ────────────────────────────────────


class TestStreamArgValidation:
    async def test_stream_requires_a_bound(self, admin_client):
        r = await admin_client.get(
            "/api/agent/v1/devices/cam-001/events/motion/stream", headers=H())
        assert r.status == 400
        body = await r.json()
        assert body["error"]["code"] == "missing_bound"

    async def test_stream_rejects_invalid_format(self, admin_client):
        r = await admin_client.get(
            "/api/agent/v1/devices/cam-001/events/motion/stream"
            "?format=xml&duration=1",
            headers=H())
        assert r.status == 400

    async def test_stream_rejects_negative_duration(self, admin_client):
        r = await admin_client.get(
            "/api/agent/v1/devices/cam-001/events/motion/stream?duration=-1",
            headers=H())
        assert r.status == 400
