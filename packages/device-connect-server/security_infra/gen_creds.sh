#!/usr/bin/env bash
#
# Generate NATS JWT credentials for Fabric services and devices.
#
# Usage:
#   ./gen_creds.sh --all --force          # Generate all built-in roles
#   ./gen_creds.sh --user devctl          # Generate a single user
#   ./gen_creds.sh --user robot-001       # Generate device credentials
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

ACCOUNT_NAME="FABRIC"
FORCE=false
ALL=false
USER_NAME=""

# Built-in roles
BUILT_IN_ROLES="devctl orchestrator registry"

usage() {
  echo "Usage: $0 [--all] [--force] [--user NAME]"
  echo ""
  echo "  --all       Generate credentials for all built-in roles (devctl, orchestrator, registry)"
  echo "  --force     Overwrite existing credentials"
  echo "  --user NAME Generate credentials for a specific user/device"
  exit 1
}

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)   ALL=true; shift ;;
    --force) FORCE=true; shift ;;
    --user)  USER_NAME="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done

if [ "$ALL" = false ] && [ -z "$USER_NAME" ]; then
  echo "Error: specify --all or --user NAME"
  usage
fi

mkdir -p "${CREDS_DIR}"

generate_creds() {
  local name="$1"
  local output="${CREDS_DIR}/${name}.creds.json"

  if [ -f "$output" ] && [ "$FORCE" = false ]; then
    echo "    [skip] ${output} already exists (use --force to overwrite)"
    return
  fi

  # Add user to the account
  nsc add user "${name}" --account "${ACCOUNT_NAME}" 2>/dev/null || true

  # Allow publish and subscribe on fabric subjects
  nsc edit user "${name}" \
    --account "${ACCOUNT_NAME}" \
    --allow-pub "fabric.>" \
    --allow-sub "fabric.>" \
    --allow-pub "_INBOX.>" \
    --allow-sub "_INBOX.>" 2>/dev/null || true

  # Export as .creds file
  local tmp_creds
  tmp_creds=$(mktemp)
  nsc generate creds --account "${ACCOUNT_NAME}" --name "${name}" > "${tmp_creds}" 2>/dev/null

  # Extract JWT and NKey seed, write as JSON
  local jwt seed
  jwt=$(sed -n '/-----BEGIN NATS USER JWT-----/,/------END NATS USER JWT------/p' "${tmp_creds}" | grep -v '[-][-][-]' | tr -d '\n')
  seed=$(sed -n '/-----BEGIN USER NKEY SEED-----/,/------END USER NKEY SEED------/p' "${tmp_creds}" | grep -v '[-][-][-]' | tr -d '\n')

  cat > "${output}" <<EOF
{
  "device_id": "${name}",
  "auth_type": "jwt",
  "tenant": "default",
  "nats": {
    "urls": ["nats://nats-jwt:4222"],
    "jwt": "${jwt}",
    "nkey_seed": "${seed}"
  }
}
EOF

  rm -f "${tmp_creds}"
  echo "    [ok] ${output}"
}

echo "==> Generating credentials"

if [ "$ALL" = true ]; then
  for role in $BUILT_IN_ROLES; do
    generate_creds "$role"
  done
else
  generate_creds "$USER_NAME"
fi

echo ""
echo "==> Credentials written to ${CREDS_DIR}/"
echo ""
echo "Files:"
ls -1 "${CREDS_DIR}"/*.creds.json 2>/dev/null || echo "    (none)"
