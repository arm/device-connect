"""MQTT backend \u2014 Mosquitto password file + ACL for tenant isolation."""

import json
import logging
import secrets
from pathlib import Path
from typing import Any

from .. import config
from . import mqtt_acl, mqtt_admin, mqtt_rpc
from .backend import MessagingBackendService

logger = logging.getLogger(__name__)


class MqttBackend(MessagingBackendService):
    """MessagingBackendService implementation for MQTT (Mosquitto password + ACL)."""

    def backend_name(self) -> str:
        return "mqtt"

    def is_bootstrapped(self) -> bool:
        """Bootstrapped if Mosquitto config exists and privileged creds are generated."""
        if not mqtt_acl.is_bootstrapped():
            return False
        registry_creds = config.CREDS_DIR / "registry.creds.json"
        if not registry_creds.exists():
            return False
        try:
            data = json.loads(registry_creds.read_text())
            return data.get("auth_type") == "password"
        except (json.JSONDecodeError, OSError):
            return False

    async def bootstrap(self, host: str, port: str, **kwargs) -> dict:
        from .backend import _write_backend_choice

        config.SECURITY_INFRA_DIR.mkdir(parents=True, exist_ok=True)
        config.CREDS_DIR.mkdir(parents=True, exist_ok=True)

        # 1. Generate Mosquitto config
        mqtt_acl.generate_config(host, port)

        # 2. Create privileged users with generated passwords
        privileged_names = ["registry", "facilitator"]
        for name in privileged_names:
            password = secrets.token_urlsafe(24)
            await mqtt_acl.add_user(name, password)
            self._write_credential(
                name=name, tenant="default",
                host=host, port=port,
                username=name, password=password,
            )

        # 3. Generate initial ACL (privileged users get full access)
        mqtt_acl.generate_initial_acl(privileged_names)

        # 4. Persist backend choice
        _write_backend_choice("mqtt", host, port)

        return {
            "backend": "MQTT (Mosquitto)",
            "host": host,
            "port": port,
            "auth_method": "Password + ACL",
            "privileged_creds": privileged_names,
        }

    async def create_tenant(
        self, tenant: str, num_devices: int, host: str, port: str,
    ) -> list[str]:
        device_names = []
        device_usernames = []

        for i in range(1, num_devices + 1):
            device_name = f"{tenant}-device-{i:03d}"
            password = secrets.token_urlsafe(24)
            await mqtt_acl.add_user(device_name, password)
            self._write_credential(
                name=device_name, tenant=tenant,
                host=host, port=port,
                username=device_name, password=password,
            )
            device_names.append(device_name)
            device_usernames.append(device_name)

        # Update ACL file
        mqtt_acl.add_tenant_acl(tenant, device_usernames)

        return device_names

    async def add_device(
        self, tenant: str, device_name: str, host: str, port: str,
    ) -> Path:
        password = secrets.token_urlsafe(24)
        await mqtt_acl.add_user(device_name, password)
        cred_path = self._write_credential(
            name=device_name, tenant=tenant,
            host=host, port=port,
            username=device_name, password=password,
        )
        mqtt_acl.add_devices_to_tenant(tenant, [device_name])
        return cred_path

    async def reload_broker(self) -> dict:
        return await mqtt_admin.reload_mosquitto()

    async def rpc_invoke(
        self, tenant: str, device_id: str, function: str,
        params: dict, timeout: float = 5.0,
    ) -> dict:
        return await mqtt_rpc.invoke(tenant, device_id, function, params, timeout)

    async def rpc_connect(self) -> Any:
        return await mqtt_rpc.connect()

    async def subscribe_events(
        self, client: Any, subject: str, callback,
    ) -> Any:
        """Subscribe to events using MQTTAdapter."""
        return await client.subscribe(subject, callback)

    async def unsubscribe_events(self, client: Any, subscription: Any) -> None:
        """Unsubscribe and close the MQTTAdapter."""
        if client:
            await client.close()

    async def run_verification(self) -> list[dict]:
        """Run MQTT-specific isolation verification."""
        results = []

        # Test 1: Mosquitto config exists
        if mqtt_acl.is_bootstrapped():
            results.append({
                "name": "Mosquitto Config",
                "status": "pass",
                "detail": "mosquitto.conf, password file, and ACL file exist",
            })
        else:
            results.append({
                "name": "Mosquitto Config",
                "status": "fail",
                "detail": "Mosquitto configuration files not found",
            })
            return results

        # Test 2: Privileged credentials exist
        for name in ("registry", "facilitator"):
            cred_path = config.CREDS_DIR / f"{name}.creds.json"
            if cred_path.exists():
                results.append({
                    "name": f"Privileged Credential: {name}",
                    "status": "pass",
                    "detail": f"{name}.creds.json exists with password auth",
                })
            else:
                results.append({
                    "name": f"Privileged Credential: {name}",
                    "status": "fail",
                    "detail": f"{name}.creds.json not found",
                })

        # Test 3: ACL file has access rules
        acl_text = mqtt_acl.load_acl_text()
        if "device-connect/#" in acl_text:
            results.append({
                "name": "Mosquitto ACL",
                "status": "pass",
                "detail": "ACL file contains access rules",
            })
        else:
            results.append({
                "name": "Mosquitto ACL",
                "status": "fail",
                "detail": "ACL file missing access rules",
            })

        # Test 4: Per-tenant rules
        tenant_rules = mqtt_acl.list_tenant_rules()
        if not tenant_rules:
            results.append({
                "name": "Tenant ACL Rules",
                "status": "skip",
                "detail": "No tenant rules configured yet",
            })
        else:
            for tenant, usernames in tenant_rules.items():
                results.append({
                    "name": f"Tenant '{tenant}' ACL",
                    "status": "pass",
                    "detail": f"Topic: device-connect/{tenant}/#, {len(usernames)} device(s)",
                })

        # Test 5: Cross-tenant isolation (structural)
        tenant_names = list(tenant_rules.keys())
        if len(tenant_names) >= 2:
            for i, t1 in enumerate(tenant_names):
                for t2 in tenant_names[i + 1:]:
                    users1 = set(tenant_rules[t1])
                    users2 = set(tenant_rules[t2])
                    overlap = users1 & users2
                    if overlap:
                        results.append({
                            "name": f"Cross-tenant Isolation: {t1} <-> {t2}",
                            "status": "fail",
                            "detail": f"Overlapping users: {overlap}",
                        })
                    else:
                        results.append({
                            "name": f"Cross-tenant Isolation: {t1} <-> {t2}",
                            "status": "pass",
                            "detail": (
                                f"User groups are disjoint, topics: "
                                f"device-connect/{t1}/# vs device-connect/{t2}/#"
                            ),
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
            "backend": "MQTT",
            "host": config.MQTT_HOST,
            "port": config.MQTT_PORT,
            "auth_method": "Password + ACL",
            "container": config.MQTT_CONTAINER,
        }

    def default_host(self) -> str:
        return config.MQTT_HOST

    def default_port(self) -> str:
        return config.MQTT_PORT

    @staticmethod
    def _write_credential(
        name: str,
        tenant: str,
        host: str,
        port: str,
        username: str,
        password: str,
    ) -> Path:
        """Write an MQTT credential JSON file."""
        creds_data = {
            "device_id": name,
            "auth_type": "password",
            "tenant": tenant,
            "mqtt": {
                "urls": [f"mqtt://{host}:{port}"],
                "credentials": {
                    "username": username,
                    "password": password,
                },
            },
        }

        output_path = config.CREDS_DIR / f"{name}.creds.json"
        with open(output_path, "w") as f:
            json.dump(creds_data, f, indent=2)

        logger.info("Created MQTT credentials: %s (tenant=%s)", output_path, tenant)
        return output_path
