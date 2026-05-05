# Device Connect Portal

Web-based tenant management portal for multi-tenant Device Connect deployments. Replaces the shell-script workflow (`manage_tenants.sh`, `setup_deployment.sh`) with a self-service UI where users sign up, create devices, and watch them come online.

## Architecture

```
Browser ──> Portal (aiohttp, :8080)
               ├── etcd        (user accounts, device registry)
               ├── nsc CLI     (NATS JWT credential generation)
               └── docker.sock (SIGHUP to reload NATS config)

NATS (:4222)   <── devices connect with JWT credentials
etcd (:2379)   <── shared by portal + registry service
```

The portal runs alongside the existing NATS + etcd + registry stack. It stores user accounts in etcd, generates NATS JWT credentials via the `nsc` CLI, and serves a Tailwind + htmx UI.

## Prerequisites

- **Docker & Docker Compose** (v2)
- **nsc** — NATS credential toolchain (only needed for local/non-Docker runs)
  ```bash
  # macOS
  brew install nsc
  # Linux / Go
  go install github.com/nats-io/nsc/v2@latest
  ```

## Quick Start (Docker Compose)

This is the recommended way to run the portal. It starts NATS, etcd, the registry service, and the portal together.

```bash
cd packages/device-connect-server

# 1. Start the full stack
docker compose -f infra/docker-compose-multitenant-nats.yml up -d --build portal

# 2. Open the portal
open http://localhost:8080
```

That's it. On first launch:

