/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * dc/security.h -- transport security configuration (TLS + JWT/NKey).
 *
 * The wire contract is auth-agnostic (wire_contract.md sec 2.1); PR #153 adds
 * security as an additive, off-by-default layer. v1 supports transport-level
 * AuthN: the NATS C client connects with TLS and JWT/NKey credentials. This
 * struct collects that config from the SAME environment the Python SDK uses
 * (transport_layer/auth.py: NATS_JWT + NATS_NKEY_SEED, or
 * NATS_CREDENTIALS_FILE) plus explicit fields; transport_nats applies it.
 *
 * AuthZ / verifiable-mandate ENFORCE (Ed25519 + JCS + trust bundle) is a
 * later phase; the pre-dispatch hook seam for it already exists in dispatch.h.
 *
 * ASCII-only source (per CLAUDE.md).
 */

#ifndef DC_SECURITY_H
#define DC_SECURITY_H

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    char *creds_file; /* nsc-generated NATS .creds (JWT + seed) */
    char *jwt;        /* inline user JWT (alternative to creds_file) */
    char *nkey_seed;  /* inline NKey seed (paired with jwt) */
    char *tls_ca;     /* CA certificate path (PEM) */
    char *tls_cert;   /* client certificate path (PEM) */
    char *tls_key;    /* client private key path (PEM) */
    int tls_enable;   /* 1 to require TLS */
    int verify_hostname; /* default 1 (matches SDK build_tls_context) */
} dc_security;

/* Initialise to safe defaults (TLS off, verify_hostname on, all paths NULL). */
void dc_security_init(dc_security *s);

/*
 * Populate from environment, mirroring the SDK:
 *   NATS_CREDENTIALS_FILE -> creds_file
 *   NATS_JWT + NATS_NKEY_SEED -> jwt + nkey_seed
 *   MHP_TLS_CA / MHP_TLS_CERT / MHP_TLS_KEY -> tls_* (and tls_enable=1 if any)
 *   MHP_TLS_VERIFY_HOSTNAME=0 -> verify_hostname=0
 * Existing non-NULL fields are not overwritten.
 */
void dc_security_load_env(dc_security *s);

void dc_security_free(dc_security *s);

#ifdef __cplusplus
}
#endif

#endif /* DC_SECURITY_H */
