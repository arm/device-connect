/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * dc/driver.h -- the Device Connect C edge SDK driver surface.
 *
 * A driver is the device's logic: a set of named RPC functions (the @rpc
 * equivalent), advertised events (@emit), and identity/status metadata. The
 * runtime (dc/runtime.h) serves the driver over NATS, registers it with the
 * portal registry, and dispatches inbound commands to its functions.
 *
 * Device Connect RPC shape: a command on device-connect.{tenant}.{id}.cmd is
 * JSON-RPC where `method` IS the function name and `params` are the arguments
 * directly; the reply carries the function's raw return value as `result`.
 *
 * ASCII-only source.
 */

#ifndef DC_DRIVER_H
#define DC_DRIVER_H

#include "dc/json.h"

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Result a function fills in. On success set `result` (a heap json, ownership
 * transferred to the runtime) or leave it NULL for a {}-returning call. On
 * failure set err_code (a JSON-RPC code) and optionally err_msg. */
typedef struct {
    json *result;
    int err_code;
    char err_msg[192];
} dc_rpc_result;

/* An RPC handler. `params` is the command params object (the arguments), or
 * NULL. */
typedef void (*dc_rpc_fn)(void *user, const json *params, dc_rpc_result *out);

typedef struct dc_driver dc_driver;

dc_driver *dc_driver_new(const char *device_type);
void dc_driver_free(dc_driver *d);

/*
 * Register an RPC function. `name` is the bare function name (the JSON-RPC
 * method clients call). `params_schema` is an optional JSON Schema object
 * (ownership transferred; NULL for no args). Returns 0 / -1.
 */
int dc_driver_add_function(dc_driver *d, const char *name,
                           const char *description, json *params_schema,
                           dc_rpc_fn fn, void *user);

/* Advertise an event the device emits (appears in capabilities.events). */
int dc_driver_add_event(dc_driver *d, const char *name,
                        const char *description);

/* Identity / status metadata (all optional; copied). */
void dc_driver_set_identity(dc_driver *d, const char *manufacturer,
                            const char *model, const char *firmware_version,
                            const char *description);
void dc_driver_set_location(dc_driver *d, const char *location);
void dc_driver_set_availability(dc_driver *d, const char *availability);

/* ---- used by the runtime ---- */

const char *dc_driver_device_type(const dc_driver *d);

/* Build the DC capabilities object {description, functions[], events[]}
 * (owned). */
json *dc_driver_capabilities(const dc_driver *d);

/* Build the DC identity object {device_type, manufacturer, model, ...}
 * (owned). */
json *dc_driver_identity(const dc_driver *d);

/* Build the DC status object {availability, location, ...} without ts
 * (owned). */
json *dc_driver_status(const dc_driver *d);

/*
 * Dispatch a command by function name. On success returns 0 and sets
 * *result_out (owned json, or NULL for void). On failure returns a JSON-RPC
 * error code and fills errmsg.
 */
int dc_driver_call(const dc_driver *d, const char *function, const json *params,
                   json **result_out, char *errmsg, size_t errcap);

#ifdef __cplusplus
}
#endif

#endif /* DC_DRIVER_H */
