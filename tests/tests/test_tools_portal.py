# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for portal-assisted local Zenoh route shortcuts.

Exercises ``DEVICE_CONNECT_PORTAL_CREDENTIALS_FILE`` bundles that pair a
portal (NATS/registry) route with an optional same-LAN Zenoh fast path:

- Prefer local Zenoh when ``DEVICE_CONNECT_PREFER_LOCAL`` is true (default)
- Fall back to the portal NATS route when the local connect fails
- Use the portal route directly when local preference is disabled

Also verifies the small-fleet shortcut (auto-expanded schemas) works when
the agent connects through the portal local route.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any
from unittest.mock import patch

import pytest

ITEST_NATS_URL = os.getenv("NATS_URL", "nats://localhost:4222")
ITEST_ZENOH_URL = os.getenv("ZENOH_CONNECT", "tcp/localhost:7447")

SETTLE_TIME = 0.3
DISCOVERY_TIMEOUT = 8.0

# Placeholder local route in the fallback bundle (connect failure is injected via mock).
BROKEN_LOCAL_ZENOH = "tcp/127.0.0.1:19999"

_URL_ENV_VARS = (
    "ZENOH_CONNECT",
    "MESSAGING_URLS",
    "NATS_URL",
    "NATS_URLS",
)


def _write_portal_bundle(
    path,
    *,
    local_routes: list[str] | None,
    local_tls: dict[str, str] | None = None,
    portal_nats_url: str = ITEST_NATS_URL,
    tenant: str = "default",
) -> None:
    data: dict[str, Any] = {"tenant": tenant, "nats": {"urls": [portal_nats_url]}}
    if local_routes:
        local: dict[str, Any] = {"routes": local_routes}
        if local_tls:
            local["tls"] = local_tls
        data["local"] = local
    path.write_text(json.dumps(data))


@pytest.fixture
def portal_bundle_path(tmp_path):
    """Factory fixture: write a portal credential bundle and return its path."""

    def _factory(
        *,
        local_routes: list[str] | None,
        local_tls: dict[str, str] | None = None,
        portal_nats_url: str = ITEST_NATS_URL,
        tenant: str = "default",
    ) -> str:
        bundle = tmp_path / f"portal-{tenant}.creds.json"
        _write_portal_bundle(
            bundle,
            local_routes=local_routes,
            local_tls=local_tls,
            portal_nats_url=portal_nats_url,
            tenant=tenant,
        )
        return str(bundle)

    return _factory


@pytest.fixture
def portal_agent_env(monkeypatch):
    """Clear broker URL env vars so only the portal bundle configures the agent."""

    def _activate(
        bundle_path: str,
        *,
        prefer_local: str | None = None,
        device_d2d: bool = False,
    ) -> None:
        for name in _URL_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("MESSAGING_BACKEND", raising=False)
        monkeypatch.delenv("DEVICE_CONNECT_DISCOVERY_MODE", raising=False)
        monkeypatch.setenv("DEVICE_CONNECT_ALLOW_INSECURE", "true")
        monkeypatch.setenv("DEVICE_CONNECT_PORTAL_CREDENTIALS_FILE", bundle_path)
        if device_d2d:
            monkeypatch.setenv("DEVICE_CONNECT_DISCOVERY_MODE", "d2d")
        if prefer_local is not None:
            monkeypatch.setenv("DEVICE_CONNECT_PREFER_LOCAL", prefer_local)
        else:
            monkeypatch.delenv("DEVICE_CONNECT_PREFER_LOCAL", raising=False)

    return _activate


async def _wait_for_device_ids(expected_ids: set[str]) -> None:
    from device_connect_agent_tools.connection import get_connection

    deadline = time.monotonic() + DISCOVERY_TIMEOUT
    while True:
        conn = get_connection()
        devices = await asyncio.to_thread(conn.list_devices)
        ids = {d.get("device_id") for d in devices}
        if expected_ids.issubset(ids) or time.monotonic() > deadline:
            return
        await asyncio.sleep(0.25)


