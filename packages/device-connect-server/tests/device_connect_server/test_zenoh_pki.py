# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Zenoh PKI: the router server cert must carry an IP SAN for IP hosts.

A device connecting by IP (``tls/203.0.113.5:7447``) verifies the router
cert's name against the endpoint. An IP literal placed in a DNS SAN does
not match, so IP-based onboarding fails unless the cert carries an IP SAN.
"""

import subprocess

from device_connect_server.portal.services import zenoh_pki


def _san(cert_path) -> str:
    out = subprocess.run(
        ["openssl", "x509", "-in", str(cert_path), "-noout", "-ext", "subjectAltName"],
        capture_output=True, text=True,
    )
    return out.stdout


async def test_ip_host_gets_ip_san(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "device_connect_server.portal.config.SECURITY_INFRA_DIR", tmp_path)
    await zenoh_pki.generate_ca()
    cert, _ = await zenoh_pki.generate_server_cert("203.0.113.5")
    san = _san(cert)
    assert "IP Address:203.0.113.5" in san
    # localhost loopback IP is always present; the host is NOT a DNS entry.
    assert "DNS:203.0.113.5" not in san


async def test_dns_host_gets_dns_san(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "device_connect_server.portal.config.SECURITY_INFRA_DIR", tmp_path)
    await zenoh_pki.generate_ca()
    cert, _ = await zenoh_pki.generate_server_cert("broker.example.com", name="dnscert")
    san = _san(cert)
    assert "DNS:broker.example.com" in san
