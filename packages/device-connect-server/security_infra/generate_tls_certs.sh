#!/usr/bin/env bash
# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

#
# Generate TLS certificates for Device Connect infrastructure.
#
# Supports both Zenoh and NATS backends with a shared CA.
# Certs are written to this directory (security_infra/) to match
# the docker-compose volume mount (../security_infra:/certs:ro).
#
# Usage:
#   ./generate_tls_certs.sh zenoh            # CA + Zenoh router + registry client
#   ./generate_tls_certs.sh nats             # CA + NATS server + registry client
#   ./generate_tls_certs.sh --client foo     # Device client cert (needs existing CA)
#   ./generate_tls_certs.sh zenoh --force    # Regenerate everything including CA
#   ./generate_tls_certs.sh -h               # Help
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERT_DIR="$SCRIPT_DIR"

# Certificate parameters
KEY_BITS=4096
DAYS=3650
ORG="/C=US/ST=State/L=City/O=Device-Connect/OU=Infrastructure"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

step()  { echo -e "${GREEN}==> ${NC}$1"; }
skip()  { echo -e "${YELLOW} ~  ${NC}$1"; }
header(){ echo -e "\n${CYAN}${BOLD}$1${NC}"; }

# ── Usage ───────────────────────────────────────────────────

usage() {
    cat <<EOF
Generate TLS certificates for Device Connect.

Usage:
  $(basename "$0") zenoh [--force]       CA + Zenoh router + registry client
  $(basename "$0") nats  [--force]       CA + NATS server + registry client
  $(basename "$0") --client <name>       Device client cert (CA must exist)
  $(basename "$0") -h | --help           Show this help

Options:
  --force    Regenerate all certs including CA
  --client   Generate a named client cert for a device

Files are written to: $CERT_DIR
EOF
    exit 0
}

# ── Argument parsing ────────────────────────────────────────

BACKEND=""
CLIENT_NAME=""
FORCE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        zenoh|nats)  BACKEND="$1"; shift ;;
        --force)     FORCE=true; shift ;;
        --client)    CLIENT_NAME="${2:?'--client requires a name'}"; shift 2 ;;
        -h|--help)   usage ;;
        *)           echo "Unknown argument: $1" >&2; usage ;;
    esac
done

if [[ -z "$BACKEND" && -z "$CLIENT_NAME" ]]; then
    echo "Error: specify a backend (zenoh or nats) or --client <name>" >&2
    echo ""
    usage
fi

# ── CA ──────────────────────────────────────────────────────

generate_ca() {
    if [[ -f "$CERT_DIR/ca.pem" && -f "$CERT_DIR/ca-key.pem" && "$FORCE" == false ]]; then
        skip "CA already exists (use --force to regenerate)"
        return
    fi
    step "Generating CA certificate..."
    openssl req -x509 -nodes -newkey "rsa:$KEY_BITS" \
        -keyout "$CERT_DIR/ca-key.pem" \
        -out "$CERT_DIR/ca.pem" \
        -days "$DAYS" \
        -subj "$ORG/CN=device-connect-ca" \
        2>/dev/null
    chmod 600 "$CERT_DIR/ca-key.pem"
    chmod 644 "$CERT_DIR/ca.pem"
}

# ── Server cert with SANs ──────────────────────────────────

