#!/usr/bin/env bash
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

usage() {
  echo "Usage: $0 --nats-host HOST [--nats-port PORT]"
  echo ""
  echo "One-time bootstrap for multi-tenant NATS JWT infrastructure."
  echo "Creates the NATS operator/account and privileged credentials"
  echo "(registry, facilitator)."
  echo ""
  echo "Options:"
  echo "  --nats-host HOST   Public hostname or IP of the NATS server (required)"
  echo "  --nats-port PORT   NATS port (default: 4222)"
  echo "  -h, --help         Show this help"
  echo ""
  echo "After running this, use manage_tenants.sh to create tenants."
  exit 1
}

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --nats-host)  NATS_HOST="$2"; shift 2 ;;
    --nats-port)  NATS_PORT="$2"; shift 2 ;;
    -h|--help)    usage ;;
    *)            echo "Unknown option: $1"; usage ;;
  esac
done

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
nsc generate config --mem-resolver --config-file "${OUTPUT_CONF}"

# Re-append listen directives (nsc generate overwrites the file)
cat >> "${OUTPUT_CONF}" <<EOF

# Device Connect additions
listen: 0.0.0.0:4222
http_port: 8222
EOF

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
echo "     DC_TENANTS=alpha,beta,gamma docker compose -f infra/docker-compose-multitenant.yml up -d"
echo ""
echo "  3. Verify isolation:"
echo "     ./verify_tenants.sh --nats-host ${NATS_HOST}"
