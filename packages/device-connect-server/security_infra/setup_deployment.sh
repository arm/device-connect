#!/usr/bin/env bash
# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

#
# One-time setup of multi-tenant NATS JWT infrastructure + privileged credentials.
#
# Usage:
#   ./setup_deployment.sh --nats-host dc.example.com
#   ./setup_deployment.sh --nats-host 192.168.1.100 --nats-port 4222
#
# Prerequisites:
#   - nsc (brew install nsc OR go install github.com/nats-io/nsc/v2@latest)
#
# After this, use manage_tenants.sh to create tenants and device tokens.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NATS_HOST=""
NATS_PORT="4222"
ENABLE_WEBSOCKET=0
WS_PORT="8443"
WS_ALLOWED_ORIGINS=""
WS_TLS_CERT=""
WS_TLS_KEY=""

usage() {
  cat <<'USAGE'
Usage: setup_deployment.sh --nats-host HOST [options]

One-time bootstrap for multi-tenant NATS JWT infrastructure. Creates the
NATS operator/account and privileged credentials (registry, facilitator).

Required:
  --nats-host HOST                Public hostname or IP of the NATS server.

Optional:
  --nats-port PORT                NATS TCP port (default: 4222).

Browser-based devices (WebSocket):
  --enable-websocket              Add a `websocket {}` block to the generated
                                  NATS config so browser-based devices can
                                  connect over WS (nats.ws / @nats-io/nats-core).
                                  OFF by default; existing deployments are
                                  unaffected.
  --websocket-port PORT           WS listen port inside the container (default: 8443).
  --websocket-allowed-origins LIST
                                  Comma-separated list of allowed Origin headers.
                                  Defaults to empty, which keeps nats-server's
                                  same_origin=true behavior. Set this only when
                                  a reverse proxy rewrites Host headers (e.g.
                                  the page is at https://app.example.com and
                                  the WS endpoint is wss://app.example.com/nats
                                  proxied to a local NATS).
  --websocket-tls-cert FILE       Native TLS cert (path inside the NATS
  --websocket-tls-key FILE        container). When both are set, NATS does
                                  TLS termination itself; otherwise the
                                  listener is plain WS and MUST be fronted by
                                  TLS (Caddy / nginx) before public exposure.

After running this, use manage_tenants.sh to create tenants.

Security notes for --enable-websocket:
  * The compose port for the WS listener is provided via
    infra/docker-compose-nats-websocket.yml (loopback-bound by default).
    Combine it with the main compose file. The container-side WS port is
    read from DC_NATS_WS_PORT (default 8443) and MUST match
    --websocket-port -- otherwise NATS listens on one port and compose
    maps a different one, silently leaving the listener unreachable.
        DC_NATS_WS_PORT=8443 docker compose \
            -f infra/docker-compose-multitenant-nats.yml \
            -f infra/docker-compose-nats-websocket.yml up -d
    (The "WebSocket listener enabled" message at the end of this script
    prints the exact invocation including the value you passed.)
  * Do NOT change the loopback binding without putting TLS in front; without
    TLS, NATS JWTs travel in cleartext.
USAGE
  exit 1
}

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --nats-host)                  NATS_HOST="$2"; shift 2 ;;
    --nats-port)                  NATS_PORT="$2"; shift 2 ;;
    --enable-websocket)           ENABLE_WEBSOCKET=1; shift ;;
    --websocket-port)             WS_PORT="$2"; shift 2 ;;
    --websocket-allowed-origins)  WS_ALLOWED_ORIGINS="$2"; shift 2 ;;
    --websocket-tls-cert)         WS_TLS_CERT="$2"; shift 2 ;;
    --websocket-tls-key)          WS_TLS_KEY="$2"; shift 2 ;;
    -h|--help)                    usage ;;
    *)                            echo "Unknown option: $1"; usage ;;
  esac
done

# TLS pair: both or neither.
if { [ -n "$WS_TLS_CERT" ] && [ -z "$WS_TLS_KEY" ]; } || \
   { [ -z "$WS_TLS_CERT" ] && [ -n "$WS_TLS_KEY" ]; }; then
  echo "Error: --websocket-tls-cert and --websocket-tls-key must be used together."
  exit 1
fi

# Port arguments must be numeric -- a typo like `--websocket-port 84as3`
# would otherwise flow into the generated config and only fail later.
if ! [[ "$NATS_PORT" =~ ^[0-9]+$ ]]; then
  echo "Error: --nats-port must be numeric (got: ${NATS_PORT})."
  exit 1
fi
if [ "$ENABLE_WEBSOCKET" -eq 1 ] && ! [[ "$WS_PORT" =~ ^[0-9]+$ ]]; then
  echo "Error: --websocket-port must be numeric (got: ${WS_PORT})."
  exit 1
fi

if [ -z "$NATS_HOST" ]; then
  echo "Error: --nats-host is required"
  echo ""
  usage
fi

# Check prerequisites
if ! command -v nsc &>/dev/null; then
  echo "Error: nsc is not installed."
  echo ""
  echo "Install nsc:"
  echo "  macOS:   brew install nsc"
  echo "  go:      go install github.com/nats-io/nsc/v2@latest"
  echo "  binary:  https://github.com/nats-io/nsc/releases"
  exit 1
fi

echo "============================================"
echo "  Device Connect — Deployment Setup"
echo "============================================"
echo "  NATS host: ${NATS_HOST}:${NATS_PORT}"
echo ""

