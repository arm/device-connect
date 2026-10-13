/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * dc/transport.h -- the transport abstraction the node runs on.
 *
 * The wire contract is identical across transports (wire_contract.md sec 2.1);
 * only the broker connection and subject-syntax translation differ. The node
 * talks to this vtable; concrete backends (NATS in v1; MQTT/Zenoh/ZMQ later)
 * implement it. An in-memory loopback backend (transport_mem) implements it for
 * tests and for builds without a broker client.
 *
 * Subjects passed across this interface are in CANONICAL dotted form; the
 * backend translates to native syntax at its boundary (sec 2.3).
 *
 * ASCII-only source (per CLAUDE.md).
 */

#ifndef DC_TRANSPORT_H
#define DC_TRANSPORT_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Delivery callback for a subscription. `reply` is the reply subject for a
 * request-style message (NULL for fire-and-forget). Data aliases backend
 * memory valid only for the duration of the call.
 */
typedef void (*dc_msg_cb)(void *user, const char *subject,
                                const uint8_t *data, size_t len,
                                const char *reply);

typedef struct dc_transport dc_transport;

struct dc_transport {
    void *impl;

    /* fire-and-forget publish to a canonical subject. 0 ok, -1 fail. */
    int (*publish)(void *impl, const char *subject, const uint8_t *data,
                   size_t len);

    /* subscribe a callback to a canonical subject/pattern. 0 ok, -1 fail. */
    int (*subscribe)(void *impl, const char *pattern, dc_msg_cb cb,
                     void *user);

    /*
     * request/reply: publish to `subject` and block for one reply up to
     * timeout_ms. On success returns 0 and a freshly malloc'd reply buffer in
     * *out (caller frees) with length *out_len. -1 on timeout/error.
     */
    int (*request)(void *impl, const char *subject, const uint8_t *data,
                   size_t len, uint8_t **out, size_t *out_len, int timeout_ms);

    /* publish a reply to a request's reply subject. 0 ok, -1 fail. */
    int (*respond)(void *impl, const char *reply, const uint8_t *data,
                   size_t len);

    /* 1 if currently connected to the broker, else 0. */
    int (*connected)(void *impl);

    /* close + free the backend. */
    void (*close)(void *impl);
};

/* Wall-clock seconds (Unix epoch, sub-second). Defined in runtime.c;
 * used by the NATS backend's reconnect bookkeeping. */
double dc_now(void);

#ifdef __cplusplus
}
#endif

#endif /* DC_TRANSPORT_H */
