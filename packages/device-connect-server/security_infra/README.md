# Security Infrastructure Scripts

Tools for setting up NATS JWT authentication and multi-tenant isolation for Device Connect deployments.

## Contents

- [Security Infrastructure Scripts](#security-infrastructure-scripts)
  - [Contents](#contents)
  - [Overview](#overview)
  - [Prerequisites](#prerequisites)
  - [Single-Tenant Setup (Quick Start)](#single-tenant-setup-quick-start)
  - [Multi-Tenant Setup](#multi-tenant-setup)
    - [1. Bootstrap the deployment](#1-bootstrap-the-deployment)
    - [2. Create tenants](#2-create-tenants)
    - [3. Start infrastructure](#3-start-infrastructure)
    - [4. Verify isolation](#4-verify-isolation)
    - [5. Distribute credentials](#5-distribute-credentials)
  - [Managing Tenants](#managing-tenants)
    - [Add a new tenant](#add-a-new-tenant)
    - [Add a device to an existing tenant](#add-a-device-to-an-existing-tenant)
    - [List tenants and credentials](#list-tenants-and-credentials)
    - [Hot-reload NATS after changes](#hot-reload-nats-after-changes)
  - [How Isolation Works](#how-isolation-works)
    - [1. NATS JWT subject permissions (broker-enforced)](#1-nats-jwt-subject-permissions-broker-enforced)
    - [2. Application-level tenant namespacing](#2-application-level-tenant-namespacing)
    - [What about Zenoh?](#what-about-zenoh)
  - [Connecting Devices](#connecting-devices)
  - [Script Reference](#script-reference)
    - [gen\_creds.sh flags](#gen_credssh-flags)
    - [Environment variables](#environment-variables)
  - [TLS Certificates](#tls-certificates)

## Overview

Device Connect uses **tenants** to namespace devices. All messaging subjects follow the pattern `device-connect.{tenant}.{device_id}.{suffix}`, and the device registry stores entries under `/device-connect/{tenant}/devices/`. By default this is application-level separation — devices respect their configured tenant, but the broker doesn't enforce boundaries.

For deployments where multiple groups share the same infrastructure (workshops, labs, staging environments), you can enable **broker-enforced isolation** using NATS JWT credentials. Each tenant's devices receive JWT tokens that restrict their publish/subscribe permissions to `device-connect.{their-tenant}.>`. The NATS server rejects any attempt to access another tenant's subjects.

```
┌──────────────────────────────────────────────────┐
│                  NATS Server                     │
│                 (JWT auth)                       │
│                                                  │
│  ┌───────────────┐  ┌──────────────┐             │
│  │ tenant alpha  │  │ tenant beta  │  ...        │
│  │ dc.alpha.>    │  │ dc.beta.>    │             │
│  └───────────────┘  └──────────────┘             │
│           ▲                 ▲                    │
│      JWT allows        JWT allows                │
│      dc.alpha.>        dc.beta.>                 │
├───────────┼─────────────────┼────────────────────┤
│  Registry (privileged): dc.> across all tenants  │
└──────────────────────────────────────────────────┘
```

## Prerequisites

| Tool | Required for | Install |
|------|-------------|---------|
| [nsc](https://github.com/nats-io/nsc) | All JWT scripts | `brew install nsc` (macOS) or `go install github.com/nats-io/nsc/v2@latest` (then `sudo ln -sf "$(go env GOPATH)/bin/nsc" /usr/local/bin/nsc`) |
| [Docker](https://docs.docker.com/get-docker/) | Running infrastructure | docker.com |
| [nats CLI](https://github.com/nats-io/natscli) | `verify_tenants.sh` only | `brew install nats-io/nats-tools/nats` |
| Python 3.10+ | `manage_tenants.sh list` | Usually pre-installed |

## Single-Tenant Setup (Quick Start)

For a standard single-tenant deployment with JWT auth (no multi-tenant isolation):

```bash
# 1. Create NATS JWT infrastructure
./setup_jwt_auth.sh dev

# 2. Generate credentials for built-in roles + your devices
./gen_creds.sh --all --force
./gen_creds.sh --user camera-001
./gen_creds.sh --user robot-arm-001

# 3. Start infrastructure
docker compose -f ../infra/docker-compose-nats.yml up -d
```

See the [device-connect-server README](../README.md) for the full walkthrough.

## Multi-Tenant Setup

### 1. Bootstrap the deployment

Run once to create the NATS JWT operator/account and generate privileged credentials (registry service, facilitator/admin):

```bash
./setup_deployment.sh --nats-host dc.example.com
```

Replace `dc.example.com` with the public hostname or IP where your NATS server will be reachable. This produces:

| Output | Purpose |
|--------|---------|
| `nats-jwt-generated.conf` | NATS server config with JWT resolver |
| `~/.device-connect/credentials/registry.creds.json` | Registry service credentials (all-tenant access) |
| `~/.device-connect/credentials/facilitator.creds.json` | Admin credentials (all-tenant access) |

### 2. Create tenants

Create one or more tenants, each with a set of device tokens:

```bash
# Single tenant with 5 device tokens
./manage_tenants.sh create alpha --devices 5 --nats-host dc.example.com

# Multiple tenants at once
./manage_tenants.sh create-batch alpha,beta,gamma,delta --devices 5 --nats-host dc.example.com
```

Each device token is a `.creds.json` file containing a JWT scoped to `device-connect.{tenant}.>`. Tokens are saved to `~/.device-connect/credentials/` and bundled into distributable zip files under `tenant-bundles/`.

### 3. Start infrastructure

```bash
# Set DC_TENANTS to the comma-separated list of tenants you created:
DC_TENANTS=alpha,beta,gamma,delta \
  docker compose -f ../infra/docker-compose-multitenant-nats.yml up -d
```

This starts:
- **dc-nats** — NATS server with JWT auth on port 4222
- **dc-etcd** — etcd state store on port 2379
- **dc-registry** — Device registry service subscribed to all listed tenants

Alternatively, copy `../infra/.env.multitenant.example` to `../infra/.env`, edit the tenant list, and run `docker compose` without the env prefix.

### 4. Verify isolation

```bash
./verify_tenants.sh --nats-host dc.example.com
```

This runs automated checks:
- NATS server is reachable
- Privileged credentials can publish to all tenants
- Each tenant can publish to its own namespace
- Cross-tenant publish is **denied** (permissions violation)
- Cross-tenant subscribe is **denied**

Example output:
```
--- Test 3: Same-tenant publish (should succeed) ---
  PASS: Tenant 'alpha' can publish to own namespace
  PASS: Tenant 'beta' can publish to own namespace

--- Test 4: Cross-tenant publish (should be denied) ---
  PASS: Tenant 'alpha' correctly DENIED publish to 'beta'
  PASS: Tenant 'beta' correctly DENIED publish to 'alpha'

  Results: 7/7 passed, 0 failed
```

### 5. Distribute credentials

Each tenant gets a zip bundle in `tenant-bundles/`:

```
tenant-bundles/
  alpha/
    credentials/
      alpha-device-001.creds.json
      alpha-device-002.creds.json
      ...
    tenant-config.env
  alpha.zip
  beta.zip
  ...
```

Give each group their zip file. The `tenant-config.env` inside sets the required environment variables:

```bash
source tenant-config.env
export NATS_CREDENTIALS_FILE=./credentials/alpha-device-001.creds.json
```

## Managing Tenants

All tenant management is done with `manage_tenants.sh`. Changes take effect after reloading the NATS config.

### Add a new tenant

```bash
./manage_tenants.sh create epsilon --devices 3 --nats-host dc.example.com
```

Then update `DC_TENANTS` in your docker-compose `.env` to include the new tenant and restart the registry:

```bash
docker compose -f ../infra/docker-compose-multitenant-nats.yml up -d dc-registry
```

### Add a device to an existing tenant

```bash
./manage_tenants.sh add-device alpha my-custom-robot --nats-host dc.example.com
```

This creates a new credential file scoped to `device-connect.alpha.>`, rebuilds the tenant bundle, and regenerates the NATS config.

### List tenants and credentials

```bash
./manage_tenants.sh list
```

Output:
```
  [privileged] registry — registry.creds.json
  [privileged] facilitator — facilitator.creds.json

  Tenant: alpha (5 devices)
    alpha-device-001 — alpha-device-001.creds.json
    alpha-device-002 — alpha-device-002.creds.json
    ...

  Tenant: beta (5 devices)
    ...
```

### Hot-reload NATS after changes

If NATS is already running, reload its config without downtime after creating tenants or devices:

```bash
./manage_tenants.sh reload-nats
```

This regenerates `nats-jwt-generated.conf` and sends `SIGHUP` to the `dc-nats` container. Use `--container NAME` if your container has a different name.

## How Isolation Works

Isolation is enforced at two levels:

### 1. NATS JWT subject permissions (broker-enforced)

Each tenant device's JWT allows:
```
publish:   device-connect.{tenant}.>
subscribe: device-connect.{tenant}.>
publish:   _INBOX.>
subscribe: _INBOX.>
```

Privileged roles (registry, facilitator) allow:
```
publish:   device-connect.>
subscribe: device-connect.>
```

The NATS server rejects any publish or subscribe that falls outside the JWT's allowed subjects. This is cryptographic enforcement — the JWT is signed by the account's signing key and cannot be forged.

### 2. Application-level tenant namespacing

All Device Connect subjects embed the tenant as the second segment:
```
device-connect.{tenant}.{device_id}.cmd         → RPC commands
device-connect.{tenant}.{device_id}.event.{name} → device events
device-connect.{tenant}.{device_id}.heartbeat    → heartbeats
device-connect.{tenant}.{device_id}.presence     → D2D presence
device-connect.{tenant}.registry                 → registration RPC
device-connect.{tenant}.discovery                → discovery queries
```

The device registry stores entries under `/device-connect/{tenant}/devices/{device_id}` in etcd, so `list_devices("alpha")` only returns alpha's devices.

### What about Zenoh?

Zenoh does not have an ACL or permission system. If you use Zenoh as the messaging backend, tenant isolation is application-level only (subject naming conventions). For broker-enforced isolation, use NATS with JWT auth.

## Connecting Devices

Once a tenant has credentials, each device connects by setting three environment variables:

```bash
export TENANT=alpha
export NATS_URL=nats://dc.example.com:4222
export NATS_CREDENTIALS_FILE=./credentials/alpha-device-001.creds.json
export MESSAGING_BACKEND=nats
```

Then run any device-connect-edge device normally:

```python
from device_connect_edge import DeviceDriver, DeviceRuntime, rpc

class MyRobot(DeviceDriver):
    @rpc
    def move(self, x: float, y: float):
        return {"moved_to": [x, y]}

import asyncio
async def main():
    rt = DeviceRuntime(MyRobot(), device_id="alpha-device-001", tenant="alpha")
    await rt.start()
    await asyncio.Event().wait()

asyncio.run(main())
```

The device will register in the `alpha` tenant, and only devices within `alpha` will discover it.

## Script Reference

| Script | Purpose |
|--------|---------|
| `setup_deployment.sh` | One-time bootstrap: creates NATS JWT operator/account + privileged credentials |
| `manage_tenants.sh` | Create/manage tenants and device tokens (create, create-batch, add-device, list, reload-nats) |
| `verify_tenants.sh` | Automated smoke test for cross-tenant isolation |
| `setup_jwt_auth.sh` | Low-level: creates NATS operator and account (called by setup_deployment.sh) |
| `gen_creds.sh` | Low-level: generates a single credential file (called by manage_tenants.sh) |
| `generate_tls_certs.sh` | Generate TLS certificates for Zenoh or NATS |

### gen_creds.sh flags

| Flag | Description |
|------|-------------|
| `--tenant TENANT` | Scope JWT to `device-connect.{TENANT}.>` |
| `--privileged` | Allow `device-connect.>` (all tenants) |
| `--user NAME` | User/device name for the credential |
| `--nats-host HOST` | NATS hostname embedded in the creds file |
| `--nats-port PORT` | NATS port embedded in the creds file (default: 4222) |
| `--all` | Generate all built-in privileged roles |
| `--force` | Overwrite existing credential files |

### Environment variables

| Variable | Used by | Purpose |
|----------|---------|---------|
| `DC_NSC_OPERATOR` | `setup_jwt_auth.sh` | NATS operator name (default: `device-connect-operator`) |
| `DC_NSC_ACCOUNT` | `setup_jwt_auth.sh`, `gen_creds.sh` | NATS account name (default: `DEVICE_CONNECT`) |
| `NATS_HOST` | `gen_creds.sh`, `manage_tenants.sh` | Default NATS host if `--nats-host` not passed |
| `DC_TENANTS` | `docker-compose-multitenant-nats.yml` | Comma-separated tenant list for the registry service |

## TLS Certificates

For TLS encryption (independent of JWT tenant isolation), see `generate_tls_certs.sh`:

```bash
./generate_tls_certs.sh nats             # CA + NATS server cert
./generate_tls_certs.sh --client dev-001  # Per-device client cert
```

TLS and JWT can be combined: use `tls://` NATS URLs and provide both TLS certs and JWT credentials.
