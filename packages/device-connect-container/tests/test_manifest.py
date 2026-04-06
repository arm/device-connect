"""Unit tests for device_connect_container.manifest module.

Tests cover:
- ContainerManifest parsing from dict and file
- ContainerConfig defaults and validation
- is_containerized property (container key present vs absent)
- Extended manifest fields (resources, devices, realm, ipc)
"""

import json
from pathlib import Path

import pytest

from device_connect_container.manifest import (
    ContainerManifest,
    ContainerConfig,
    ResourceLimits,
    VolumeMount,
)


# -- Basic ContainerManifest --


class TestContainerManifest:
    def test_minimal_manifest_without_container(self):
        m = ContainerManifest(id="cap-1", class_name="MyClass")
        assert m.id == "cap-1"
        assert m.class_name == "MyClass"
        assert m.entry_point == "capability.py"
        assert m.container is None
        assert m.is_containerized is False

    def test_manifest_with_container_key(self):
        m = ContainerManifest(
            id="vision",
            class_name="VisionCap",
            container=ContainerConfig(image="ghcr.io/arm/vision:1.0"),
        )
        assert m.is_containerized is True
        assert m.container.image == "ghcr.io/arm/vision:1.0"

    def test_manifest_from_dict(self):
        data = {
            "id": "sensor",
            "class_name": "SensorCap",
            "entry_point": "sensor.py",
            "dependencies": {"python": ["numpy>=1.24"]},
            "container": {
                "image": "sensor:latest",
                "resources": {"memory": "128Mi", "cpu": "0.25"},
                "devices": ["/dev/i2c-1"],
                "shm_size": "32Mi",
                "realm": True,
            },
        }
        m = ContainerManifest(**data)
        assert m.is_containerized is True
        assert m.container.resources.memory == "128Mi"
        assert m.container.devices == ["/dev/i2c-1"]
        assert m.container.realm is True

    def test_manifest_from_file(self, tmp_path):
        data = {"id": "test-cap", "class_name": "TestCap"}
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(json.dumps(data))

        m = ContainerManifest.from_manifest_file(manifest_file)
        assert m.id == "test-cap"
        assert m.is_containerized is False

    def test_manifest_from_capability_dir(self, tmp_path):
        cap_dir = tmp_path / "my-cap"
        cap_dir.mkdir()
        data = {
            "id": "my-cap",
            "class_name": "MyCap",
            "container": {"image": "my-cap:latest"},
        }
        (cap_dir / "manifest.json").write_text(json.dumps(data))

        m = ContainerManifest.from_capability_dir(cap_dir)
        assert m.is_containerized is True
        assert m.container.image == "my-cap:latest"

    def test_from_capability_dir_no_manifest_raises(self, tmp_path):
        cap_dir = tmp_path / "empty"
        cap_dir.mkdir()
        with pytest.raises(FileNotFoundError):
            ContainerManifest.from_capability_dir(cap_dir)

    def test_defaults_filled(self):
        m = ContainerManifest(id="x", class_name="X")
        assert m.entry_point == "capability.py"
        assert m.description == ""
        assert m.dependencies == {}


# -- ContainerConfig defaults --


class TestContainerConfig:
    def test_defaults(self):
        cc = ContainerConfig()
        assert cc.image is None
        assert cc.resources.memory == "256Mi"
        assert cc.resources.cpu == "0.5"
        assert cc.devices == []
        assert cc.shm_size == "64Mi"
        assert cc.ipc == "zenoh-shm"
        assert cc.realm is False
        assert cc.env == {}
        assert cc.volumes == []

    def test_custom_values(self):
        cc = ContainerConfig(
            image="test:1",
            resources=ResourceLimits(memory="1Gi", cpu="2"),
            devices=["/dev/video0"],
            ipc="iceoryx2",
            realm=True,
            env={"FOO": "bar"},
            volumes=[VolumeMount(host_path="/data", container_path="/mnt/data")],
        )
        assert cc.ipc == "iceoryx2"
        assert cc.realm is True
        assert cc.volumes[0].host_path == "/data"
        assert cc.volumes[0].read_only is False


# -- ResourceLimits --


class TestResourceLimits:
    def test_defaults(self):
        r = ResourceLimits()
        assert r.memory == "256Mi"
        assert r.cpu == "0.5"
