# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""The reload gate: the router only restarts when the config actually changed.

This is what keeps device provisioning/revocation reload-free under
per-tenant-CN -- those operations don't change zenoh-config.json5, so the
gate skips the (fleet-wide, disruptive) restart. Only a real change (tenant
create/delete) bumps the signal file the reloader sidecar watches.
"""

import pytest

from device_connect_server.portal.services import zenoh_admin


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "device_connect_server.portal.config.SECURITY_INFRA_DIR", tmp_path)
    signal = tmp_path / "reload" / "request"
    monkeypatch.setattr(zenoh_admin, "RELOAD_SIGNAL_PATH", str(signal))
    (tmp_path / "zenoh-config.json5").write_text('{"access_control": {"rules": []}}')
    return tmp_path, signal


async def test_unchanged_config_skips_the_restart(env):
    tmp_path, signal = env
    zenoh_admin.mark_reloaded()                 # baseline = current config
    res = await zenoh_admin.reload_zenoh()      # nothing changed
    assert res["reloaded"] is False
    assert not signal.exists()                  # no signal -> sidecar won't restart


async def test_changed_config_signals_a_restart(env):
    tmp_path, signal = env
    zenoh_admin.mark_reloaded()
    # Simulate a tenant create/delete editing the ACL.
    (tmp_path / "zenoh-config.json5").write_text('{"access_control": {"rules": ["x"]}}')
    res = await zenoh_admin.reload_zenoh()
    assert res["reloaded"] is True
    assert signal.exists() and signal.read_text().strip()


async def test_no_baseline_signals_a_restart(env):
    _, signal = env
    # No mark_reloaded() baseline recorded -> treat as changed (first reload).
    res = await zenoh_admin.reload_zenoh()
    assert res["reloaded"] is True
    assert signal.exists()


async def test_second_unchanged_reload_after_a_change_skips(env):
    tmp_path, signal = env
    zenoh_admin.mark_reloaded()
    (tmp_path / "zenoh-config.json5").write_text('{"access_control": {"rules": ["x"]}}')
    first = await zenoh_admin.reload_zenoh()    # changed -> signals + records hash
    assert first["reloaded"] is True
    second = await zenoh_admin.reload_zenoh()   # unchanged since -> skip
    assert second["reloaded"] is False
