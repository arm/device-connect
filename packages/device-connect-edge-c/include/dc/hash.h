/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * dc/hash.h -- a small, dependency-free SHA-256 for the manifest hash.
 *
 * wire_contract.md sec 7.6 requires the manifest_hash to be a deterministic,
 * stable, cryptographic-strength change-detection token; it explicitly does
 * NOT require cross-implementation byte-identical hashing (only the
 * originating announcer's hashes are ever compared). We therefore use plain
 * SHA-256 over the canonical JSON rather than the Python SDK's BLAKE2b -- no
 * external crypto dependency, full collision resistance.
 *
 * ASCII-only source (per CLAUDE.md).
 */

#ifndef DC_HASH_H
#define DC_HASH_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define DC_SHA256_DIGEST 32
#define DC_SHA256_HEXLEN 64 /* not counting the NUL */

void dc_sha256(const void *data, size_t len,
                     uint8_t digest[DC_SHA256_DIGEST]);

/* lowercase-hex encode n bytes into out (out must hold 2*n + 1 bytes). */
void dc_hex_encode(const uint8_t *bytes, size_t n, char *out);

/*
 * Convenience: SHA-256 of data, lowercase-hex, freshly malloc'd
 * (DC_SHA256_HEXLEN + 1 bytes). Caller frees. NULL on OOM.
 */
char *dc_sha256_hex(const void *data, size_t len);

#ifdef __cplusplus
}
#endif

#endif /* DC_HASH_H */
