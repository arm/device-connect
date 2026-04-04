"""Wraps the nsc CLI for NATS JWT credential management."""

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path

from .. import config

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()


def _nsc_env() -> dict[str, str]:
    """Environment variables for nsc subprocess calls."""
    nsc_home = str(config.NSC_HOME)
    return {
        "NSC_HOME": nsc_home,
        "NKEYS_PATH": str(config.NSC_HOME / "nkeys"),
        "XDG_DATA_HOME": str(config.NSC_HOME / "data"),
        "XDG_CONFIG_HOME": str(config.NSC_HOME / "config"),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/root"),
    }


class NscError(Exception):
    """Raised when an nsc command fails."""


async def _run_nsc(*args: str) -> str:
    """Run an nsc command and return stdout. Raises NscError on failure."""
    proc = await asyncio.create_subprocess_exec(
        "nsc", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_nsc_env(),
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    if proc.returncode != 0:
        msg = stderr.decode().strip() or stdout.decode().strip()
        raise NscError(f"nsc {' '.join(args)} failed: {msg}")
    return stdout.decode()


def is_bootstrapped() -> bool:
    """Check if the NATS JWT infrastructure has been set up."""
    return config.NSC_HOME.exists() and (config.NSC_HOME / "nkeys").exists()


async def bootstrap(nats_host: str, nats_port: str = "4222") -> dict:
    """One-time setup: create operator, account, privileged credentials.

    Returns dict with summary info.
    """
    async with _lock:
        import shutil
        # Clean previous state
        if config.NSC_HOME.exists():
            shutil.rmtree(config.NSC_HOME)
        config.NSC_HOME.mkdir(parents=True, exist_ok=True)

        # Create operator
        await _run_nsc("add", "operator", config.NSC_OPERATOR, "--sys")

        # Create account
        await _run_nsc("add", "account", config.NSC_ACCOUNT)

        # Add signing key and configure JetStream limits
        await _run_nsc("edit", "account", config.NSC_ACCOUNT, "--sk", "generate")
        await _run_nsc(
            "edit", "account", config.NSC_ACCOUNT,
            "--js-mem-storage", "-1",
            "--js-disk-storage", "-1",
            "--js-streams", "-1",
            "--js-consumer", "-1",
        )

        # Generate NATS config
        await _regenerate_config_unlocked()

        # Generate privileged credentials for registry and facilitator
        await _create_user_unlocked("registry", tenant=None, privileged=True,
                                    nats_host=nats_host, nats_port=nats_port)
        await _create_user_unlocked("facilitator", tenant=None, privileged=True,
                                    nats_host=nats_host, nats_port=nats_port)

        # Regenerate config again (includes new user JWTs)
        await _regenerate_config_unlocked()

        return {
            "operator": config.NSC_OPERATOR,
            "account": config.NSC_ACCOUNT,
            "nats_host": nats_host,
            "nats_port": nats_port,
            "privileged_creds": ["registry", "facilitator"],
        }


async def _regenerate_config_unlocked():
    """Regenerate NATS server config from nsc state (caller holds lock)."""
    output_conf = config.SECURITY_INFRA_DIR / "nats-jwt-generated.conf"
    await _run_nsc("generate", "config", "--mem-resolver",
                   "--config-file", str(output_conf))
    # Append listen directives
    with open(output_conf, "a") as f:
        f.write("\n# Device Connect additions\nlisten: 0.0.0.0:4222\nhttp_port: 8222\n")


async def regenerate_config():
    """Regenerate NATS server config (public, acquires lock)."""
    async with _lock:
        await _regenerate_config_unlocked()


async def _create_user_unlocked(
    name: str,
    tenant: str | None,
    privileged: bool,
    nats_host: str,
    nats_port: str,
) -> Path:
    """Create an nsc user and write credential JSON. Caller holds lock.

    Returns path to the written credential file.
    """
    config.CREDS_DIR.mkdir(parents=True, exist_ok=True)

    # Add user (ignore error if already exists)
    try:
        await _run_nsc("add", "user", name, "--account", config.NSC_ACCOUNT)
    except NscError:
        pass  # user already exists

    # Set permissions
    if privileged or not tenant:
        pub_sub = "device-connect.>"
    else:
        pub_sub = f"device-connect.{tenant}.>"

    try:
        await _run_nsc(
            "edit", "user", name,
            "--account", config.NSC_ACCOUNT,
            "--allow-pub", pub_sub,
            "--allow-sub", pub_sub,
            "--allow-pub", "_INBOX.>",
            "--allow-sub", "_INBOX.>",
        )
    except NscError:
        pass  # permissions may already be set

    # Export credentials
    raw_creds = await _run_nsc(
        "generate", "creds",
        "--account", config.NSC_ACCOUNT,
        "--name", name,
    )

    # Parse JWT and NKey seed from .creds format
    jwt_match = re.search(
        r'-----BEGIN NATS USER JWT-----\n(.+?)\n------END NATS USER JWT------',
        raw_creds, re.DOTALL,
    )
    seed_match = re.search(
        r'-----BEGIN USER NKEY SEED-----\n(.+?)\n------END USER NKEY SEED------',
        raw_creds, re.DOTALL,
    )

    jwt_token = jwt_match.group(1).strip() if jwt_match else ""
    nkey_seed = seed_match.group(1).strip() if seed_match else ""

    tenant_value = tenant if tenant else "default"
    creds_data = {
        "device_id": name,
        "auth_type": "jwt",
        "tenant": tenant_value,
        "nats": {
            "urls": [f"nats://{nats_host}:{nats_port}"],
            "jwt": jwt_token,
            "nkey_seed": nkey_seed,
        },
    }

    output_path = config.CREDS_DIR / f"{name}.creds.json"
    with open(output_path, "w") as f:
        json.dump(creds_data, f, indent=2)

    logger.info("Created credentials: %s (tenant=%s)", output_path, tenant_value)
    return output_path


async def create_tenant(
    tenant: str,
    num_devices: int,
    nats_host: str,
    nats_port: str = "4222",
) -> list[str]:
    """Create a tenant with N device credentials. Returns list of device names."""
    async with _lock:
        device_names = []
        for i in range(1, num_devices + 1):
            device_name = f"{tenant}-device-{i:03d}"
            await _create_user_unlocked(
                device_name, tenant=tenant, privileged=False,
                nats_host=nats_host, nats_port=nats_port,
            )
            device_names.append(device_name)
        await _regenerate_config_unlocked()
        return device_names


async def add_device(
    tenant: str,
    device_name: str,
    nats_host: str,
    nats_port: str = "4222",
) -> Path:
    """Add a single device credential to a tenant. Returns credential path."""
    async with _lock:
        path = await _create_user_unlocked(
            device_name, tenant=tenant, privileged=False,
            nats_host=nats_host, nats_port=nats_port,
        )
        await _regenerate_config_unlocked()
        return path
