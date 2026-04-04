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
docker compose -f infra/docker-compose-multitenant.yml up -d

# 2. Open the portal
open http://localhost:8080
```

That's it. On first launch:

1. Log in as **admin** / **qwe123**
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
| `SESSION_SECRET` | (built-in) | Secret key for signing session cookies. **Change in production.** |
| `NATS_HOST` | `localhost` | NATS server hostname (embedded in generated credentials) |
| `NATS_PORT` | `4222` | NATS server port |
| `NATS_CONTAINER` | `dc-nats` | Docker container name for NATS (used by Reload NATS) |
| `ETCD_HOST` | `localhost` | etcd server hostname |
| `ETCD_PORT` | `2379` | etcd server port |
| `ADMIN_USER` | `admin` | Admin account username (seeded on startup) |
| `ADMIN_PASS` | `qwe123` | Admin account password (seeded on startup) |
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

## Tech Stack

- **aiohttp** + **aiohttp-jinja2** — async HTTP server with Jinja2 templates
- **Tailwind CSS** (CDN) — utility-class styling, no build step
- **htmx** (CDN) — server-driven interactivity (form submissions, live polling)
- **bcrypt** — password hashing
- **etcd** — user account storage + device registry
- **nsc** — NATS JWT credential generation (called via subprocess)
