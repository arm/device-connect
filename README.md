# Device Connect

An open-source Python framework by Arm for connecting AI agents to physical devices (such as robots, vehicles, edge/IoT devices, and more). It supports device-to-device (D2D) communication with zero infrastructure or scales to thousands of devices. Zenoh, NATS, and MQTT backends are supported.

## Packages

| Package | Description | PyPI |
|---------|-------------|------|
| [`device-connect-sdk`](packages/device-connect-sdk/) | Python SDK for enabling physical devices to work with Device Connect | `pip install device-connect-sdk` |
| [`device-connect-server`](packages/device-connect-server/) | Registry service, devctl CLI, and Docker infrastructure | `pip install device-connect-server` |
| [`device-connect-agent-tools`](packages/device-connect-agent-tools/) | Framework-agnostic tools for AI agents to discover and invoke devices | `pip install device-connect-agent-tools` |

## Integration Tests

Cross-package integration tests live in [`tests/`](tests/).

## Quick Start

```bash
# Install the SDK
pip install "device-connect-sdk[zenoh]"

# Install agent tools
pip install device-connect-agent-tools

# Run infrastructure (optional — P2P works without it)
cd packages/device-connect-server
docker compose -f infra/docker-compose-dev.yml up -d
```

## License

Apache License 2.0 — see [LICENSE](LICENSE).
