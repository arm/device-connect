# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the @rpc caller-identity hook (get_rpc_source_device).

Security hardening: device drivers need to know the authenticated source
device of the in-flight RPC so they can perform per-call authorization. The
@rpc wrapper exposes it via a contextvar for the duration of the handler.
"""

import pytest

from device_connect_edge.drivers import DeviceDriver, rpc, get_rpc_source_device
from device_connect_edge.types import DeviceIdentity, DeviceStatus


class _CallerAwareDriver(DeviceDriver):
    device_type = "caller_aware"

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(device_type="caller_aware", manufacturer="Test", model="X")

    @property
    def status(self) -> DeviceStatus:
        return DeviceStatus()

    @rpc()
    async def whoami(self) -> dict:
        """Return the authenticated caller as seen inside the handler."""
        return {"caller": get_rpc_source_device()}

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass


@pytest.mark.asyncio
async def test_source_device_visible_inside_handler():
    d = _CallerAwareDriver()
    res = await d.whoami(source_device="controller-1")
    assert res["caller"] == "controller-1"


@pytest.mark.asyncio
async def test_source_device_none_when_absent():
    d = _CallerAwareDriver()
    res = await d.whoami()
    assert res["caller"] is None


@pytest.mark.asyncio
async def test_source_device_reset_after_call():
    d = _CallerAwareDriver()
    await d.whoami(source_device="controller-1")
    # Outside the handler the contextvar must be back to its default.
    assert get_rpc_source_device() is None


@pytest.mark.asyncio
async def test_source_device_not_leaked_into_handler_kwargs():
    # source_device must be consumed by the wrapper, not passed to the
    # handler body (which does not declare it).
    d = _CallerAwareDriver()
    # whoami takes no params; passing source_device must not raise.
    res = await d.whoami(source_device="x")
    assert res["caller"] == "x"
