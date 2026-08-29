/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * dc/jsonrpc.h -- JSON-RPC 2.0 envelopes (wire_contract.md sec 4.1-4.2).
 *
 * Requests, responses, errors and notifications, plus the closed set of error
 * codes a wire node uses. The envelope carries no binary; the binary trailer
 * is handled separately by dc/frame.h.
 *
 * ASCII-only source (per CLAUDE.md).
 */

#ifndef DC_JSONRPC_H
#define DC_JSONRPC_H

#include "dc/json.h"

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* JSON-RPC / MHP error codes (wire_contract.md sec 4.2). */
#define DC_ERR_PARSE (-32700)         /* invalid JSON */
#define DC_ERR_INVALID_REQ (-32600)   /* not an object; jsonrpc != "2.0" */
#define DC_ERR_METHOD_NF (-32601)     /* unknown method / unknown invoke name */
#define DC_ERR_INVALID_PARAMS (-32602)/* bad params / bad kind tag */
#define DC_ERR_INTERNAL (-32603)      /* uncaught error during dispatch */
#define DC_ERR_SERVER (-32000)        /* offset-routed verb, safety refusal */

/* A parsed inbound JSON-RPC request. Borrowed views into the owning json. */
typedef struct {
    const char *id;     /* request id string, or NULL for a notification */
    const char *method; /* method name (never NULL on success) */
    json *params;       /* borrowed; NULL if absent */
} dc_rpc_request;

/*
 * Validate and view a request out of an already-parsed JSON value.
 * Returns 0 on success; on failure returns the JSON-RPC error code to reply
 * with (DC_ERR_INVALID_REQ when jsonrpc != "2.0" or shape is wrong --
 * clause C5). The request views alias into `root`, which must outlive use.
 */
int dc_rpc_parse_request(json *root, dc_rpc_request *out);

/*
 * Build envelopes as freshly malloc'd NUL-terminated JSON byte strings
 * (caller frees). *out_len, when non-NULL, receives the length. NULL on OOM.
 *
 * For responses/errors, id may be NULL (emits JSON null id, as permitted for
 * errors whose request id could not be determined).
 *
 * build_response takes ownership of `result` (it is embedded then freed).
 */
char *dc_rpc_build_response(const char *id, json *result, size_t *out_len);
char *dc_rpc_build_error(const char *id, int code, const char *message,
                               size_t *out_len);
/* Notification: no id. Takes ownership of `params`. */
char *dc_rpc_build_notification(const char *method, json *params,
                                      size_t *out_len);
/* Request: takes ownership of `params`. id is copied. */
char *dc_rpc_build_request(const char *id, const char *method,
                                 json *params, size_t *out_len);

#ifdef __cplusplus
}
#endif

#endif /* DC_JSONRPC_H */
