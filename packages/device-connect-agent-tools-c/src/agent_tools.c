/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * dc/agent_tools.c -- the agent-tools meta-tools over the portal HTTP API.
 *
 * ASCII-only source.
 */

#include "dc/agent_tools.h"
#include "dc/http.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int dc_agent_resolve(dc_agent *a) {
    if (a == NULL) {
        return -1;
    }
    if (a->portal_url == NULL) {
        a->portal_url = getenv("DEVICE_CONNECT_PORTAL_URL");
    }
    if (a->token == NULL) {
        a->token = getenv("DEVICE_CONNECT_PORTAL_TOKEN");
    }
    return (a->portal_url != NULL && a->token != NULL) ? 0 : -1;
}

/* percent-encode a query-parameter value into out (best-effort, alnum + -_.~) */
static void urlencode(const char *s, char *out, size_t cap) {
    static const char hexd[] = "0123456789ABCDEF";
    size_t o = 0;
    for (; s != NULL && *s != '\0' && o + 4 < cap; s++) {
        unsigned char c = (unsigned char)*s;
        if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
            (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' ||
            c == '~') {
            out[o++] = (char)c;
        } else {
            out[o++] = '%';
            out[o++] = hexd[c >> 4];
            out[o++] = hexd[c & 0xf];
        }
    }
    out[o] = '\0';
}

static json *parse_or_null(dc_http_response *resp, long *http_status) {
    if (http_status != NULL) {
        *http_status = resp->status;
    }
    json *j = NULL;
    if (resp->body != NULL) {
        const char *err = NULL;
        j = json_parse(resp->body, resp->len, &err);
    }
    dc_http_response_free(resp);
    return j;
}

json *dc_describe_fleet(const dc_agent *a, long *http_status) {
    if (a == NULL || a->portal_url == NULL) {
        return NULL;
    }
    char url[1024];
    snprintf(url, sizeof(url), "%s/api/agent/v1/fleet", a->portal_url);
    dc_http_response resp;
    if (dc_http_get(url, a->token, &resp) != 0) {
        return NULL;
    }
    return parse_or_null(&resp, http_status);
}

json *dc_list_devices(const dc_agent *a, const char *device_type,
                      const char *location, long *http_status) {
    if (a == NULL || a->portal_url == NULL) {
        return NULL;
    }
    char url[1536];
    int n = snprintf(url, sizeof(url), "%s/api/agent/v1/devices", a->portal_url);
    const char *sep = "?";
    char enc[512];
    if (device_type != NULL) {
        urlencode(device_type, enc, sizeof(enc));
        n += snprintf(url + n, sizeof(url) - (size_t)n, "%sdevice_type=%s", sep,
                      enc);
        sep = "&";
    }
    if (location != NULL) {
        urlencode(location, enc, sizeof(enc));
        n += snprintf(url + n, sizeof(url) - (size_t)n, "%slocation=%s", sep,
                      enc);
    }
    dc_http_response resp;
    if (dc_http_get(url, a->token, &resp) != 0) {
        return NULL;
    }
    return parse_or_null(&resp, http_status);
}

json *dc_get_device_functions(const dc_agent *a, const char *device_id,
                              long *http_status) {
    if (a == NULL || a->portal_url == NULL || device_id == NULL) {
        return NULL;
    }
    char enc[512];
    urlencode(device_id, enc, sizeof(enc));
    char url[1280];
    snprintf(url, sizeof(url), "%s/api/agent/v1/devices/%s/functions",
             a->portal_url, enc);
    dc_http_response resp;
    if (dc_http_get(url, a->token, &resp) != 0) {
        return NULL;
    }
    return parse_or_null(&resp, http_status);
}

json *dc_invoke_device(const dc_agent *a, const char *device_id,
                       const char *function, const json *params,
                       const char *reason, long *http_status) {
    if (a == NULL || a->portal_url == NULL || device_id == NULL ||
        function == NULL) {
        return NULL;
    }
    /* body: {"function":..., "params":{...}, "reason":..., "timeout":10} */
    json *body = json_object();
    if (body == NULL) {
        return NULL;
    }
    json_object_set(body, "function", json_string(function));
    json_object_set(body, "params",
                    params ? json_clone(params) : json_object());
    json_object_set(body, "reason", json_string(reason ? reason : ""));
    json_object_set(body, "timeout", json_int(10));
    size_t blen = 0;
    char *json_body = json_dumps(body, 0, &blen);
    json_free(body);
    if (json_body == NULL) {
        return NULL;
    }
    char enc[512];
    urlencode(device_id, enc, sizeof(enc));
    char url[1280];
    snprintf(url, sizeof(url), "%s/api/agent/v1/devices/%s/invoke",
             a->portal_url, enc);
    dc_http_response resp;
    int rc = dc_http_post(url, a->token, json_body, &resp);
    free(json_body);
    if (rc != 0) {
        return NULL;
    }
    return parse_or_null(&resp, http_status);
}
