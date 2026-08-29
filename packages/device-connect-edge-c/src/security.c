/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * dc/security.c -- transport security config loading.
 *
 * ASCII-only source (per CLAUDE.md).
 */

#include "dc/security.h"

#include <stdlib.h>
#include <string.h>

void dc_security_init(dc_security *s) {
    if (s == NULL) {
        return;
    }
    memset(s, 0, sizeof(*s));
    s->verify_hostname = 1;
}

static char *dup_env(const char *name) {
    const char *v = getenv(name);
    if (v == NULL || v[0] == '\0') {
        return NULL;
    }
    return strdup(v);
}

void dc_security_load_env(dc_security *s) {
    if (s == NULL) {
        return;
    }
    if (s->creds_file == NULL) {
        s->creds_file = dup_env("NATS_CREDENTIALS_FILE");
    }
    if (s->jwt == NULL) {
        s->jwt = dup_env("NATS_JWT");
    }
    if (s->nkey_seed == NULL) {
        s->nkey_seed = dup_env("NATS_NKEY_SEED");
    }
    if (s->tls_ca == NULL) {
        s->tls_ca = dup_env("MHP_TLS_CA");
    }
    if (s->tls_cert == NULL) {
        s->tls_cert = dup_env("MHP_TLS_CERT");
    }
    if (s->tls_key == NULL) {
        s->tls_key = dup_env("MHP_TLS_KEY");
    }
    if (s->tls_ca != NULL || s->tls_cert != NULL || s->tls_key != NULL) {
        s->tls_enable = 1;
    }
    const char *vh = getenv("MHP_TLS_VERIFY_HOSTNAME");
    if (vh != NULL && strcmp(vh, "0") == 0) {
        s->verify_hostname = 0;
    }
}

void dc_security_free(dc_security *s) {
    if (s == NULL) {
        return;
    }
    free(s->creds_file);
    free(s->jwt);
    free(s->nkey_seed);
    free(s->tls_ca);
    free(s->tls_cert);
    free(s->tls_key);
    memset(s, 0, sizeof(*s));
    s->verify_hostname = 1;
}
