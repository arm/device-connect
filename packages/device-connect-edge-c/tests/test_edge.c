/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * test_edge.c -- unit tests for the edge driver + JSON core.
 *
 * ASCII-only source.
 */

#include "dc/driver.h"
#include "dc/json.h"
#include "dc/jsonrpc.h"
#include "dc_test.h"

#include <stdlib.h>

static void rpc_reading(void *u, const json *p, dc_rpc_result *o) {
    (void)u;
    (void)p;
    json *r = json_object();
    json_object_set(r, "temp_c", json_real(22.5));
    o->result = r;
}
static void rpc_fail(void *u, const json *p, dc_rpc_result *o) {
    (void)u;
    (void)p;
    o->err_code = DC_ERR_INVALID_PARAMS;
    snprintf(o->err_msg, sizeof(o->err_msg), "boom");
}

static dc_driver *make(void) {
    dc_driver *d = dc_driver_new("temp_sensor");
    dc_driver_set_identity(d, "ACME", "T1", "0.1.0", "demo");
    dc_driver_add_function(d, "get_reading", "Read temp", NULL, rpc_reading,
                           NULL);
    dc_driver_add_function(d, "boom", "fails", NULL, rpc_fail, NULL);
    dc_driver_add_event(d, "reading_changed", "moved");
    return d;
}

static int has(const char *hay, const char *needle) {
    return hay && strstr(hay, needle) != NULL;
}

static void test_capabilities(void) {
    dc_driver *d = make();
    json *caps = dc_driver_capabilities(d);
    char *s = json_dumps(caps, 1, NULL);
    CHECK(has(s, "\"functions\""));
    CHECK(has(s, "\"events\""));
    CHECK(has(s, "get_reading"));
    CHECK(has(s, "reading_changed"));
    free(s);
    json_free(caps);
    dc_driver_free(d);
}

static void test_identity_status(void) {
    dc_driver *d = make();
    json *id = dc_driver_identity(d);
    CHECK_STR(json_str(json_object_get(id, "device_type")), "temp_sensor");
    CHECK_STR(json_str(json_object_get(id, "manufacturer")), "ACME");
    json_free(id);
    json *st = dc_driver_status(d);
    /* DC healthy availability is "available" (portal online predicate). */
    CHECK_STR(json_str(json_object_get(st, "availability")), "available");
    json_free(st);
    dc_driver_free(d);
}

static void test_call(void) {
    dc_driver *d = make();
    json *res = NULL;
    char err[192];
    /* found -> 0, returns result */
    CHECK(dc_driver_call(d, "get_reading", NULL, &res, err, sizeof(err)) == 0);
    CHECK(res && json_number(json_object_get(res, "temp_c")) == 22.5);
    json_free(res);
    /* unknown -> -32601 */
    res = NULL;
    CHECK(dc_driver_call(d, "nope", NULL, &res, err, sizeof(err)) ==
          DC_ERR_METHOD_NF);
    /* handler error propagates */
    res = NULL;
    CHECK(dc_driver_call(d, "boom", NULL, &res, err, sizeof(err)) ==
          DC_ERR_INVALID_PARAMS);
    CHECK_STR(err, "boom");
    dc_driver_free(d);
}

static void test_jsonrpc_shapes(void) {
    /* DC reply: result is the raw value */
    json *r = json_object();
    json_object_set(r, "temp_c", json_real(1.0));
    size_t n = 0;
    char *s = dc_rpc_build_response("7", r, &n);
    CHECK(has(s, "\"jsonrpc\":\"2.0\"") && has(s, "\"id\":\"7\"") &&
          has(s, "\"result\""));
    free(s);
    s = dc_rpc_build_error("7", DC_ERR_METHOD_NF, "no such function", &n);
    CHECK(has(s, "-32601") && has(s, "no such function"));
    free(s);
}

int main(void) {
    RUN(test_capabilities);
    RUN(test_identity_status);
    RUN(test_call);
    RUN(test_jsonrpc_shapes);
    REPORT();
}
