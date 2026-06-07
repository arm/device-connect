# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Zenoh backend — mTLS certificates + ACL plugin for tenant isolation."""

import json
import logging
import os
from pathlib import Path
from typing import Any

from .. import config
from . import zenoh_acl, zenoh_admin, zenoh_pki, zenoh_rpc
from .backend import MessagingBackendService

logger = logging.getLogger(__name__)


class ZenohBackend(MessagingBackendService):
    """MessagingBackendService implementation for Zenoh (mTLS + ACL)."""

    def backend_name(self) -> str:
        return "zenoh"

    def is_bootstrapped(self) -> bool:
        """Bootstrapped if CA exists and privileged creds are generated."""
        if not zenoh_pki.ca_exists():
            return False
        registry_creds = config.CREDS_DIR / "registry.creds.json"
        if not registry_creds.exists():
            return False
        # Verify it's a Zenoh credential (not leftover NATS)
        try:
            data = json.loads(registry_creds.read_text())
            return data.get("auth_type") == "mtls"
        except (json.JSONDecodeError, OSError):
            return False

    async def bootstrap(self, host: str, port: str, **kwargs) -> dict:
        from .backend import _write_backend_choice

        pki_dir = config.SECURITY_INFRA_DIR
        pki_dir.mkdir(parents=True, exist_ok=True)
        config.CREDS_DIR.mkdir(parents=True, exist_ok=True)

        # 1. Generate CA -- but ONLY if one does not already exist. Re-running
        #    setup must never rotate the CA: every issued device credential is
        #    signed by it, so a new CA would silently invalidate the whole
        #    fleet. Keeping it makes bootstrap idempotent, which is what lets an
        #    operator safely re-run setup to refresh the server cert (step 2).
        if zenoh_pki.ca_exists():
            ca_cert = pki_dir / "ca.pem"
            ca_key = pki_dir / "ca-key.pem"
            logger.info("CA already present; keeping it (refreshing server cert only)")
        else:
            ca_cert, ca_key = await zenoh_pki.generate_ca()

        # 2. (Re)generate the router server cert. Always refreshed so cert fixes
        #    -- e.g. emitting an IP SAN for an IP-literal host -- take effect on
        #    a plain setup re-run, signed by the existing (unchanged) CA.
        await zenoh_pki.generate_server_cert(host)

        # 3. Generate privileged client certs
        for name in ("registry", "facilitator"):
            cert_path, key_path = await zenoh_pki.generate_client_cert(name)
            # Write credential JSON
            self._write_credential(
                name=name,
                tenant="default",
                host=host,
                port=port,
                cert_path=cert_path,
                key_path=key_path,
                ca_cert=ca_cert,
            )

        # 4. Generate Zenoh router config with ACL
        zenoh_acl.generate_config(host, port)
        # Record the baseline config hash: the router starts with exactly this
        # config, so the first device provision (which does not change it)
        # correctly skips the reload instead of forcing a spurious restart.
        zenoh_admin.mark_reloaded()

        # 5. Persist backend choice
        _write_backend_choice("zenoh", host, port)

        fingerprint = await zenoh_pki.get_ca_fingerprint()
        return {
            "backend": "Zenoh",
            "host": host,
            "port": port,
            "auth_method": "mTLS + ACL",
            "ca_fingerprint": fingerprint,
            "privileged_creds": ["registry", "facilitator"],
        }

    async def create_tenant(
        self, tenant: str, num_devices: int, host: str, port: str,
    ) -> list[str]:
        ca_cert = config.SECURITY_INFRA_DIR / "ca.pem"
        device_names = []

        for i in range(1, num_devices + 1):
            device_name = f"{tenant}-device-{i:03d}"
            # CN = tenant (device id goes in the cert OU); see add_tenant_rule.
            cert_path, key_path = await zenoh_pki.generate_client_cert(
                device_name, common_name=tenant)
            self._write_credential(
                name=device_name,
                tenant=tenant,
                host=host,
                port=port,
                cert_path=cert_path,
                key_path=key_path,
                ca_cert=ca_cert,
            )
            device_names.append(device_name)

        # One static ACL rule per tenant (CN=tenant). This is the only ACL
        # write in the device lifecycle and the only step that needs a reload.
        zenoh_acl.add_tenant_rule(tenant)

        return device_names

    async def add_device(
        self, tenant: str, device_name: str, host: str, port: str,
    ) -> Path:
        ca_cert = config.SECURITY_INFRA_DIR / "ca.pem"
        # CN = tenant (shared by all the tenant's devices); device id -> OU.
        cert_path, key_path = await zenoh_pki.generate_client_cert(
            device_name, common_name=tenant)

        cred_path = self._write_credential(
            name=device_name,
            tenant=tenant,
            host=host,
            port=port,
            cert_path=cert_path,
            key_path=key_path,
            ca_cert=ca_cert,
        )

        # Ensure the tenant's static ACL rule exists (a no-op once the tenant
        # was created). Adding a device does NOT change the ACL, so
        # reload_broker() below will detect no change and skip the restart.
        zenoh_acl.add_tenant_rule(tenant)

        return cred_path

    async def remove_device(self, tenant: str, device_name: str) -> None:
        """Soft-revoke a device: delete its credential key material.

        Under the per-tenant-CN model every device shares ``CN=tenant``, so a
        single device cannot be denied at the ACL level -- the ACL is left
        untouched (and reload_broker() will skip the restart). This removes
        the device from the portal/registry and deletes its cert/key so the
        credential cannot be re-downloaded, but a copy already deployed
        remains cryptographically valid until the certificate expires.

        For an immediate certificate-level cutoff, hard-revoke the whole
        tenant (zenoh_acl.remove_tenant_rule) or use short-lived certs.
        See docs/zenoh-per-tenant-cn.md.
        """
        zenoh_pki.delete_client_cert(device_name)

    @staticmethod
    def per_device_revocation_note() -> str:
        """Caller-facing caveat: per-device revocation is soft on Zenoh.

        Surfaced by the revoke endpoints so operators are not misled into
        thinking a revoked device is cryptographically locked out.
        """
        return (
            "Soft revocation: the device's credential was deleted, but under "
            "the per-tenant-CN model its certificate (shared CN=tenant) is "
            "NOT denied at the broker and stays valid until it expires. For an "
            "immediate cutoff, hard-revoke the whole tenant or use short-lived "
            "certificates."
        )

    async def reload_broker(self) -> dict:
        return await zenoh_admin.reload_zenoh()

    async def rpc_invoke(
        self, tenant: str, device_id: str, function: str,
        params: dict, timeout: float = 5.0,
    ) -> dict:
        return await zenoh_rpc.invoke(tenant, device_id, function, params, timeout)

    async def rpc_connect(self) -> Any:
        return await zenoh_rpc.connect()

    async def subscribe_events(
        self, client: Any, subject: str, callback,
    ) -> Any:
        """Subscribe to events using ZenohAdapter."""
        return await client.subscribe(subject, callback)

    async def unsubscribe_events(self, client: Any, subscription: Any) -> None:
        """Unsubscribe and close the ZenohAdapter."""
        # ZenohAdapter subscriptions are managed internally;
        # closing the adapter cleans them up
        if client:
            await client.close()

    async def run_verification(self) -> list[dict]:
        """Run Zenoh-specific isolation verification."""
        results = []

        # Test 1: CA exists
        if zenoh_pki.ca_exists():
            try:
                fp = await zenoh_pki.get_ca_fingerprint()
                results.append({
                    "name": "CA Certificate",
                    "status": "pass",
                    "detail": f"CA exists (SHA256: {fp[:20]}...)",
                })
            except Exception as e:
                results.append({
                    "name": "CA Certificate",
                    "status": "fail",
                    "detail": f"CA exists but fingerprint failed: {e}",
                })
        else:
            results.append({
                "name": "CA Certificate",
                "status": "fail",
                "detail": "CA certificate not found",
            })
            return results

        # Test 2: Privileged certs exist
        for name in ("registry", "facilitator"):
            cred_path = config.CREDS_DIR / f"{name}.creds.json"
            if cred_path.exists():
                results.append({
                    "name": f"Privileged Credential: {name}",
                    "status": "pass",
                    "detail": f"{name}.creds.json exists with mTLS auth",
                })
            else:
                results.append({
                    "name": f"Privileged Credential: {name}",
                    "status": "fail",
                    "detail": f"{name}.creds.json not found",
                })

        # Test 3: Zenoh config with ACL
        # Zenoh 1.x exposes access control as a top-level config field, not a
        # loadable plugin (see zenoh_acl.generate_config); reading it under
        # "plugins" always reported the ACL as disabled.
        cfg = zenoh_acl.load_config()
        acl = cfg.get("access_control", {})
        if acl.get("enabled"):
            results.append({
                "name": "Zenoh ACL Plugin",
                "status": "pass",
                "detail": f"Enabled with default_permission={acl.get('default_permission', 'unknown')}",
            })
        else:
            results.append({
                "name": "Zenoh ACL Plugin",
                "status": "fail",
                "detail": "ACL plugin not enabled in Zenoh config",
            })

        # Test 4: Per-tenant rules
        tenant_rules = zenoh_acl.list_tenant_rules()
        if not tenant_rules:
            results.append({
                "name": "Tenant ACL Rules",
                "status": "skip",
                "detail": "No tenant rules configured yet",
            })
        else:
            for tenant, cns in tenant_rules.items():
                results.append({
                    "name": f"Tenant '{tenant}' ACL",
                    "status": "pass",
                    "detail": f"Key expr: device-connect/{tenant}/**, {len(cns)} device(s)",
                })

        # Test 5: Cross-tenant isolation (structural)
        tenant_names = list(tenant_rules.keys())
        if len(tenant_names) >= 2:
            for i, t1 in enumerate(tenant_names):
                for t2 in tenant_names[i + 1:]:
                    cns1 = set(tenant_rules[t1])
                    cns2 = set(tenant_rules[t2])
                    overlap = cns1 & cns2
                    if overlap:
                        results.append({
                            "name": f"Cross-tenant Isolation: {t1} <-> {t2}",
                            "status": "fail",
                            "detail": f"Overlapping CNs: {overlap}",
                        })
                    else:
                        results.append({
                            "name": f"Cross-tenant Isolation: {t1} <-> {t2}",
                            "status": "pass",
                            "detail": f"CN groups are disjoint, key exprs: device-connect/{t1}/** vs device-connect/{t2}/**",
                        })
        elif len(tenant_names) == 1:
            results.append({
                "name": "Cross-tenant Isolation",
                "status": "skip",
                "detail": "Need at least 2 tenants to test cross-tenant isolation",
            })

        return results

    def broker_display_info(self) -> dict:
        return {
            "backend": "Zenoh",
            "host": config.ZENOH_HOST,
            "port": config.ZENOH_PORT,
            "auth_method": "mTLS + ACL",
            "container": config.ZENOH_CONTAINER,
        }

    def default_host(self) -> str:
        return config.ZENOH_HOST

    def default_port(self) -> str:
        return config.ZENOH_PORT

    @staticmethod
    def _write_credential(
        name: str,
        tenant: str,
        host: str,
        port: str,
        cert_path: Path,
        key_path: Path,
        ca_cert: Path,
    ) -> Path:
        """Write a Zenoh credential JSON file."""
        creds_data = {
            "device_id": name,
            "auth_type": "mtls",
            "tenant": tenant,
            "zenoh": {
                "urls": [f"zenoh+tls://{host}:{port}"],
                "tls": {
                    "ca_file": str(ca_cert),
                    "cert_file": str(cert_path),
                    "key_file": str(key_path),
                },
            },
        }

        output_path = config.CREDS_DIR / f"{name}.creds.json"
        fd = os.open(str(output_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(creds_data, f, indent=2)

        logger.info("Created Zenoh credentials: %s (tenant=%s)", output_path, tenant)
        return output_path
