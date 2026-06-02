# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for portal admin-password resolution.

``portal.config`` resolves ``ADMIN_PASS`` at import time and exposes
``ADMIN_PASS_GENERATED`` so the startup path knows whether to log the
generated password. The trap these tests pin: an *empty* ``ADMIN_PASS``
must be treated exactly like an unset one -- generate a random password
AND flag it as generated so it gets logged. The earlier implementation
keyed ``ADMIN_PASS_GENERATED`` off ``"ADMIN_PASS" not in os.environ``,
so ``ADMIN_PASS=""`` silently generated a password but never logged it,
locking the operator out.
"""

from __future__ import annotations

import importlib

import pytest


def _reload_config(monkeypatch, value):
    """Reload portal.config with ADMIN_PASS set to ``value`` (None = unset)."""
    if value is None:
        monkeypatch.delenv("ADMIN_PASS", raising=False)
    else:
        monkeypatch.setenv("ADMIN_PASS", value)
    from device_connect_server.portal import config

    return importlib.reload(config)


def test_unset_admin_pass_is_generated(monkeypatch):
    config = _reload_config(monkeypatch, None)
    assert config.ADMIN_PASS_GENERATED is True
    assert config.ADMIN_PASS  # a password was generated


def test_empty_admin_pass_is_generated(monkeypatch):
    config = _reload_config(monkeypatch, "")
    # Empty string must behave like unset: generated AND flagged so it logs.
    assert config.ADMIN_PASS_GENERATED is True
    assert config.ADMIN_PASS


def test_explicit_admin_pass_is_not_generated(monkeypatch):
    config = _reload_config(monkeypatch, "s3cret-pass")
    assert config.ADMIN_PASS_GENERATED is False
    assert config.ADMIN_PASS == "s3cret-pass"


@pytest.fixture(scope="module", autouse=True)
def _restore_config():
    """Reload config from the ambient environment once the module finishes so
    the import-time ADMIN_PASS state doesn't leak into other test modules. This
    is module-scoped so it runs after every per-test ``monkeypatch`` has already
    restored ``os.environ``."""
    yield
    from device_connect_server.portal import config

    importlib.reload(config)
