#!/usr/bin/env bash
#
# Verify multi-tenant isolation: test that per-tenant NATS JWT permissions work.
#
# Usage:
#   ./verify_tenants.sh --nats-host dc.example.com
#   ./verify_tenants.sh --nats-host localhost --tenants alpha,beta
#
# Prerequisites:
#   - nats CLI (https://github.com/nats-io/natscli)
#   - Deployment setup completed (setup_deployment.sh + manage_tenants.sh)
#   - NATS server running
#

set -euo pipefail

CREDS_DIR="${HOME}/.device-connect/credentials"
NATS_HOST="${NATS_HOST:-localhost}"
NATS_PORT="${NATS_PORT:-4222}"
TENANTS=""

PASS=0
FAIL=0

usage() {
  echo "Usage: $0 --nats-host HOST [--tenants alpha,beta]"
  echo ""
  echo "Options:"
  echo "  --nats-host HOST          NATS server hostname (required)"
  echo "  --nats-port PORT          NATS server port (default: 4222)"
  echo "  --tenants TENANT,TENANT   Tenants to test (default: auto-detect from credentials)"
  echo "  -h, --help                Show this help"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --nats-host)  NATS_HOST="$2"; shift 2 ;;
    --nats-port)  NATS_PORT="$2"; shift 2 ;;
    --tenants)    TENANTS="$2"; shift 2 ;;
    -h|--help)    usage ;;
    *)            echo "Unknown option: $1"; usage ;;
  esac
done

NATS_URL="nats://${NATS_HOST}:${NATS_PORT}"

# Check prerequisites
if ! command -v nats &>/dev/null; then
  echo "Error: nats CLI is not installed."
  echo "  Install: https://github.com/nats-io/natscli#installation"
  exit 1
fi