generate_server_cert() {
    local name="$1"
    shift
    local san_entries=("$@")

    step "Generating server certificate: ${name}..."

    # Build SAN config
    local san_conf
    san_conf=$(mktemp)
    {
        echo "[req]"
        echo "distinguished_name = req_dn"
        echo "req_extensions = v3_req"
        echo "[req_dn]"
        echo "[v3_req]"
        echo "subjectAltName = @alt_names"
        echo "[alt_names]"
        local dns_i=1 ip_i=1
        for entry in "${san_entries[@]}"; do
            if [[ "$entry" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
                echo "IP.$ip_i = $entry"
                ((ip_i++))
            else
                echo "DNS.$dns_i = $entry"
                ((dns_i++))
            fi
        done
    } > "$san_conf"

    # Generate key + CSR
    openssl req -nodes -newkey "rsa:$KEY_BITS" \
        -keyout "$CERT_DIR/${name}-key.pem" \
        -out "$CERT_DIR/${name}.csr" \
        -subj "$ORG/CN=${name}" \
        2>/dev/null

    # Sign with CA
    openssl x509 -req \
        -in "$CERT_DIR/${name}.csr" \
        -CA "$CERT_DIR/ca.pem" \
        -CAkey "$CERT_DIR/ca-key.pem" \
        -CAcreateserial \
        -out "$CERT_DIR/${name}-cert.pem" \
        -days "$DAYS" \
        -extensions v3_req \
        -extfile "$san_conf" \
        2>/dev/null

    rm -f "$CERT_DIR/${name}.csr" "$san_conf"
    chmod 600 "$CERT_DIR/${name}-key.pem"
    chmod 644 "$CERT_DIR/${name}-cert.pem"
}

# ── Client cert ─────────────────────────────────────────────

generate_client_cert() {
    local name="$1"

    step "Generating client certificate: ${name}..."

    openssl req -nodes -newkey "rsa:$KEY_BITS" \
        -keyout "$CERT_DIR/${name}-key.pem" \
        -out "$CERT_DIR/${name}.csr" \
        -subj "$ORG/OU=Clients/CN=${name}" \
        2>/dev/null

    openssl x509 -req \
        -in "$CERT_DIR/${name}.csr" \
        -CA "$CERT_DIR/ca.pem" \
        -CAkey "$CERT_DIR/ca-key.pem" \
        -CAcreateserial \
        -out "$CERT_DIR/${name}-cert.pem" \
        -days "$DAYS" \
        2>/dev/null

    rm -f "$CERT_DIR/${name}.csr"
    chmod 600 "$CERT_DIR/${name}-key.pem"
    chmod 644 "$CERT_DIR/${name}-cert.pem"
}

# ── Summary helpers ─────────────────────────────────────────

print_files() {
    header "Generated files"
    echo ""
    for f in "$@"; do
        local base
        base=$(basename "$f")
        if [[ "$base" == *-key.pem || "$base" == ca-key.pem ]]; then
            printf "  %-30s %s\n" "$base" "(private key — keep secure)"
        else
            printf "  %-30s %s\n" "$base" ""
        fi
    done
}

print_zenoh_next_steps() {
    header "Next steps"
    cat <<EOF

  1. Start TLS infrastructure:
     cd device-connect-server
     docker compose -f infra/docker-compose.yml up -d

  2. Generate a device client cert:
     ./security_infra/generate_tls_certs.sh --client my-device

  3. Connect a device with TLS:
     ZENOH_CONNECT=tls/localhost:7447 \\
     MESSAGING_TLS_CA_FILE=security_infra/ca.pem \\
     MESSAGING_TLS_CERT_FILE=security_infra/my-device-cert.pem \\
     MESSAGING_TLS_KEY_FILE=security_infra/my-device-key.pem \\
     python my_device.py

EOF
}

print_nats_next_steps() {
    header "Next steps"
    cat <<EOF

  1. Enable TLS in NATS config:
     Uncomment the tls block in security_infra/nats-jwt-generated.conf
     and set cert paths to /certs/nats-server-cert.pem, etc.

  2. Start NATS with TLS:
     cd device-connect-server
     docker compose -f infra/docker-compose-nats.yml up -d

  3. Connect a device with TLS:
     NATS_URL=tls://localhost:4222 \\
     MESSAGING_TLS_CA_FILE=security_infra/ca.pem \\
     MESSAGING_TLS_CERT_FILE=security_infra/my-device-cert.pem \\
     MESSAGING_TLS_KEY_FILE=security_infra/my-device-key.pem \\
     python my_device.py

EOF
}

print_client_next_steps() {
    local name="$1"
    header "Next steps"
    cat <<EOF

  Connect device "${name}" with TLS:

  # Zenoh
  ZENOH_CONNECT=tls/localhost:7447 \\
  MESSAGING_TLS_CA_FILE=security_infra/ca.pem \\
  MESSAGING_TLS_CERT_FILE=security_infra/${name}-cert.pem \\
  MESSAGING_TLS_KEY_FILE=security_infra/${name}-key.pem \\
  python my_device.py

  # NATS
  NATS_URL=tls://localhost:4222 \\
  MESSAGING_TLS_CA_FILE=security_infra/ca.pem \\
  MESSAGING_TLS_CERT_FILE=security_infra/${name}-cert.pem \\
  MESSAGING_TLS_KEY_FILE=security_infra/${name}-key.pem \\
  python my_device.py

EOF
}

# ── Main ────────────────────────────────────────────────────

# --client mode: generate a single device cert
if [[ -n "$CLIENT_NAME" ]]; then
    if [[ ! -f "$CERT_DIR/ca.pem" || ! -f "$CERT_DIR/ca-key.pem" ]]; then
        echo "Error: CA not found. Run with 'zenoh' or 'nats' first to generate infrastructure certs." >&2
        exit 1
    fi
    header "Generating device client certificate"
    generate_client_cert "$CLIENT_NAME"
    print_files "$CERT_DIR/${CLIENT_NAME}-cert.pem" "$CERT_DIR/${CLIENT_NAME}-key.pem"
    print_client_next_steps "$CLIENT_NAME"
    exit 0
fi

# Backend mode: generate full infrastructure certs
header "Generating TLS certificates (${BACKEND})"

generate_ca

case "$BACKEND" in
    zenoh)
        generate_server_cert "zenoh" "zenoh" "localhost" "127.0.0.1"
        generate_client_cert "registry"
        print_files \
            "$CERT_DIR/ca.pem" "$CERT_DIR/ca-key.pem" \
            "$CERT_DIR/zenoh-cert.pem" "$CERT_DIR/zenoh-key.pem" \
            "$CERT_DIR/registry-cert.pem" "$CERT_DIR/registry-key.pem"
        print_zenoh_next_steps
        ;;
    nats)
        generate_server_cert "nats-server" "nats" "nats-jwt" "localhost" "127.0.0.1"
        generate_client_cert "registry"
        print_files \
            "$CERT_DIR/ca.pem" "$CERT_DIR/ca-key.pem" \
            "$CERT_DIR/nats-server-cert.pem" "$CERT_DIR/nats-server-key.pem" \
            "$CERT_DIR/registry-cert.pem" "$CERT_DIR/registry-key.pem"
        print_nats_next_steps
        ;;
esac