1. Log in as **admin** with the password from the container logs (or the `ADMIN_PASS` env var if you set one)
2. Go to **Admin > Setup** and enter the NATS host (use `nats` if running inside Docker, or your machine's IP if devices connect from outside)
3. The bootstrap creates the NATS operator, account, and privileged credentials

Users can now self-register at `/signup`.

## Local Development (without Docker)

```bash
cd packages/device-connect-server

# 1. Install with portal extras
pip install -e ".[portal]"

# 2. Start etcd (required for user accounts and device registry)
docker run -d --name dc-etcd -p 2379:2379 \
  quay.io/coreos/etcd:v3.5.17 \
  etcd --listen-client-urls http://0.0.0.0:2379 \
        --advertise-client-urls http://localhost:2379

# 3. Start NATS (required for device messaging)
docker run -d --name dc-nats -p 4222:4222 -p 8222:8222 nats:2.10-alpine

# 4. Run the portal
python -m device_connect_server.portal
```

The portal starts on http://localhost:8080.

## Configuration

All settings are via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `PORTAL_PORT` | `8080` | HTTP listen port |
| `PORTAL_HOST` | `0.0.0.0` | HTTP listen address |
| `SESSION_SECRET` | (auto-generated) | Secret key for signing session cookies. Set explicitly for stable sessions across restarts. |
| `NATS_HOST` | `localhost` | NATS server hostname (embedded in generated credentials) |
| `NATS_PORT` | `4222` | NATS server port |
| `NATS_CONTAINER` | `dc-nats` | Docker container name for NATS (used by Reload NATS) |
| `ETCD_HOST` | `localhost` | etcd server hostname |
| `ETCD_PORT` | `2379` | etcd server port |
| `ADMIN_USER` | `admin` | Admin account username (seeded on startup) |
| `ADMIN_PASS` | (auto-generated) | Admin account password (seeded on startup; logged to console if generated) |
| `SECURITY_INFRA_DIR` | `../security_infra` | Path to the `security_infra/` directory containing `.nsc/` state |
| `CREDS_DIR` | `~/.device-connect/credentials` | Path where credential JSON files are written |
| `DC_NSC_ACCOUNT` | `DEVICE_CONNECT` | NSC account name |
| `DC_NSC_OPERATOR` | `device-connect-operator` | NSC operator name |

## User Guide

### Admin Workflow

1. **Log in** at `/login` with the admin credentials
2. **Bootstrap** (first time only): go to `/admin/setup`, enter the NATS host, click "Bootstrap Platform"
3. **Monitor tenants**: the admin dashboard shows all tenants, their credential counts, and live device counts
4. **View as User**: click any tenant to see their dashboard exactly as they see it (read-only)
5. **Health Check**: go to `/admin/health` and run the verification suite to confirm tenant isolation
6. **Reload NATS**: after new tenants sign up, click "Reload NATS" to hot-reload the NATS config (no downtime)

### User Workflow

1. **Sign up** at `/signup` — choose a username (becomes your tenant name) and password
2. **Dashboard**: see live devices as they register (auto-refreshes every 3 seconds)
3. **Create devices**: go to `/devices`, enter a device name, click "Create" — a credential file is generated
4. **Download credentials**: download individual `.creds.json` files or a full `.zip` bundle
5. **Connect a device**: use the connection instructions shown for each device:
   ```bash
   export NATS_CREDENTIALS_FILE=./myuser-robot-001.creds.json
   export NATS_URL=nats://your-host:4222
   export DEVICE_CONNECT_ALLOW_INSECURE=true
   python your_device.py
   ```
6. **Watch it appear**: the device shows up in the Live Devices panel on your dashboard within seconds

### Account Model

- Each user account maps to exactly one tenant namespace
- Tenant namespace: `device-connect.<username>.>`
- Devices within a tenant can communicate freely
- Cross-tenant communication is blocked at the NATS JWT level (cryptographic enforcement)
- Admin has visibility into all tenants but cannot modify them

## API Endpoints

### Public

| Method | Path | Description |
|--------|------|-------------|
| GET | `/login` | Login page |
| POST | `/login` | Authenticate |
| GET | `/signup` | Sign up page |
| POST | `/signup` | Create account + tenant |
| POST | `/logout` | Clear session |

### User (requires login)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/dashboard` | User dashboard with live device panel |
| GET | `/devices` | Device credentials list |
| GET | `/devices/{name}` | Device detail page |
| POST | `/api/devices` | Create a device credential |
| GET | `/api/devices/live` | Live device table (htmx polling) |
| GET | `/api/devices/{name}/creds` | Download credential file |
| GET | `/api/devices/bundle` | Download all credentials as .zip |

### Admin (requires admin role)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin` | Admin dashboard |
| GET | `/admin/tenants/{name}` | View tenant's dashboard (read-only) |
| GET | `/admin/tenants/{name}/devices` | View tenant's devices (read-only) |
| GET | `/admin/health` | Health check page |
| GET | `/admin/setup` | Bootstrap wizard |
| POST | `/api/admin/setup` | Run bootstrap |
| POST | `/api/admin/nats/reload` | Regenerate config + SIGHUP |
| POST | `/api/admin/health/verify` | Run isolation verification |

### Agent API (`/api/agent/v1/*`, requires Bearer token)

JSON-only namespace for coding agents and CI clients. Distinct from browser
routes — never returns HTML, never redirects. Errors are JSON 401/403/4xx
with `{"success": false, "error": {"code", "message"}}`.

Auth: `Authorization: Bearer dcp_...`. Tokens carry per-request scopes:
`devices:read`, `devices:provision`, `devices:credentials`, `devices:invoke`,
`events:read`, `admin:tenants`, `admin:*`.

| Method | Path | Scope | Description |
|--------|------|-------|-------------|
| GET    | `/api/agent/v1/me` | (any) | Token identity + scopes |
| GET    | `/api/agent/v1/fleet` | `devices:read` | Fleet summary |
| GET    | `/api/agent/v1/devices` | `devices:read` | Paginated device list |
| GET    | `/api/agent/v1/devices/{id}` | `devices:read` | Whole device record |
| GET    | `/api/agent/v1/devices/{id}/identity` | `devices:read` | Whole `identity` sub-object |
| GET    | `/api/agent/v1/devices/{id}/status` | `devices:read` | Whole `status` sub-object |
| GET    | `/api/agent/v1/devices/{id}/capabilities` | `devices:read` | `{functions, events}` |
| GET    | `/api/agent/v1/devices/{id}/functions` | `devices:read` | `capabilities.functions` |
| GET    | `/api/agent/v1/devices/{id}/events` | `devices:read` | `capabilities.events` |
| POST   | `/api/agent/v1/devices` | `devices:provision` | Create device + return creds inline |
| GET    | `/api/agent/v1/devices/{id}/credentials` | `devices:credentials` | Re-download creds |
| POST   | `/api/agent/v1/devices/{id}/credentials:rotate` | `devices:credentials` | Rotate creds |
| DELETE | `/api/agent/v1/devices/{id}` | `devices:provision` | Decommission |
| POST   | `/api/agent/v1/devices/{id}/invoke` | `devices:invoke` | Invoke function |
| POST   | `/api/agent/v1/invoke-with-fallback` | `devices:invoke` | Try device list in order |
| GET    | `/api/agent/v1/devices/{id}/events/{event}/stream` | `events:read` | Bounded NDJSON/SSE stream |

Tenant override (`?tenant=other`) requires admin role plus `admin:tenants` or `admin:*`.

#### Bounded event stream

The `stream` endpoint refuses to start unless one of `duration`, `count`, or
`follow=true` is supplied — agents can't accidentally hang on an unbounded
stream. Server hard caps: 1 hour duration, 10 000 events. NDJSON output emits
a final `_meta` line with `closed_by`, `events_received`, `elapsed_s`.

```bash
# At most 5 events or 30 seconds, whichever first
curl -H "Authorization: Bearer $DEVICE_CONNECT_PORTAL_TOKEN" \
  "$DEVICE_CONNECT_PORTAL_URL/api/agent/v1/devices/cam-001/events/motion/stream?format=ndjson&count=5&duration=30"
```

## Token management (`dc-portalctl` + admin CLI)

Mint the first token from the portal host (writes directly to etcd):

```bash
python -m device_connect_server.portalctl.admin_tokens create \
    --user alice --tenant acme \
    --scopes devices:read,devices:invoke,events:read \
    --label ci-bot
# → JSON record. The "token" field (dcp_...) is shown ONLY ONCE — save it.

# List
python -m device_connect_server.portalctl.admin_tokens list

# Revoke
python -m device_connect_server.portalctl.admin_tokens revoke --token-id <id>
```

## `dc-portalctl` — agent-facing CLI

Configure once:

```bash
export DEVICE_CONNECT_PORTAL_URL=http://localhost:8080
export DEVICE_CONNECT_PORTAL_TOKEN=dcp_...
```

Read-only inspection:

```bash
dc-portalctl auth me
dc-portalctl fleet describe
dc-portalctl devices list
dc-portalctl devices identity acme-cam-001
dc-portalctl devices status acme-cam-001        # whole status sub-object
dc-portalctl devices capabilities acme-cam-001  # {functions, events}
dc-portalctl devices functions acme-cam-001
dc-portalctl devices events acme-cam-001
```

Provisioning + credentials:

```bash
# Create + receive credentials inline; pipe to a file
dc-portalctl devices provision cam-001 \
    --device-type camera --location warehouse1/loading-dock \
    --creds-output-file ./acme-cam-001.creds.json

# Re-download
dc-portalctl devices credentials acme-cam-001 --output-file ./acme-cam-001.creds.json
```

Invocation:

```bash
dc-portalctl devices invoke acme-cam-001 capture_frame \
    --params '{"resolution":"4k"}' \
    --reason "Daily inspection job"

dc-portalctl devices invoke-fallback acme-cam-001,acme-cam-002 capture_frame \
    --params '{}' --reason "Fallback to backup"
```

Bounded event stream — at least one of `--duration`, `--count`, `--follow`
must be supplied:

```bash
# Up to 5 events, no longer than 30 s, whichever fires first
dc-portalctl devices stream acme-cam-001 motion_detected \
    --duration 30 --count 5 --format ndjson

# Explicit unbounded (still capped server-side)
dc-portalctl devices stream acme-cam-001 motion_detected --follow
```

Exit codes: `0` ok; `2` no events received within duration window or argument
error; `4` 401; `5` 403; `6` 404; `1` other.

## Tech Stack

- **aiohttp** + **aiohttp-jinja2** — async HTTP server with Jinja2 templates
- **Tailwind CSS** (CDN) — utility-class styling, no build step
- **htmx** (CDN) — server-driven interactivity (form submissions, live polling)
- **bcrypt** — password hashing
- **etcd** — user account storage + device registry
- **nsc** — NATS JWT credential generation (called via subprocess)