# Step 1: Create NATS JWT infrastructure
echo "==> Step 1: Setting up NATS JWT auth infrastructure"
"${SCRIPT_DIR}/setup_jwt_auth.sh" dev

# Step 2: Generate privileged credentials
echo ""
echo "==> Step 2: Generating privileged credentials"

export NATS_HOST NATS_PORT

"${SCRIPT_DIR}/gen_creds.sh" --privileged --user registry --nats-host "$NATS_HOST" --nats-port "$NATS_PORT" --force
"${SCRIPT_DIR}/gen_creds.sh" --privileged --user facilitator --nats-host "$NATS_HOST" --nats-port "$NATS_PORT" --force

# Step 3: Regenerate NATS config (includes updated account JWT with new users)
echo ""
echo "==> Step 3: Regenerating NATS server config"

NSC_HOME="${SCRIPT_DIR}/.nsc"
export NKEYS_PATH="${NSC_HOME}/nkeys"
export NSC_HOME
export XDG_DATA_HOME="${NSC_HOME}/data"
export XDG_CONFIG_HOME="${NSC_HOME}/config"

OUTPUT_CONF="${SCRIPT_DIR}/nats-jwt-generated.conf"
# Newer nsc (v2.12+) refuses to overwrite an existing --config-file, and Step 1
# (setup_jwt_auth.sh) has already created this file, so remove it first.
rm -f "${OUTPUT_CONF}"
nsc generate config --mem-resolver --config-file "${OUTPUT_CONF}"

# Re-append listen directives (nsc generate overwrites the file)
cat >> "${OUTPUT_CONF}" <<EOF

# Device Connect additions
listen: 0.0.0.0:4222
http_port: 8222
# Raised from the 1MB default so the registry can return fleet snapshots
# for large deployments (~1400 devices at ~6KB/record = ~8MB).
max_payload: 8MB
EOF

# Optional: WebSocket listener for browser-based devices.
# Operator-mode JWT auth applies identically to WS and TCP clients; this
# block adds a transport, not a new auth path.
if [ "$ENABLE_WEBSOCKET" -eq 1 ]; then
  {
    echo ""
    echo "# WebSocket listener (added by --enable-websocket)."
    echo "# Browsers reach NATS via this listener; same JWT auth as TCP."
    echo "websocket {"
    echo "  port: ${WS_PORT}"
    if [ -n "$WS_TLS_CERT" ] && [ -n "$WS_TLS_KEY" ]; then
      echo "  tls {"
      echo "    cert_file: \"${WS_TLS_CERT}\""
      echo "    key_file:  \"${WS_TLS_KEY}\""
      echo "  }"
    else
      echo "  # Plain WS. The compose override binds this to 127.0.0.1 only;"
      echo "  # a reverse proxy (Caddy/nginx) MUST terminate TLS before this"
      echo "  # port is exposed to the network."
      echo "  no_tls: true"
    fi
    if [ -n "$WS_ALLOWED_ORIGINS" ]; then
      # Trim each token and skip empties so "a.com,,b.com" or a trailing
      # comma doesn't produce a stray "" entry in allowed_origins.
      origins_json=$(echo "$WS_ALLOWED_ORIGINS" | awk -F, '{
        out=""; first=1;
        for (i=1; i<=NF; i++) {
          tok = $i;
          gsub(/^[ \t]+|[ \t]+$/, "", tok);
          if (tok == "") continue;
          out = out (first ? "" : ", ") "\"" tok "\"";
          first = 0;
        }
        print out
      }')
      if [ -n "$origins_json" ]; then
        echo "  allowed_origins: [${origins_json}]"
      fi
    fi
    echo "  compression: true"
    echo "}"
  } >> "${OUTPUT_CONF}"
  echo ""
  echo "==> WebSocket listener enabled on port ${WS_PORT}"
  echo ""
  echo "    Bring up the compose stack with BOTH files and pass the WS port"
  echo "    as DC_NATS_WS_PORT so the host->container mapping matches the"
  echo "    listener port written into the config (they live in different"
  echo "    files and would otherwise drift silently):"
  echo ""
  echo "      DC_NATS_WS_PORT=${WS_PORT} \\"
  echo "        docker compose \\"
  echo "          -f infra/docker-compose-multitenant-nats.yml \\"
  echo "          -f infra/docker-compose-nats-websocket.yml up -d"
  echo ""
  echo "    To bind the host port somewhere other than 127.0.0.1, also set"
  echo "    DC_NATS_WS_BIND=10.0.0.5:${WS_PORT} (LAN only, never 0.0.0.0"
  echo "    without TLS termination in front)."
fi

echo ""
echo "============================================"
echo "  Deployment infrastructure ready!"
echo "============================================"
echo ""
echo "Generated files:"
echo "  NATS config:       ${OUTPUT_CONF}"
echo "  Registry creds:    ~/.device-connect/credentials/registry.creds.json"
echo "  Facilitator creds: ~/.device-connect/credentials/facilitator.creds.json"
echo ""
echo "Next steps:"
echo "  1. Create tenants:"
echo "     ./manage_tenants.sh create alpha --devices 5 --nats-host ${NATS_HOST}"
echo "     ./manage_tenants.sh create-batch alpha,beta,gamma --devices 5 --nats-host ${NATS_HOST}"
echo ""
echo "  2. Start infrastructure:"
echo "     DC_TENANTS=alpha,beta,gamma docker compose -f infra/docker-compose-multitenant-nats.yml up -d"
echo ""
echo "  3. Verify isolation:"
echo "     ./verify_tenants.sh --nats-host ${NATS_HOST}"
