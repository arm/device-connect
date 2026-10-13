/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * dc/transport_nats.h -- NATS backend for the wire transport.
 *
 * Implemented against the NATS C client (cnats). The whole backend is compiled
 * only when DC_WITH_NATS is defined and cnats is available; otherwise the
 * constructor is a stub that returns -1 with a diagnostic, so the library and
 * the example still link (and fall back to the in-memory transport).
 *
 * NATS uses dotted subjects natively, so canonical subjects pass through
 * untranslated (wire_contract.md sec 2.3). TLS + JWT/NKey come from the
 * dc_security config (sec 2.1, PR #153 transport AuthN). The cnats
 * client owns intra-blip reconnect (unlimited, wait + jitter); the outer
 * exponential-backoff connect supervisor lives in the run loop (therm01).
 *
 * ASCII-only source (per CLAUDE.md).
 */

#ifndef DC_TRANSPORT_NATS_H
#define DC_TRANSPORT_NATS_H

#include "dc/security.h"
#include "dc/transport.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    const char *url;            /* e.g. "nats://127.0.0.1:4222" */
    dc_security *security; /* borrowed; may be NULL */
    int reconnect_wait_ms;      /* base reconnect wait; <=0 -> 250 */
    int reconnect_jitter_ms;    /* added jitter; <=0 -> 250 */
} dc_nats_config;

/*
 * Create + connect a NATS-backed transport. Returns 0 on success (fills `t`),
 * -1 on failure (including "built without NATS"). On failure `t` is untouched.
 */
int dc_transport_nats_create(dc_transport *t,
                                   const dc_nats_config *cfg);

/*
 * Poll for a reconnect that happened since the last poll. Returns 1 and writes
 * the prior outage duration (seconds) to *outage_s when a reconnect occurred,
 * else 0. The run loop calls this each tick and forwards to
 * dc_node_on_reconnect from the main thread (so node state is touched by
 * one thread only). Returns 0 for non-NATS transports.
 */
int dc_transport_nats_poll_reconnect(dc_transport *t,
                                           double *outage_s);

#ifdef __cplusplus
}
#endif

#endif /* DC_TRANSPORT_NATS_H */
