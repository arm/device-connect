# device-connect-server

Server-side runtime for the Device Connect framework. Extends [device-connect-edge](../device-connect-edge/) with device registry, security (commissioning, ACLs), distributed state, audit logging, and CLI tools.

## Contents

- [Where This Fits](#where-this-fits)
- [Install](#install)
- [Quick Start](#quick-start)
- [Multi-Tenant Deployment](#multi-tenant-deployment)
- [CLI Tools](#cli-tools)
- [Device Commissioning Flow](#device-commissioning-flow)
- [Testing](#testing)
- [Contributing](#contributing)

## Where This Fits

```
  device-connect-edge          device-connect-server           device-connect-agent-tools
  (edge runtime)          (server runtime — this)   (agent SDK)
        │                         │                         │
        └──────────────── Mesh ─────────────────────────────┘
```

- **device-connect-edge** — runs on physical devices (Raspberry Pi, robots, cameras, sensors)
- **device-connect-server** — runs on servers. Adds registry, security, state, and CLIs
- **device-connect-agent-tools** — connects AI agents (Strands, LangChain, MCP) to the device mesh

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install device-connect-server
```

Optional extras:

| Extra | Adds |
|-------|------|
| `[security]` | bcrypt, aiohttp, zeroconf, qrcode (commissioning + ACLs) |
| `[telemetry]` | OpenTelemetry SDK + OTLP exporters |
| `[state]` | etcd3gw (distributed state store) |
| `[logging]` | pymongo (audit logging to MongoDB) |
| `[mqtt]` | aiomqtt (MQTT messaging backend) |
| `[all]` | All of the above + dev tools |

> **Note:** Zenoh is now a core dependency of `device-connect-edge` and is included automatically.

## Quick Start

> **Prerequisites:** Complete the [Install](#install) steps first (venv active, packages installed). You also need Docker. For JWT auth you additionally need [nsc](https://github.com/nats-io/nsc) (`brew install nsc`).

### 1. Start infrastructure

Choose a deployment mode:

<details open>
<summary><b>Zenoh (dev mode, no auth)</b></summary>

```bash
docker compose -f infra/docker-compose-dev.yml up -d
```

</details>

<details>
<summary><b>Secure — Zenoh (TLS)</b></summary>

```bash
# 1. Generate CA + Zenoh router + registry client certificates
./security_infra/generate_tls_certs.sh zenoh

# 2. Generate a client certificate for each device
./security_infra/generate_tls_certs.sh --client rng-001

# 3. Start infrastructure with TLS
docker compose -f infra/docker-compose.yml up -d
```

</details>

<details>
<summary><b>NATS (dev mode, no auth)</b></summary>

```bash
docker compose -f infra/docker-compose-nats-dev.yml up -d
```

</details>

<details>
<summary><b>Authenticated — NATS (JWT auth)</b></summary>

```bash
# 1. Generate NATS JWT config
./security_infra/setup_jwt_auth.sh dev

# 2. Generate credentials for built-in roles (registry, devctl)
./security_infra/gen_creds.sh --all --force

# 3. Generate credentials for each device and agent
./security_infra/gen_creds.sh --user rng-001
./security_infra/gen_creds.sh --user my-agent

# 4. Start infrastructure with JWT auth
docker compose -f infra/docker-compose-nats.yml up -d
```

</details>

| Service | Port | Purpose |
|---------|------|---------|
| zenoh | 7447 | Zenoh messaging |
| nats / nats-jwt | 4222 | NATS messaging |
| etcd | 2379 | Distributed state store |
| device-registry-service | 8080 | Device registration and discovery |

### 2. Connect a simulated device

The number generator simulator connects to the messaging backend, registers itself with the device registry, and emits `number_generated` events every 5 seconds.

```bash
cd ../device-connect-edge  # sibling package in the monorepo

# Zenoh (dev mode, no auth)
DEVICE_CONNECT_ALLOW_INSECURE=true ZENOH_CONNECT=tcp/localhost:7447 \
  python examples/number_generator/device_simulator.py

# Secure — Zenoh (TLS)
# ZENOH_CONNECT=tls/localhost:7447 \
#   MESSAGING_TLS_CA_FILE=../device-connect-server/security_infra/ca.pem \
#   MESSAGING_TLS_CERT_FILE=../device-connect-server/security_infra/rng-001-cert.pem \
#   MESSAGING_TLS_KEY_FILE=../device-connect-server/security_infra/rng-001-key.pem \
#   python examples/number_generator/device_simulator.py

# Insecure — NATS (no auth)
# DEVICE_CONNECT_ALLOW_INSECURE=true python examples/number_generator/device_simulator.py

# Authenticated — NATS (JWT auth)
# NATS_CREDENTIALS_FILE=~/.device-connect/credentials/rng-001.creds.json \
#   NATS_URL=nats://localhost:4222 python examples/number_generator/device_simulator.py
```

### 3. Verify the device registered

`devctl list` queries the registry service and returns all connected devices with their capabilities, identity, and status. `statectl` reads the same data directly from etcd.

```bash
# Zenoh (dev mode, no auth)
ZENOH_CONNECT=tcp/localhost:7447 devctl list
statectl --raw list /device-connect/default/devices/

# Secure — Zenoh (TLS)
# ZENOH_CONNECT=tls/localhost:7447 \
#   MESSAGING_TLS_CA_FILE=security_infra/ca.pem \
#   MESSAGING_TLS_CERT_FILE=security_infra/rng-001-cert.pem \
#   MESSAGING_TLS_KEY_FILE=security_infra/rng-001-key.pem \
#   devctl list

# Insecure — NATS (no auth)
# devctl list

# Authenticated — NATS (JWT auth)
# NATS_CREDENTIALS_FILE=~/.device-connect/credentials/devctl.creds.json \
#   NATS_URL=nats://localhost:4222 devctl list
```

### 4. Connect an AI agent

The Strands Agent subscribes to all device events on the mesh (`device-connect.default.*.event.>`), batches them over a time window, and sends them to Claude for analysis. Claude can call back into devices using `invoke_device()` and `get_device_status()` tools.

```bash
pip install "device-connect-agent-tools[strands]"
```

```python
from device_connect_agent_tools.adapters.strands_agent import StrandsDeviceConnectAgent

agent = StrandsDeviceConnectAgent(
    goal="Monitor devices and react to events",
    model_id="claude-sonnet-4-20250514",
)

async with agent:
    await agent.run()  # subscribes to events, batches, and prompts the LLM
```

```bash
# Insecure — Zenoh (no auth)
ZENOH_CONNECT=tcp/localhost:7447 \
  DEVICE_CONNECT_ALLOW_INSECURE=true \
  ANTHROPIC_API_KEY="sk-ant-..." python my_agent.py

# Secure — Zenoh (TLS)
# ZENOH_CONNECT=tls/localhost:7447 \
#   MESSAGING_TLS_CA_FILE=security_infra/ca.pem \
#   MESSAGING_TLS_CERT_FILE=security_infra/my-agent-cert.pem \
#   MESSAGING_TLS_KEY_FILE=security_infra/my-agent-key.pem \
#   ANTHROPIC_API_KEY="sk-ant-..." python my_agent.py

# Insecure — NATS (no auth)
# DEVICE_CONNECT_ALLOW_INSECURE=true \
#   ANTHROPIC_API_KEY="sk-ant-..." python my_agent.py

# Authenticated — NATS (JWT auth)
# ANTHROPIC_API_KEY="sk-ant-..." \
#   NATS_CREDENTIALS_FILE=~/.device-connect/credentials/my-agent.creds.json \
#   NATS_URL=nats://localhost:4222 python my_agent.py
```

> Get an API key at [console.anthropic.com](https://console.anthropic.com/).

### 5. Tear down

```bash
# Insecure — Zenoh (no auth)
docker compose -f infra/docker-compose-dev.yml down

# Secure — Zenoh (TLS)
# docker compose -f infra/docker-compose.yml down

# Insecure — NATS (no auth)
# docker compose -f infra/docker-compose-nats-dev.yml down

# Authenticated — NATS (JWT auth)
# docker compose -f infra/docker-compose-nats.yml down
```

To clean up everything:

```bash
rm -rf .venv
rm -rf ~/.device-connect/credentials
rm -rf security_infra/.nsc security_infra/nats-jwt-generated.conf
rm -f security_infra/*.pem security_infra/*.srl
```

See [device-connect-edge — Credentials](../device-connect-edge/README.md#credentials) for the credentials file format.

## Multi-Tenant Deployment

For deployments where multiple groups share the same infrastructure (workshops, labs, multi-team environments), you can enforce per-tenant isolation at the NATS broker level using JWT credentials. Each tenant's devices get JWT tokens restricted to their own namespace, making cross-tenant access cryptographically impossible.

```bash
cd security_infra

# 1. Bootstrap (once)
./setup_deployment.sh --nats-host dc.example.com

# 2. Create tenants with device tokens
./manage_tenants.sh create-batch alpha,beta,gamma --devices 5 --nats-host dc.example.com

# 3. Start infrastructure
DC_TENANTS=alpha,beta,gamma docker compose -f ../infra/docker-compose-multitenant-nats.yml up -d

# 4. Verify isolation
./verify_tenants.sh --nats-host dc.example.com
```

Each tenant gets a distributable credential bundle (zip) in `security_infra/tenant-bundles/`. See the full guide in [security_infra/README.md](security_infra/README.md).

## CLI Tools

```bash
# Device control
devctl list                                # list registered devices
devctl list --compact                      # compact output
devctl register --id myDevice --keepalive  # register a test device
devctl discover --timeout 5               # find uncommissioned devices (mDNS)
devctl commission cam-001 --pin 1234-5678  # commission a device
devctl interactive                         # REPL for device operations

# State management
statectl get experiments/EXP-001           # read a key
statectl list experiments/                 # list keys under prefix
statectl set experiments/EXP-001 '{"status": "done"}' --ttl 3600
statectl delete experiments/EXP-001        # delete a key
statectl watch experiments/ --prefix       # watch for changes
statectl locks                             # list held locks
statectl stats                             # key counts by namespace
```

## Device Commissioning Flow

New devices must be provisioned and commissioned before joining the mesh. The mechanism depends on your messaging backend:

- **Zenoh** — uses mTLS (mutual TLS). Each device gets a client certificate signed by the shared CA. Generate one with `./security_infra/generate_tls_certs.sh --client <device-id>`.
- **NATS** — uses JWT credentials. Each device gets a JWT + NKey pair. Generate one with `./security_infra/gen_creds.sh --user <device-id>`.

### NATS JWT commissioning (detailed)

Using `camera-001` as an example:

1. **Provision** (factory): generate an identity with NKey keypair + factory PIN
2. **Generate credentials** (admin): `./security_infra/gen_creds.sh --user camera-001`
3. **Commission** (admin): `devctl commission camera-001 --pin 1234-5678`
   - Delivers credentials to the device's commissioning HTTP server
   - Device saves credentials and connects to NATS
4. **Operate** (every boot): device loads credentials from disk, connects, registers, starts heartbeats

Each device becomes a user under the shared DEVICE_CONNECT account. By default all users can communicate over `device-connect.*` subjects. Use `--tenant` to restrict a user's JWT to a specific tenant namespace:

```
Operator: device-connect-operator   (trust root)
  └─ Account: DEVICE_CONNECT        (shared namespace)
       ├─ User: registry             (privileged — device-connect.>)
       ├─ User: devctl               (privileged — device-connect.>)
       ├─ User: orchestrator         (privileged — device-connect.>)
       ├─ User: camera-001           (--user, default tenant)
       ├─ User: alpha-device-001     (--tenant alpha — device-connect.alpha.>)
       └─ User: beta-device-001      (--tenant beta — device-connect.beta.>)
```

For multi-tenant deployments (workshops, labs), see [Multi-Tenant Deployment](#multi-tenant-deployment) and the full guide in [security_infra/README.md](security_infra/README.md).

See [device-connect-edge — Credentials](../device-connect-edge/README.md#credentials) for how devices consume credentials at runtime.

AI agents connecting via [device-connect-agent-tools](../device-connect-agent-tools/) also need their own credentials: `./security_infra/gen_creds.sh --user my-agent` (NATS) or `./security_infra/generate_tls_certs.sh --client my-agent` (Zenoh).

## Testing

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[all]"
pytest tests/ -v --timeout=30
```

Unit tests run without external services. Integration tests are in [tests/](../../tests/).

## Contributing

We welcome contributions! Please open an [issue](https://github.com/arm/device-connect/issues) to report bugs or suggest features, or submit a [pull request](https://github.com/arm/device-connect/pulls) directly.
