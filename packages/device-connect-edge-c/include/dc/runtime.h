/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * dc/runtime.h -- the Device Connect C edge SDK runtime.
 *
 * Connects a driver to a NATS broker, registers it with the portal registry,
 * serves inbound commands on device-connect.{tenant}.{id}.cmd, keeps the lease
 * alive with a heartbeat, answers requestRegistration pulls, and exposes
 * device-to-device invoke_remote and event emission.
 *
 * Credentials: accepts the portal's native *.creds.json (nats.jwt +
 * nats.nkey_seed, with device_id/tenant) or an nsc-chained *.creds; device_id
 * and tenant are auto-detected from a *.creds.json when not supplied.
 *
 * ASCII-only source.
 */

#ifndef DC_RUNTIME_H
#define DC_RUNTIME_H

#include "dc/driver.h"
#include "dc/json.h"

#ifdef __cplusplus
extern "C" {
#endif

#define DC_DEFAULT_DEVICE_TTL 30

typedef struct {
    const char *server;     /* nats URL; NULL -> $NATS_URL */
    const char *creds_file; /* .creds.json or chained .creds; NULL -> $NATS_CREDENTIALS_FILE */
    const char *device_id;  /* NULL -> from creds.json */
    const char *tenant;     /* NULL -> from creds.json, else "default" */
    int device_ttl;         /* <=0 -> DC_DEFAULT_DEVICE_TTL */
} dc_runtime_config;

typedef struct dc_runtime dc_runtime;

dc_runtime *dc_runtime_new(dc_driver *driver, const dc_runtime_config *cfg);
void dc_runtime_free(dc_runtime *r);

/* Connect, subscribe the cmd subject, register with the registry, and publish
 * the first heartbeat. Returns 0 on success. */
int dc_runtime_start(dc_runtime *r);

/* Drive periodic work (heartbeat + reconnect re-register). Call frequently. */
void dc_runtime_tick(dc_runtime *r, double now);

/* Blocking run loop until SIGINT/SIGTERM; calls tick internally. */
void dc_runtime_run(dc_runtime *r);

/* Graceful shutdown: announce departure and disconnect. */
void dc_runtime_stop(dc_runtime *r);

/*
 * Device-to-device RPC: call `function` on `device_id` in the same tenant.
 * Takes ownership of `params`. Returns the parsed JSON-RPC reply object
 * (caller json_free) or NULL on transport failure. Check for an "error" key
 * before reading "result".
 */
json *dc_runtime_invoke_remote(dc_runtime *r, const char *device_id,
                               const char *function, json *params,
                               int timeout_ms);

/* Emit an event: publish to device-connect.{tenant}.{id}.event.{name}.
 * Takes ownership of `params`. */
void dc_runtime_emit(dc_runtime *r, const char *event, json *params);

const char *dc_runtime_device_id(const dc_runtime *r);
const char *dc_runtime_registration_id(const dc_runtime *r);

#ifdef __cplusplus
}
#endif

#endif /* DC_RUNTIME_H */
