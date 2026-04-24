#!/usr/bin/env bash
# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

#
# Generate NATS JWT credentials for Device Connect services and devices.
#
# Usage:
#   ./gen_creds.sh --all --force                        # Generate all built-in privileged roles
#   ./gen_creds.sh --privileged --user registry          # Single privileged user (all tenants)
#   ./gen_creds.sh --user robot-001                      # Device creds (default tenant, legacy)
#   ./gen_creds.sh --tenant alpha --user robot-001       # Tenant-scoped device creds
#   ./gen_creds.sh --nats-host dc.example.com ...        # Custom NATS host in creds file
#
# Prerequisites:
#   - nsc (brew install nsc OR go install github.com/nats-io/nsc/v2@latest)
#   - Run setup_jwt_auth.sh first
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CREDS_DIR="${HOME}/.device-connect/credentials"

NSC_HOME="${SCRIPT_DIR}/.nsc"
export NKEYS_PATH="${NSC_HOME}/nkeys"
export NSC_HOME
export XDG_DATA_HOME="${NSC_HOME}/data"
export XDG_CONFIG_HOME="${NSC_HOME}/config"

ACCOUNT_NAME="${DC_NSC_ACCOUNT:-DEVICE_CONNECT}"
FORCE=false
ALL=false
USER_NAME=""
TENANT_SCOPE=""
PRIVILEGED=false
NATS_HOST="${NATS_HOST:-nats-jwt}"
NATS_PORT="${NATS_PORT:-4222}"

# Built-in privileged roles (have access to all tenants)
BUILT_IN_ROLES="devctl orchestrator registry"

usage() {
  echo "Usage: $0 [OPTIONS]"
  echo ""
  echo "Options:"
  echo "  --all              Generate credentials for all built-in privileged roles"
  echo "  --user NAME        Generate credentials for a specific user/device"
  echo "  --tenant TENANT    Scope credentials to a tenant (subjects: device-connect.TENANT.>)"
  echo "  --privileged       Generate privileged credentials (all tenants: device-connect.>)"
  echo "  --nats-host HOST   NATS server hostname for creds file (default: nats-jwt)"
  echo "  --nats-port PORT   NATS server port for creds file (default: 4222)"
  echo "  --force            Overwrite existing credentials"
  echo "  -h, --help         Show this help"
  exit 1
}

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)        ALL=true; shift ;;
    --force)      FORCE=true; shift ;;
    --user)       USER_NAME="$2"; shift 2 ;;
    --tenant)     TENANT_SCOPE="$2"; shift 2 ;;
    --privileged) PRIVILEGED=true; shift ;;
    --nats-host)  NATS_HOST="$2"; shift 2 ;;
    --nats-port)  NATS_PORT="$2"; shift 2 ;;
    -h|--help)    usage ;;
    *)            echo "Unknown option: $1"; usage ;;
  esac
done

if [ "$ALL" = false ] && [ -z "$USER_NAME" ]; then
  echo "Error: specify --all or --user NAME"
  usage
fi

# --all implies --privileged (built-in roles need all-tenant access)
if [ "$ALL" = true ]; then
  PRIVILEGED=true
fi

mkdir -p "${CREDS_DIR}"

generate_creds() {
  local name="$1"
  local tenant="$2"      # empty string for privileged/legacy
  local privileged="$3"  # "true" or "false"
  local output="${CREDS_DIR}/${name}.creds.json"

  if [ -f "$output" ] && [ "$FORCE" = false ]; then
    echo "    [skip] ${output} already exists (use --force to overwrite)"
    return
  fi

  # Add user to the account
  nsc add user "${name}" --account "${ACCOUNT_NAME}" 2>/dev/null || true

  # Set subject permissions based on scope
  if [ "$privileged" = "true" ]; then
    # Privileged: access to all tenants
    nsc edit user "${name}" \
      --account "${ACCOUNT_NAME}" \
      --allow-pub "device-connect.>" \
      --allow-sub "device-connect.>" \
      --allow-pub "_INBOX.>" \
      --allow-sub "_INBOX.>" 2>/dev/null || true
  elif [ -n "$tenant" ]; then
    # Tenant-scoped: access only to this tenant
    nsc edit user "${name}" \
      --account "${ACCOUNT_NAME}" \
      --allow-pub "device-connect.${tenant}.>" \
      --allow-sub "device-connect.${tenant}.>" \
      --allow-pub "_INBOX.>" \
      --allow-sub "_INBOX.>" 2>/dev/null || true
  else
    # Legacy: default tenant access
    nsc edit user "${name}" \
      --account "${ACCOUNT_NAME}" \
      --allow-pub "device-connect.>" \
      --allow-sub "device-connect.>" \
      --allow-pub "_INBOX.>" \
      --allow-sub "_INBOX.>" 2>/dev/null || true
  fi

  # Export as .creds file
  local tmp_creds
  tmp_creds=$(mktemp)
  nsc generate creds --account "${ACCOUNT_NAME}" --name "${name}" > "${tmp_creds}" 2>/dev/null

  # Extract JWT and NKey seed, write as JSON
  local jwt seed tenant_value
  jwt=$(sed -n '/-----BEGIN NATS USER JWT-----/,/------END NATS USER JWT------/p' "${tmp_creds}" | grep -v '[-][-][-]' | tr -d '\n')
  seed=$(sed -n '/-----BEGIN USER NKEY SEED-----/,/------END USER NKEY SEED------/p' "${tmp_creds}" | grep -v '[-][-][-]' | tr -d '\n')

  # Determine tenant value for the creds file
  if [ -n "$tenant" ]; then
    tenant_value="$tenant"
  else
    tenant_value="default"
  fi

  cat > "${output}" <<EOF
{
  "device_id": "${name}",
  "auth_type": "jwt",
  "tenant": "${tenant_value}",
  "nats": {
    "urls": ["nats://${NATS_HOST}:${NATS_PORT}"],
    "jwt": "${jwt}",
    "nkey_seed": "${seed}"
  }
}
EOF

  rm -f "${tmp_creds}"
  echo "    [ok] ${output} (tenant=${tenant_value})"
}

echo "==> Generating credentials (account=${ACCOUNT_NAME})"

if [ "$ALL" = true ]; then
  for role in $BUILT_IN_ROLES; do
    generate_creds "$role" "" "true"
  done
else
  generate_creds "$USER_NAME" "$TENANT_SCOPE" "$PRIVILEGED"
fi

echo ""
echo "==> Credentials written to ${CREDS_DIR}/"
echo ""
echo "Files:"
ls -1 "${CREDS_DIR}"/*.creds.json 2>/dev/null || echo "    (none)"
