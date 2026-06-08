# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Registry tenant extraction must work on every backend's subject form.

Regression: ``_extract_tenant`` split only on ``.`` (NATS dotted subjects),
so Zenoh's slash-delimited key expressions (``device-connect/acme/registry``)
fell through to ``default`` -- mis-attributing every Zenoh device to the
wrong tenant in the registry/etcd records.
"""

import pytest

from device_connect_server.registry.service.main import _extract_tenant


@pytest.mark.parametrize(
    "subject,expected",
    [
        ("device-connect.acme.registry", "acme"),        # NATS dotted
        ("device-connect/acme/registry", "acme"),        # Zenoh slash
        ("device-connect/acme/cam-1/heartbeat", "acme"),  # deeper Zenoh key
        ("device-connect.acme.cam-1.heartbeat", "acme"),  # deeper NATS subject
        ("device-connect/tenant-1/discovery", "tenant-1"),
        ("malformed", "default"),                         # too short -> default
        ("device-connect/", "default"),
    ],
)
def test_extract_tenant_handles_both_delimiters(subject, expected):
    assert _extract_tenant(subject) == expected
