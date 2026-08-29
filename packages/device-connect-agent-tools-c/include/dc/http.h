/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * dc/http.h -- a tiny libcurl wrapper for the Device Connect portal HTTP API.
 *
 * Bearer-authenticated GET/POST returning the response body. Used by the
 * agent-tools layer (dc/agent_tools.h). TLS is handled by libcurl.
 *
 * ASCII-only source.
 */

#ifndef DC_HTTP_H
#define DC_HTTP_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    long status;  /* HTTP status code, or 0 on transport failure */
    char *body;   /* malloc'd response body (caller frees), or NULL */
    size_t len;
} dc_http_response;

/* Call once at startup (wraps curl_global_init). */
int dc_http_global_init(void);
void dc_http_global_cleanup(void);

/*
 * GET url with "Authorization: Bearer <token>". Returns 0 on a completed
 * request (check resp.status), -1 on a transport-level failure. Caller frees
 * resp.body.
 */
int dc_http_get(const char *url, const char *token, dc_http_response *resp);

/* POST url with a JSON body (Content-Type: application/json) + Bearer token. */
int dc_http_post(const char *url, const char *token, const char *json_body,
                 dc_http_response *resp);

void dc_http_response_free(dc_http_response *resp);

#ifdef __cplusplus
}
#endif

#endif /* DC_HTTP_H */
