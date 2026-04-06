"""Unit tests for device_connect_container.zenoh_router module.

Tests cover:
- ZenohRouterManager compose generation
- Compose includes router, runtime, and sidecar services
- SHM configuration (ipc: shareable)
- D2D vs routed mode
"""

import json
from pathlib import Path

import pytest

from device_connect_container.zenoh_router import ZenohRouterManager
from device_connect_container.manifest import ContainerManifest


# -- Helpers --


def _write_containerized_cap(tmp_path, cap_id, image=None):
    """Write a containerized capability for compose generation."""
    cap_dir = tmp_path / cap_id
    cap_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": cap_id,
        "class_name": f"{cap_id.title()}Cap",
        "entry_point": "capability.py",
        "container": {
            "image": image or f"{cap_id}:latest",
            "resources": {"memory": "128Mi", "cpu": "0.5"},
        },
    }
    (cap_dir / "manifest.json").write_text(json.dumps(manifest))
    (cap_dir / "capability.py").write_text("class Cap:\n    pass\n")


# -- Compose generation --


class TestZenohRouterManagerCompose:
    def test_compose_has_router_service(self, tmp_path):
        mgr = ZenohRouterManager(device_id="dev-1", capabilities_dir=tmp_path)
        compose = mgr.generate_compose()

        assert "zenoh-router" in compose["services"]
        assert "eclipse/zenoh" in compose["services"]["zenoh-router"]["image"]

    def test_compose_has_runtime_service(self, tmp_path):
        mgr = ZenohRouterManager(device_id="dev-1", capabilities_dir=tmp_path)
        compose = mgr.generate_compose()

        assert "device-runtime" in compose["services"]
        runtime = compose["services"]["device-runtime"]
        assert "zenoh-router" in runtime["depends_on"]

    def test_compose_includes_capability_sidecars(self, tmp_path):
        _write_containerized_cap(tmp_path, "vision")
        _write_containerized_cap(tmp_path, "arm")

        mgr = ZenohRouterManager(device_id="robot-001", capabilities_dir=tmp_path)
        compose = mgr.generate_compose()

        assert "cap-vision" in compose["services"]
        assert "cap-arm" in compose["services"]
        assert compose["services"]["cap-vision"]["image"] == "vision:latest"

    def test_compose_sidecar_env(self, tmp_path):
        _write_containerized_cap(tmp_path, "sensor")

        mgr = ZenohRouterManager(device_id="dev-1", tenant="lab", capabilities_dir=tmp_path)
        compose = mgr.generate_compose()

        env = compose["services"]["cap-sensor"]["environment"]
        assert env["DEVICE_ID"] == "dev-1"
        assert env["TENANT"] == "lab"
        assert env["ZENOH_ROUTER_ENDPOINT"] == "tcp/zenoh-router:7447"

    def test_compose_shm_mode(self, tmp_path):
        mgr = ZenohRouterManager(device_id="dev-1", capabilities_dir=tmp_path)
        compose = mgr.generate_compose(shm_enabled=True)

        router = compose["services"]["zenoh-router"]
        assert router.get("ipc") == "shareable"
        assert "shm_size" in router

        runtime = compose["services"]["device-runtime"]
        assert "container:zenoh-router" in str(runtime.get("ipc", ""))

    def test_compose_d2d_mode_no_scouting_flag(self, tmp_path):
        mgr = ZenohRouterManager(device_id="dev-1", capabilities_dir=tmp_path)
        compose = mgr.generate_compose(d2d_mode=True)

        # D2D mode: no --no-multicast-scouting flag
        router = compose["services"]["zenoh-router"]
        cmd = router.get("command", "")
        assert "--no-multicast-scouting" not in cmd

    def test_compose_routed_mode_has_scouting_flag(self, tmp_path):
        mgr = ZenohRouterManager(device_id="dev-1", capabilities_dir=tmp_path)
        compose = mgr.generate_compose(d2d_mode=False)

        router = compose["services"]["zenoh-router"]
        assert "--no-multicast-scouting" in router.get("command", "")

    def test_compose_upstream_endpoints(self, tmp_path):
        mgr = ZenohRouterManager(device_id="dev-1", capabilities_dir=tmp_path)
        compose = mgr.generate_compose(
            d2d_mode=False,
            upstream_endpoints=["tcp/infra-router:7447"],
        )

        router = compose["services"]["zenoh-router"]
        assert "tcp/infra-router:7447" in router.get("command", "")

    def test_compose_has_network(self, tmp_path):
        mgr = ZenohRouterManager(device_id="dev-1", capabilities_dir=tmp_path)
        compose = mgr.generate_compose()

        assert "dev-1-net" in compose["networks"]

    def test_compose_skips_non_containerized(self, tmp_path):
        # Write a non-containerized capability
        cap_dir = tmp_path / "local-cap"
        cap_dir.mkdir()
        manifest = {"id": "local-cap", "class_name": "LocalCap"}
        (cap_dir / "manifest.json").write_text(json.dumps(manifest))
        (cap_dir / "capability.py").write_text("class LocalCap:\n    pass\n")

        mgr = ZenohRouterManager(device_id="dev-1", capabilities_dir=tmp_path)
        compose = mgr.generate_compose()

        # Only router and runtime — no sidecar for non-containerized
        assert "cap-local-cap" not in compose["services"]
