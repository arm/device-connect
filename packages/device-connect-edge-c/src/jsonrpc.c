/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * dc/jsonrpc.c -- JSON-RPC 2.0 envelope build/parse.
 *
 * ASCII-only source (per CLAUDE.md).
 */

#include "dc/jsonrpc.h"

#include <stdlib.h>
#include <string.h>

int dc_rpc_parse_request(json *root, dc_rpc_request *out) {
    if (out == NULL) {
        return DC_ERR_INTERNAL;
    }
    out->id = NULL;
    out->method = NULL;
    out->params = NULL;
    if (root == NULL || json_typeof(root) != JSON_OBJECT) {
        return DC_ERR_INVALID_REQ; /* C5: not an object */
    }
    /* jsonrpc MUST be exactly "2.0" (C5). */
    json *ver = json_object_get(root, "jsonrpc");
    const char *vs = json_str(ver);
    if (vs == NULL || strcmp(vs, "2.0") != 0) {
        return DC_ERR_INVALID_REQ;
    }
    json *method = json_object_get(root, "method");
    const char *ms = json_str(method);
    if (ms == NULL) {
        return DC_ERR_INVALID_REQ;
    }
    out->method = ms;
    /* id is optional (absent => notification). Accept string ids; a non-string
     * id is tolerated but normalized to NULL for our reply path. */
    json *id = json_object_get(root, "id");
    out->id = json_str(id); /* NULL if absent or non-string */
    out->params = json_object_get(root, "params"); /* borrowed, may be NULL */
    return 0;
}

/* shared envelope assembler: builds {"jsonrpc":"2.0", <id?>, <body>} */
static char *build_envelope(const char *id, int with_id, const char *body_key,
                            json *body_val, const char *method,
                            size_t *out_len) {
    json *env = json_object();
    if (env == NULL) {
        json_free(body_val);
        return NULL;
    }
    if (json_object_set(env, "jsonrpc", json_string("2.0")) != 0) {
        json_free(body_val);
        json_free(env);
        return NULL;
    }
    if (method != NULL) {
        if (json_object_set(env, "method", json_string(method)) != 0) {
            json_free(body_val);
            json_free(env);
            return NULL;
        }
    }
    if (with_id) {
        json *idv = (id != NULL) ? json_string(id) : json_null();
        if (json_object_set(env, "id", idv) != 0) {
            json_free(body_val);
            json_free(env);
            return NULL;
        }
    }
    if (body_key != NULL) {
        if (json_object_set(env, body_key, body_val) != 0) {
            json_free(env);
            return NULL;
        }
    } else {
        json_free(body_val);
    }
    char *s = json_dumps(env, 0, out_len);
    json_free(env);
    return s;
}

char *dc_rpc_build_response(const char *id, json *result,
                                  size_t *out_len) {
    if (result == NULL) {
        result = json_null();
    }
    return build_envelope(id, 1, "result", result, NULL, out_len);
}

char *dc_rpc_build_error(const char *id, int code, const char *message,
                               size_t *out_len) {
    json *err = json_object();
    if (err == NULL) {
        return NULL;
    }
    if (json_object_set(err, "code", json_int(code)) != 0 ||
        json_object_set(err, "message",
                        json_string(message != NULL ? message : "")) != 0) {
        json_free(err);
        return NULL;
    }
    return build_envelope(id, 1, "error", err, NULL, out_len);
}

char *dc_rpc_build_notification(const char *method, json *params,
                                      size_t *out_len) {
    if (params == NULL) {
        params = json_object();
    }
    return build_envelope(NULL, 0, "params", params, method, out_len);
}

char *dc_rpc_build_request(const char *id, const char *method,
                                 json *params, size_t *out_len) {
    if (params == NULL) {
        params = json_object();
    }
    return build_envelope(id, 1, "params", params, method, out_len);
}