# Auto-detect tenants from credentials if not specified
if [ -z "$TENANTS" ]; then
  TENANTS=$(python3 -c "
import json, glob, sys
tenants = set()
for f in glob.glob('${CREDS_DIR}/*.creds.json'):
    try:
        t = json.load(open(f)).get('tenant', '')
        if t and t != 'default':
            tenants.add(t)
    except: pass
print(','.join(sorted(tenants)))
" 2>/dev/null)

  if [ -z "$TENANTS" ]; then
    echo "Error: No tenant credentials found. Run manage_tenants.sh first."
    exit 1
  fi
fi

IFS=',' read -ra TENANT_LIST <<< "$TENANTS"

echo "============================================"
echo "  Tenant Isolation Verification"
echo "============================================"
echo "  NATS:    ${NATS_URL}"
echo "  Tenants: ${TENANTS}"
echo ""

pass() {
  echo "  PASS: $1"
  PASS=$((PASS + 1))
}

fail() {
  echo "  FAIL: $1"
  FAIL=$((FAIL + 1))
}

# --- Test 1: NATS connectivity ---
echo "--- Test 1: NATS server health ---"
if nats server check connection -s "$NATS_URL" --creds "${CREDS_DIR}/registry.creds.json" 2>/dev/null; then
  pass "NATS server is reachable"
else
  fail "Cannot connect to NATS at ${NATS_URL}"
  echo ""
  echo "Cannot proceed without NATS connectivity. Is the server running?"
  exit 1
fi
echo ""

# --- Test 2: Registry (privileged) can publish to any tenant ---
echo "--- Test 2: Privileged credentials (registry) ---"
REGISTRY_CREDS="${CREDS_DIR}/registry.creds.json"
if [ ! -f "$REGISTRY_CREDS" ]; then
  fail "Registry credentials not found at ${REGISTRY_CREDS}"
else
  for tenant in "${TENANT_LIST[@]}"; do
    tenant=$(echo "$tenant" | xargs)
    if nats pub "device-connect.${tenant}.verify.test" "registry-check" \
        -s "$NATS_URL" --creds "$REGISTRY_CREDS" 2>/dev/null; then
      pass "Registry can publish to tenant '${tenant}'"
    else
      fail "Registry cannot publish to tenant '${tenant}'"
    fi
  done
fi
echo ""

# --- Test 3: Tenant credentials can publish to own tenant ---
echo "--- Test 3: Same-tenant publish (should succeed) ---"
for tenant in "${TENANT_LIST[@]}"; do
  tenant=$(echo "$tenant" | xargs)
  # Find the first creds file for this tenant
  creds_file=$(python3 -c "
import json, glob
for f in sorted(glob.glob('${CREDS_DIR}/*.creds.json')):
    try:
        d = json.load(open(f))
        if d.get('tenant') == '${tenant}':
            print(f)
            break
    except: pass
" 2>/dev/null)

  if [ -z "$creds_file" ]; then
    fail "No credentials found for tenant '${tenant}'"
    continue
  fi

  if nats pub "device-connect.${tenant}.verify.own-tenant" "hello" \
      -s "$NATS_URL" --creds "$creds_file" 2>/dev/null; then
    pass "Tenant '${tenant}' can publish to own namespace"
  else
    fail "Tenant '${tenant}' cannot publish to own namespace"
  fi
done
echo ""

# --- Test 4: Tenant credentials CANNOT publish to other tenant ---
echo "--- Test 4: Cross-tenant publish (should be denied) ---"
if [ ${#TENANT_LIST[@]} -lt 2 ]; then
  echo "  [skip] Need at least 2 tenants for cross-tenant test"
else
  for i in "${!TENANT_LIST[@]}"; do
    src_tenant=$(echo "${TENANT_LIST[$i]}" | xargs)
    # Pick the next tenant as target (wrap around)
    next_idx=$(( (i + 1) % ${#TENANT_LIST[@]} ))
    dst_tenant=$(echo "${TENANT_LIST[$next_idx]}" | xargs)

    creds_file=$(python3 -c "
import json, glob
for f in sorted(glob.glob('${CREDS_DIR}/*.creds.json')):
    try:
        d = json.load(open(f))
        if d.get('tenant') == '${src_tenant}':
            print(f)
            break
    except: pass
" 2>/dev/null)

    if [ -z "$creds_file" ]; then
      fail "No credentials found for tenant '${src_tenant}'"
      continue
    fi

    # Attempt to publish to another tenant's subject — this should fail
    if nats pub "device-connect.${dst_tenant}.verify.cross-tenant" "intruder" \
        -s "$NATS_URL" --creds "$creds_file" 2>&1 | grep -qi "permission"; then
      pass "Tenant '${src_tenant}' correctly DENIED publish to '${dst_tenant}'"
    else
      # nats pub may succeed silently even if permissions violation occurs
      # (NATS disconnects the client). Check if we got an error.
      result=$(nats pub "device-connect.${dst_tenant}.verify.cross-tenant2" "intruder" \
        -s "$NATS_URL" --creds "$creds_file" 2>&1 || true)
      if echo "$result" | grep -qi "permission\|authorization\|disconnect\|error"; then
        pass "Tenant '${src_tenant}' correctly DENIED publish to '${dst_tenant}'"
      else
        fail "Tenant '${src_tenant}' was NOT denied publish to '${dst_tenant}' (ISOLATION BREACH)"
      fi
    fi
  done
fi
echo ""

# --- Test 5: Cross-tenant subscribe (should be denied) ---
echo "--- Test 5: Cross-tenant subscribe (should be denied) ---"
if [ ${#TENANT_LIST[@]} -lt 2 ]; then
  echo "  [skip] Need at least 2 tenants for cross-tenant test"
else
  src_tenant=$(echo "${TENANT_LIST[0]}" | xargs)
  dst_tenant=$(echo "${TENANT_LIST[1]}" | xargs)

  creds_file=$(python3 -c "
import json, glob
for f in sorted(glob.glob('${CREDS_DIR}/*.creds.json')):
    try:
        d = json.load(open(f))
        if d.get('tenant') == '${src_tenant}':
            print(f)
            break
    except: pass
" 2>/dev/null)

  if [ -n "$creds_file" ]; then
    # Try to subscribe with a short timeout
    result=$(timeout 3 nats sub "device-connect.${dst_tenant}.>" \
      -s "$NATS_URL" --creds "$creds_file" 2>&1 || true)
    if echo "$result" | grep -qi "permission\|authorization\|disconnect\|error"; then
      pass "Tenant '${src_tenant}' correctly DENIED subscribe to '${dst_tenant}.>'"
    else
      fail "Tenant '${src_tenant}' was NOT denied subscribe to '${dst_tenant}.>' (ISOLATION BREACH)"
    fi
  fi
fi
echo ""

# --- Summary ---
TOTAL=$((PASS + FAIL))
echo "============================================"
echo "  Results: ${PASS}/${TOTAL} passed, ${FAIL} failed"
echo "============================================"

if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo "  WARNING: Some isolation tests failed!"
  echo "  Review the FAIL lines above."
  exit 1
else
  echo ""
  echo "  All tests passed. Tenant isolation is working correctly."
  exit 0
fi
