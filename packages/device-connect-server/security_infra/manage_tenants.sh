#!/usr/bin/env bash
#
# Manage tenants and device tokens for multi-tenant Device Connect deployments.
#
# Usage:
#   ./manage_tenants.sh create alpha --devices 5 --nats-host dc.example.com
#   ./manage_tenants.sh create-batch alpha,beta,gamma --devices 5 --nats-host dc.example.com
#   ./manage_tenants.sh add-device alpha my-robot --nats-host dc.example.com
#   ./manage_tenants.sh list
#   ./manage_tenants.sh reload-nats [--container dc-nats]
#
# Prerequisites:
#   - nsc (installed and setup_deployment.sh already run)
#   - For reload-nats: docker CLI access
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CREDS_DIR="${HOME}/.device-connect/credentials"
BUNDLES_DIR="${SCRIPT_DIR}/tenant-bundles"

NSC_HOME="${SCRIPT_DIR}/.nsc"
export NKEYS_PATH="${NSC_HOME}/nkeys"
export NSC_HOME
export XDG_DATA_HOME="${NSC_HOME}/data"
export XDG_CONFIG_HOME="${NSC_HOME}/config"

NATS_HOST="${NATS_HOST:-localhost}"
NATS_PORT="${NATS_PORT:-4222}"
NATS_CONTAINER="dc-nats"
NUM_DEVICES=5
FORCE=false

usage() {
  echo "Usage: $0 COMMAND [OPTIONS]"
  echo ""
  echo "Commands:"
  echo "  create       TENANT          Create a tenant with device tokens"
  echo "  create-batch TENANT,TENANT,. Create multiple tenants at once"
  echo "  add-device   TENANT NAME     Add a device token to an existing tenant"
  echo "  list                         List all tenants and their credentials"
  echo "  reload-nats                  Regenerate NATS config and signal reload"
  echo ""
  echo "Options:"
  echo "  --devices N               Devices per tenant (default: 5)"
  echo "  --nats-host HOST          NATS hostname for creds (default: localhost)"
  echo "  --nats-port PORT          NATS port for creds (default: 4222)"
  echo "  --container NAME          NATS Docker container name (default: dc-nats)"
  echo "  --force                   Overwrite existing credentials"
  echo "  -h, --help                Show this help"
  exit 1
}

# --- Helper functions ---

regenerate_nats_config() {
  local output="${SCRIPT_DIR}/nats-jwt-generated.conf"
  nsc generate config --mem-resolver --config-file "${output}" 2>/dev/null
  cat >> "${output}" <<EOF

# Device Connect additions
listen: 0.0.0.0:4222
http_port: 8222
EOF
  echo "    NATS config regenerated: ${output}"
}