# ── Local Zenoh shortcut ───────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.portal
async def test_portal_prefers_local_zenoh_route(
    infrastructure,
    portal_bundle_path,
    portal_agent_env,
):
    """Portal bundle connects via local Zenoh and discovers a peer on the itest router."""
    from fixtures.devices import DeviceFactory

    from device_connect_agent_tools import connect, disconnect, invoke
    from device_connect_agent_tools.connection import get_connection

    zenoh_url = ITEST_ZENOH_URL
    bundle = portal_bundle_path(local_routes=[zenoh_url])
    portal_agent_env(bundle, device_d2d=True)

    factory = DeviceFactory(messaging_url=zenoh_url)
    try:
        await factory.spawn_sensor(
            "itest-portal-local-sensor",
            initial_temp=21.0,
            initial_humidity=55.0,
        )
        await asyncio.sleep(SETTLE_TIME)

        await asyncio.to_thread(connect)
        try:
            conn = get_connection()
            assert conn._using_local_route is True
            assert conn._backend == "zenoh"
            assert conn._servers == [zenoh_url]
            assert conn._fallback_config is not None
            assert conn._fallback_config["servers"] == [ITEST_NATS_URL]

            await _wait_for_device_ids({"itest-portal-local-sensor"})

            result = await asyncio.to_thread(
                invoke,
                "device(itest-portal-local-sensor).function(get_reading)",
                {"unit": "celsius"},
            )
            assert result["success"] is True
            assert result["device_id"] == "itest-portal-local-sensor"
            assert "temperature" in result["result"]
        finally:
            await asyncio.to_thread(disconnect)
    finally:
        await factory.cleanup()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.portal
async def test_portal_local_route_small_fleet_shortcut(
    infrastructure,
    portal_bundle_path,
    portal_agent_env,
):
    """Small-fleet auto-expand works when the agent uses the portal local Zenoh route."""
    from fixtures.devices import DeviceFactory

    from device_connect_agent_tools import connect, disconnect
    from device_connect_agent_tools.tools import describe_fleet

    zenoh_url = ITEST_ZENOH_URL
    bundle = portal_bundle_path(local_routes=[zenoh_url])
    portal_agent_env(bundle, device_d2d=True)

    factory = DeviceFactory(messaging_url=zenoh_url)
    try:
        await factory.spawn_sensor("itest-portal-shortcut-sensor")
        await asyncio.sleep(SETTLE_TIME)

        await asyncio.to_thread(connect)
        try:
            deadline = time.monotonic() + DISCOVERY_TIMEOUT
            result = None
            while True:
                result = await asyncio.to_thread(describe_fleet)
                if (
                    result.get("total_devices", 0) >= 1
                    and any(
                        d.get("device_id") == "itest-portal-shortcut-sensor"
                        for d in result.get("devices", [])
                    )
                ):
                    break
                if time.monotonic() > deadline:
                    break
                await asyncio.sleep(0.25)

            assert result is not None
            assert "devices" in result, "Small fleet should auto-include device details"
            sensor = next(
                d for d in result["devices"]
                if d["device_id"] == "itest-portal-shortcut-sensor"
            )
            assert len(sensor.get("functions", [])) >= 1
            assert "parameters" in sensor["functions"][0]
        finally:
            await asyncio.to_thread(disconnect)
    finally:
        await factory.cleanup()


# ── Portal NATS route ──────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.portal
async def test_portal_prefers_portal_nats_when_local_disabled(
    infrastructure,
    portal_bundle_path,
    portal_agent_env,
):
    """DEVICE_CONNECT_PREFER_LOCAL=false uses the portal NATS/registry route."""
    from fixtures.devices import DeviceFactory

    from device_connect_agent_tools import connect, disconnect, invoke
    from device_connect_agent_tools.connection import get_connection

    nats_url = ITEST_NATS_URL
    bundle = portal_bundle_path(
        local_routes=[ITEST_ZENOH_URL],
        portal_nats_url=nats_url,
    )
    portal_agent_env(bundle, prefer_local="false")

    factory = DeviceFactory(messaging_url=nats_url)
    try:
        await factory.spawn_sensor("itest-portal-nats-sensor", initial_temp=19.5)
        await asyncio.sleep(SETTLE_TIME)

        await asyncio.to_thread(connect)
        try:
            conn = get_connection()
            assert conn._using_local_route is False
            assert conn._backend == "nats"
            assert conn._servers == [nats_url]
            assert conn._fallback_config is None

            await _wait_for_device_ids({"itest-portal-nats-sensor"})

            result = await asyncio.to_thread(
                invoke,
                "device(itest-portal-nats-sensor).function(get_reading)",
                {"unit": "celsius"},
            )
            assert result["success"] is True
        finally:
            await asyncio.to_thread(disconnect)
    finally:
        await factory.cleanup()


