/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * dc/agent_tools.h -- the Device Connect agent-tools meta-tools in C.
 *
 * The external-agent path (the C analogue of device-connect-agent-tools):
 * describe_fleet / list_devices / get_device_functions / invoke_device over
 * the portal HTTP API. Results are returned as parsed JSON (caller json_free).
 *
 * ASCII-only source.
 */

#ifndef DC_AGENT_TOOLS_H
#define DC_AGENT_TOOLS_H

#include "dc/json.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    const char *portal_url; /* e.g. https://portal.deviceconnect.dev; NULL -> $DEVICE_CONNECT_PORTAL_URL */
    const char *token;      /* dcp_... ; NULL -> $DEVICE_CONNECT_PORTAL_TOKEN */
} dc_agent;

/* Resolve portal_url/token from the struct or the environment. Returns 0 if
 * both are available, -1 otherwise. */
int dc_agent_resolve(dc_agent *a);

/*
 * Each call performs one portal request and returns the parsed JSON response
 * (caller json_free), or NULL on transport/parse failure. *http_status, when
 * non-NULL, receives the HTTP status code.
 */
json *dc_describe_fleet(const dc_agent *a, long *http_status);
json *dc_list_devices(const dc_agent *a, const char *device_type,
                      const char *location, long *http_status);
json *dc_get_device_functions(const dc_agent *a, const char *device_id,
                              long *http_status);
/*
 * Invoke a device function. `params` is a JSON object (borrowed; may be NULL).
 * `reason` is the mandatory audit string. Returns the portal's response
 * envelope (which embeds the device's JSON-RPC `response`).
 */
json *dc_invoke_device(const dc_agent *a, const char *device_id,
                       const char *function, const json *params,
                       const char *reason, long *http_status);

#ifdef __cplusplus
}
#endif

#endif /* DC_AGENT_TOOLS_H */
