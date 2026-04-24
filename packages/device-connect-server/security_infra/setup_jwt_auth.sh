#!/usr/bin/env bash
# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

#
# Setup NATS JWT authentication infrastructure using nsc.
#
# Usage:
#   ./setup_jwt_auth.sh dev     # Development setup (no TLS)
#   ./setup_jwt_auth.sh prod    # Production setup (TLS enabled)
#
# Environment:
#   DC_NSC_OPERATOR  — Operator name  (default: device-connect-operator)
#   DC_NSC_ACCOUNT   — Account name   (default: DEVICE_CONNECT)
#
# Prerequisites:
#   - nsc (brew install nsc OR go install github.com/nats-io/nsc/v2@latest)
#
# Outputs:
#   security_infra/nats-jwt-generated.conf   — NATS server config with JWT resolver
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV="${1:-dev}"

OPERATOR_NAME="${DC_NSC_OPERATOR:-device-connect-operator}"
ACCOUNT_NAME="${DC_NSC_ACCOUNT:-DEVICE_CONNECT}"
SYS_ACCOUNT_NAME="SYS"

NSC_HOME="${SCRIPT_DIR}/.nsc"
export NKEYS_PATH="${NSC_HOME}/nkeys"
export NSC_HOME

echo "==> Setting up NATS JWT auth (env=${ENV})"
echo "    NSC_HOME=${NSC_HOME}"
echo "    Operator=${OPERATOR_NAME}  Account=${ACCOUNT_NAME}"

# Clean previous state
rm -rf "${NSC_HOME}"
mkdir -p "${NSC_HOME}"

# nsc uses XDG_DATA_HOME or XDG_CONFIG_HOME; override to keep everything local
export XDG_DATA_HOME="${NSC_HOME}/data"
export XDG_CONFIG_HOME="${NSC_HOME}/config"

# Create operator
echo "==> Creating operator: ${OPERATOR_NAME}"
nsc add operator "${OPERATOR_NAME}" --sys

# Create the main account
echo "==> Creating account: ${ACCOUNT_NAME}"
nsc add account "${ACCOUNT_NAME}"

# Add a signing key to the account (used by gen_creds.sh)
echo "==> Adding signing key to ${ACCOUNT_NAME}"
nsc edit account "${ACCOUNT_NAME}" --sk generate

# Allow all subjects for the account (user-level JWTs restrict per-team)
nsc edit account "${ACCOUNT_NAME}" \
  --js-mem-storage -1 \
  --js-disk-storage -1 \
  --js-streams -1 \
  --js-consumer -1

# Generate the NATS server config with memory resolver (no push needed)
OUTPUT_CONF="${SCRIPT_DIR}/nats-jwt-generated.conf"
echo "==> Generating NATS server config: ${OUTPUT_CONF}"
rm -f "${OUTPUT_CONF}"

nsc generate config --mem-resolver --config-file "${OUTPUT_CONF}"

# Append monitoring, listen, and remote-access directives
cat >> "${OUTPUT_CONF}" <<EOF

# Device Connect additions
listen: 0.0.0.0:4222
http_port: 8222

EOF

if [ "${ENV}" = "prod" ]; then
  echo "==> Production mode: TLS config placeholder added"
  cat >> "${OUTPUT_CONF}" <<EOF
# TLS (fill in your cert paths)
# tls {
#   cert_file: "/certs/server-cert.pem"
#   key_file: "/certs/server-key.pem"
#   ca_file: "/certs/ca-cert.pem"
# }
EOF
fi

echo ""
echo "==> Done. NATS config written to:"
echo "    ${OUTPUT_CONF}"
echo ""
echo "Next: run ./gen_creds.sh --all --force to generate credentials."