# ── Fallback to portal ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.portal
async def test_portal_falls_back_to_nats_when_local_unavailable(
    infrastructure,
    portal_bundle_path,
    portal_agent_env,
):
    """When the local Zenoh connect fails, the agent falls back to portal NATS/registry."""
    from fixtures.devices import DeviceFactory

    from device_connect_agent_tools import connect, disconnect, invoke
    from device_connect_agent_tools import connection as conn_mod
    from device_connect_agent_tools.connection import get_connection

    nats_url = ITEST_NATS_URL
    bundle = portal_bundle_path(
        local_routes=[BROKEN_LOCAL_ZENOH],
        portal_nats_url=nats_url,
    )
    portal_agent_env(bundle)

    factory = DeviceFactory(messaging_url=nats_url)
    try:
        await factory.spawn_sensor("itest-portal-fallback-sensor", initial_temp=18.0)
        await asyncio.sleep(SETTLE_TIME)

        original_connect = conn_mod.DeviceConnection._async_connect_current
        attempts = 0

        async def _connect_with_simulated_local_failure(self):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("local route unavailable")
            return await original_connect(self)

        with patch.object(
            conn_mod.DeviceConnection,
            "_async_connect_current",
            _connect_with_simulated_local_failure,
        ):
            await asyncio.to_thread(connect)

        try:
            conn = get_connection()
            assert attempts == 2
            assert conn._using_local_route is False
            assert conn._backend == "nats"
            assert conn._servers == [nats_url]
            assert conn._fallback_config is None

            await _wait_for_device_ids({"itest-portal-fallback-sensor"})

            result = await asyncio.to_thread(
                invoke,
                "device(itest-portal-fallback-sensor).function(get_reading)",
                {"unit": "celsius"},
            )
            assert result["success"] is True
            assert result["device_id"] == "itest-portal-fallback-sensor"
        finally:
            await asyncio.to_thread(disconnect)
    finally:
        await factory.cleanup()


# ── Registry-advertised local routes ───────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.portal
async def test_portal_discovers_local_zenoh_from_registry(
    infrastructure,
    portal_bundle_path,
    portal_agent_env,
    monkeypatch,
):
    """Portal bundle without ``local`` uses registry ``status.local_zenoh`` advertisements."""
    from fixtures.devices import DeviceFactory

    from device_connect_agent_tools import connect, disconnect, invoke
    from device_connect_agent_tools.connection import get_connection

    nats_url = ITEST_NATS_URL
    zenoh_url = ITEST_ZENOH_URL
    bundle = portal_bundle_path(local_routes=None, portal_nats_url=nats_url)
    portal_agent_env(bundle)

    nats_factory = DeviceFactory(messaging_url=nats_url)
    zenoh_factory = DeviceFactory(messaging_url=zenoh_url)
    try:
        await nats_factory.spawn_sensor(
            "itest-portal-registry-beacon",
            status={"local_zenoh": {"routes": [zenoh_url]}},
        )
        monkeypatch.setenv("DEVICE_CONNECT_DISCOVERY_MODE", "d2d")
        await zenoh_factory.spawn_sensor("itest-portal-registry-target", initial_temp=20.0)
        await asyncio.sleep(SETTLE_TIME)

        await asyncio.to_thread(connect)
        try:
            conn = get_connection()
            assert conn._using_local_route is True
            assert conn._backend == "zenoh"
            assert conn._servers == [zenoh_url]

            await _wait_for_device_ids({"itest-portal-registry-target"})

            result = await asyncio.to_thread(
                invoke,
                "device(itest-portal-registry-target).function(get_reading)",
                {"unit": "celsius"},
            )
            assert result["success"] is True
            assert result["device_id"] == "itest-portal-registry-target"
        finally:
            await asyncio.to_thread(disconnect)
    finally:
        await nats_factory.cleanup()
        await zenoh_factory.cleanup()
