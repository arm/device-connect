# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Zenoh PKI — certificate generation via openssl subprocess.

Generates:
  - CA certificate (self-signed, RSA 4096)
  - Zenoh router server certificate (with SANs)
  - Per-device client certificates (CN = device name, used for ACL identity)
"""

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from .. import config

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()

# Certificate validity (days)
CA_DAYS = 3650  # ~10 years
CERT_DAYS = 825  # ~2.25 years


class PkiError(Exception):
    """Raised when an openssl command fails."""


async def _run_openssl(*args: str, stdin_data: str | None = None) -> str:
    """Run an openssl command. Returns stdout. Raises PkiError on failure."""
    proc = await asyncio.create_subprocess_exec(
        "openssl", *args,
        stdin=asyncio.subprocess.PIPE if stdin_data else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(input=stdin_data.encode() if stdin_data else None),
        timeout=30,
    )
    if proc.returncode != 0:
        msg = stderr.decode().strip()
        raise PkiError(f"openssl {' '.join(args[:2])} failed: {msg}")
    return stdout.decode()


def _pki_dir() -> Path:
    """Return and ensure the PKI output directory exists."""
    d = config.SECURITY_INFRA_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def ca_exists() -> bool:
    """Check if the CA has been generated."""
    d = _pki_dir()
    return (d / "ca.pem").exists() and (d / "ca-key.pem").exists()


async def generate_ca() -> tuple[Path, Path]:
    """Generate a self-signed CA certificate and key.

    Returns (ca_cert_path, ca_key_path).
    """
    d = _pki_dir()
    ca_key = d / "ca-key.pem"
    ca_cert = d / "ca.pem"

    # Generate CA private key
    await _run_openssl(
        "genrsa", "-out", str(ca_key), "4096",
    )
    os.chmod(ca_key, 0o600)

    # Generate self-signed CA certificate
    await _run_openssl(
        "req", "-new", "-x509",
        "-key", str(ca_key),
        "-out", str(ca_cert),
        "-days", str(CA_DAYS),
        "-subj", "/CN=Device Connect CA/O=Device Connect",
    )

    logger.info("Generated CA: %s", ca_cert)
    return ca_cert, ca_key


def _validate_cn(value: str, label: str = "CN") -> str:
    """Validate a value is safe for use as an OpenSSL certificate CN.

    Prevents injection of extra subject fields via slashes or special chars.
    """
    import re
    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$', value):
        raise PkiError(
            f"Invalid {label}: must be alphanumeric with dots/hyphens/underscores, 1-64 chars"
        )
    return value


async def generate_server_cert(
    hostname: str,
    name: str = "zenoh",
) -> tuple[Path, Path]:
    """Generate a TLS server certificate for the Zenoh router.

    Includes SANs for the hostname, localhost, and 127.0.0.1.
    Returns (cert_path, key_path).
    """
    d = _pki_dir()
    ca_key = d / "ca-key.pem"
    ca_cert = d / "ca.pem"
    server_key = d / f"{name}-key.pem"
    server_cert = d / f"{name}-cert.pem"
    csr_path = d / f"{name}.csr"

    # Generate server key
    await _run_openssl("genrsa", "-out", str(server_key), "2048")
    os.chmod(server_key, 0o600)

    # Create SAN config
    san_entries = [
        f"DNS:{hostname}",
        "DNS:localhost",
        "DNS:zenoh",
        "IP:127.0.0.1",
    ]
    san_ext = (
        "[req]\n"
        "distinguished_name = req_dn\n"
        "req_extensions = v3_req\n"
        "[req_dn]\n"
        "[v3_req]\n"
        "subjectAltName = " + ",".join(san_entries) + "\n"
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".cnf", delete=False) as f:
        f.write(san_ext)
        san_cnf = f.name

    try:
        # Generate CSR
        _validate_cn(hostname, "hostname")
        await _run_openssl(
            "req", "-new",
            "-key", str(server_key),
            "-out", str(csr_path),
            "-subj", f"/CN={hostname}",
            "-config", san_cnf,
        )

        # Sign with CA
        ext_content = "subjectAltName = " + ",".join(san_entries)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ext", delete=False) as f:
            f.write(ext_content)
            ext_file = f.name

        try:
            await _run_openssl(
                "x509", "-req",
                "-in", str(csr_path),
                "-CA", str(ca_cert),
                "-CAkey", str(ca_key),
                "-CAcreateserial",
                "-out", str(server_cert),
                "-days", str(CERT_DAYS),
                "-extfile", ext_file,
            )
        finally:
            os.unlink(ext_file)
    finally:
        os.unlink(san_cnf)
        if csr_path.exists():
            csr_path.unlink()

    logger.info("Generated server cert: %s (SANs: %s)", server_cert, ", ".join(san_entries))
    return server_cert, server_key


async def generate_client_cert(
    name: str,
    common_name: str | None = None,
) -> tuple[Path, Path]:
    """Generate a client certificate signed by the CA.

    The CN is used by Zenoh ACL for identity matching.
    Returns (cert_path, key_path).
    """
    d = _pki_dir()
    ca_key = d / "ca-key.pem"
    ca_cert = d / "ca.pem"
    client_key = d / f"{name}-key.pem"
    client_cert = d / f"{name}-cert.pem"
    csr_path = d / f"{name}.csr"

    cn = common_name or name
    _validate_cn(cn, "certificate CN")

    # Generate client key
    await _run_openssl("genrsa", "-out", str(client_key), "2048")
    os.chmod(client_key, 0o600)

    # Generate CSR
    await _run_openssl(
        "req", "-new",
        "-key", str(client_key),
        "-out", str(csr_path),
        "-subj", f"/CN={cn}",
    )

    # Sign with CA
    await _run_openssl(
        "x509", "-req",
        "-in", str(csr_path),
        "-CA", str(ca_cert),
        "-CAkey", str(ca_key),
        "-CAcreateserial",
        "-out", str(client_cert),
        "-days", str(CERT_DAYS),
    )

    if csr_path.exists():
        csr_path.unlink()

    logger.info("Generated client cert: %s (CN=%s)", client_cert, cn)
    return client_cert, client_key


async def get_ca_fingerprint() -> str:
    """Return the SHA256 fingerprint of the CA certificate."""
    d = _pki_dir()
    ca_cert = d / "ca.pem"
    output = await _run_openssl(
        "x509", "-in", str(ca_cert), "-noout", "-fingerprint", "-sha256",
    )
    # Output: "sha256 Fingerprint=AA:BB:CC:..."
    return output.strip().split("=", 1)[-1]