build_tenant_bundle() {
  local tenant="$1"
  local tenant_dir="${BUNDLES_DIR}/${tenant}"

  mkdir -p "${tenant_dir}/credentials"

  # Copy tenant credentials into the bundle
  for f in "${CREDS_DIR}"/*.creds.json; do
    [ -f "$f" ] || continue
    # Check if this creds file belongs to the tenant
    local file_tenant
    file_tenant=$(python3 -c "import json,sys; print(json.load(open('$f')).get('tenant',''))" 2>/dev/null || echo "")
    if [ "$file_tenant" = "$tenant" ]; then
      cp "$f" "${tenant_dir}/credentials/"
    fi
  done

  # Generate tenant-config.env
  cat > "${tenant_dir}/tenant-config.env" <<EOF
# Device Connect — Tenant: ${tenant}
# Source this file: source tenant-config.env

export TENANT=${tenant}
export NATS_URL=nats://${NATS_HOST}:${NATS_PORT}
export MESSAGING_BACKEND=nats

# Set this to the credentials file for your device:
# export NATS_CREDENTIALS_FILE=./credentials/${tenant}-device-001.creds.json
EOF

  # Create zip
  (cd "${BUNDLES_DIR}" && zip -qr "${tenant}.zip" "${tenant}/")
  echo "    Bundle: ${BUNDLES_DIR}/${tenant}.zip"
}

create_tenant() {
  local tenant="$1"
  local num_devices="$2"

  echo "==> Creating tenant: ${tenant} (${num_devices} devices)"

  local force_flag=""
  if [ "$FORCE" = true ]; then
    force_flag="--force"
  fi

  for i in $(seq 1 "$num_devices"); do
    local device_name
    device_name="${tenant}-device-$(printf "%03d" "$i")"
    "${SCRIPT_DIR}/gen_creds.sh" \
      --tenant "$tenant" \
      --user "$device_name" \
      --nats-host "$NATS_HOST" \
      --nats-port "$NATS_PORT" \
      $force_flag
  done

  # Build the tenant bundle
  build_tenant_bundle "$tenant"

  echo "    Tenant ${tenant}: ${num_devices} device credentials created"
}

# --- Command dispatch ---

if [ $# -lt 1 ]; then
  usage
fi

COMMAND="$1"
shift

case "$COMMAND" in
  create)
    if [ $# -lt 1 ]; then
      echo "Error: create requires a tenant name"
      usage
    fi
    TENANT_NAME="$1"
    shift

    # Parse remaining options
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --devices)    NUM_DEVICES="$2"; shift 2 ;;
        --nats-host)  NATS_HOST="$2"; shift 2 ;;
        --nats-port)  NATS_PORT="$2"; shift 2 ;;
        --force)      FORCE=true; shift ;;
        *)            echo "Unknown option: $1"; usage ;;
      esac
    done

    create_tenant "$TENANT_NAME" "$NUM_DEVICES"
    regenerate_nats_config
    echo ""
    echo "Done. If NATS is already running, reload with:"
    echo "  $0 reload-nats"
    ;;

  create-batch)
    if [ $# -lt 1 ]; then
      echo "Error: create-batch requires comma-separated tenant names"
      usage
    fi
    TENANTS_CSV="$1"
    shift

    # Parse remaining options
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --devices)    NUM_DEVICES="$2"; shift 2 ;;
        --nats-host)  NATS_HOST="$2"; shift 2 ;;
        --nats-port)  NATS_PORT="$2"; shift 2 ;;
        --force)      FORCE=true; shift ;;
        *)            echo "Unknown option: $1"; usage ;;
      esac
    done

    IFS=',' read -ra TENANTS <<< "$TENANTS_CSV"
    for tenant in "${TENANTS[@]}"; do
      tenant=$(echo "$tenant" | xargs)  # trim whitespace
      create_tenant "$tenant" "$NUM_DEVICES"
      echo ""
    done

    regenerate_nats_config
    echo ""
    echo "Created ${#TENANTS[@]} tenants. If NATS is already running, reload with:"
    echo "  $0 reload-nats"
    echo ""
    echo "Tenant bundles ready for distribution:"
    ls -1 "${BUNDLES_DIR}"/*.zip 2>/dev/null || echo "    (none)"
    echo ""
    echo "Tenants list for docker-compose DC_TENANTS env var:"
    echo "  DC_TENANTS=${TENANTS_CSV}"
    ;;

  add-device)
    if [ $# -lt 2 ]; then
      echo "Error: add-device requires TENANT and DEVICE_NAME"
      echo "Usage: $0 add-device TENANT DEVICE_NAME [--nats-host HOST]"
      exit 1
    fi
    TENANT_NAME="$1"
    DEVICE_NAME="$2"
    shift 2

    while [[ $# -gt 0 ]]; do
      case "$1" in
        --nats-host)  NATS_HOST="$2"; shift 2 ;;
        --nats-port)  NATS_PORT="$2"; shift 2 ;;
        --force)      FORCE=true; shift ;;
        *)            echo "Unknown option: $1"; usage ;;
      esac
    done

    echo "==> Adding device ${DEVICE_NAME} to tenant ${TENANT_NAME}"

    local_force=""
    if [ "$FORCE" = true ]; then
      local_force="--force"
    fi

    "${SCRIPT_DIR}/gen_creds.sh" \
      --tenant "$TENANT_NAME" \
      --user "$DEVICE_NAME" \
      --nats-host "$NATS_HOST" \
      --nats-port "$NATS_PORT" \
      $local_force

    # Rebuild the tenant bundle
    build_tenant_bundle "$TENANT_NAME"
    regenerate_nats_config
    echo ""
    echo "Done. If NATS is already running, reload with:"
    echo "  $0 reload-nats"
    ;;

  list)
    echo "==> Tenants and credentials"
    echo ""

    if [ ! -d "$CREDS_DIR" ]; then
      echo "No credentials found at ${CREDS_DIR}"
      exit 0
    fi

    # Group credentials by tenant
    declare -A tenant_devices
    declare -A tenant_count

    for f in "${CREDS_DIR}"/*.creds.json; do
      [ -f "$f" ] || continue
      local_tenant=$(python3 -c "import json,sys; print(json.load(open('$f')).get('tenant',''))" 2>/dev/null || echo "unknown")
      local_device=$(python3 -c "import json,sys; print(json.load(open('$f')).get('device_id',''))" 2>/dev/null || echo "unknown")

      if [ "$local_tenant" = "default" ]; then
        # Privileged roles (registry, facilitator, etc.)
        echo "  [privileged] ${local_device} — $(basename "$f")"
      else
        tenant_devices["$local_tenant"]+="    ${local_device} — $(basename "$f")"$'\n'
        tenant_count["$local_tenant"]=$(( ${tenant_count["$local_tenant"]:-0} + 1 ))
      fi
    done

    echo ""
    for tenant in $(echo "${!tenant_count[@]}" | tr ' ' '\n' | sort); do
      echo "  Tenant: ${tenant} (${tenant_count[$tenant]} devices)"
      echo "${tenant_devices[$tenant]}"
    done

    # Show bundles if they exist
    if [ -d "$BUNDLES_DIR" ]; then
      echo "  Bundles:"
      ls -1 "${BUNDLES_DIR}"/*.zip 2>/dev/null | while read -r z; do
        echo "    $(basename "$z")"
      done
    fi
    ;;

  reload-nats)
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --container)  NATS_CONTAINER="$2"; shift 2 ;;
        *)            echo "Unknown option: $1"; usage ;;
      esac
    done

    echo "==> Regenerating NATS config and signaling reload"
    regenerate_nats_config

    if docker ps --format '{{.Names}}' | grep -q "^${NATS_CONTAINER}$"; then
      docker kill --signal=SIGHUP "${NATS_CONTAINER}"
      echo "    Sent SIGHUP to ${NATS_CONTAINER} — config reloaded"
    else
      echo "    Container '${NATS_CONTAINER}' not running. Start it with:"
      echo "    docker compose -f infra/docker-compose-multitenant.yml up -d"
    fi
    ;;

  -h|--help)
    usage
    ;;

  *)
    echo "Unknown command: ${COMMAND}"
    usage
    ;;
esac
