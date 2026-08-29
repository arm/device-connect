/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * test_agent.c -- unit tests for agent-tools JSON handling and env resolution.
 * (HTTP itself is exercised by the live e2e, which needs a portal + token.)
 *
 * ASCII-only source.
 */

#include "dc/agent_tools.h"
#include "dc/json.h"
#include "dc_test.h"

#include <stdlib.h>

static void test_resolve_env(void) {
    setenv("DEVICE_CONNECT_PORTAL_URL", "https://portal.example", 1);
    setenv("DEVICE_CONNECT_PORTAL_TOKEN", "dcp_x_y", 1);
    dc_agent a;
    memset(&a, 0, sizeof(a));
    CHECK(dc_agent_resolve(&a) == 0);
    CHECK_STR(a.portal_url, "https://portal.example");
    CHECK_STR(a.token, "dcp_x_y");
    /* explicit fields win over env */
    dc_agent b = {"https://other", "dcp_a_b"};
    CHECK(dc_agent_resolve(&b) == 0);
    CHECK_STR(b.portal_url, "https://other");
}

static void test_parse_fleet_response(void) {
    /* shape the portal returns for /fleet */
    const char *body =
        "{\"tenant\":\"alpha\",\"devices_registered\":3,\"devices_online\":3,"
        "\"by_device_type\":{\"temp_sensor\":3}}";
    const char *err = NULL;
    json *j = json_parse(body, strlen(body), &err);
    CHECK(j != NULL);
    CHECK(json_integer(json_object_get(j, "devices_online")) == 3);
    json *bt = json_object_get(j, "by_device_type");
    CHECK(json_integer(json_object_get(bt, "temp_sensor")) == 3);
    json_free(j);
}

static void test_parse_invoke_response(void) {
    /* portal invoke envelope embedding the device JSON-RPC response */
    const char *body =
        "{\"success\":true,\"result\":{\"device_id\":\"alpha-temp-001\","
        "\"function\":\"get_reading\",\"elapsed_ms\":42,"
        "\"response\":{\"jsonrpc\":\"2.0\",\"id\":\"1\","
        "\"result\":{\"temp_c\":21.5}}}}";
    const char *err = NULL;
    json *j = json_parse(body, strlen(body), &err);
    CHECK(j != NULL);
    json *resp = json_object_get(json_object_get(j, "result"), "response");
    json *res = json_object_get(resp, "result");
    CHECK(json_number(json_object_get(res, "temp_c")) == 21.5);
    json_free(j);
}

int main(void) {
    RUN(test_resolve_env);
    RUN(test_parse_fleet_response);
    RUN(test_parse_invoke_response);
    REPORT();
}
