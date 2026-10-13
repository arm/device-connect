/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * temp_sensor.c -- a minimal Device Connect device in C, the analogue of the
 * Python edge SDK's number_generator/dht22 examples. Exposes get_reading and
 * set_target, advertises a reading_changed event, and runs under the runtime.
 *
 * Usage:
 *   NATS_CREDENTIALS_FILE=./alpha-temp-001.creds.json \
 *   temp_sensor --server nats://portal:4222 [--device-id alpha-temp-001] \
 *               [--tenant alpha] [--device-ttl 30]
 *
 * device_id and tenant are auto-detected from a *.creds.json when omitted.
 *
 * ASCII-only source.
 */

#include "dc/driver.h"
#include "dc/jsonrpc.h" /* DC_ERR_* codes */
#include "dc/runtime.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    double target_c;
    double measured_c;
} sensor_state;

static void rpc_get_reading(void *user, const json *params, dc_rpc_result *out) {
    (void)params;
    sensor_state *s = (sensor_state *)user;
    s->measured_c += (s->target_c - s->measured_c) * 0.25;
    json *r = json_object();
    json_object_set(r, "temp_c", json_real(s->measured_c));
    out->result = r;
}

static void rpc_set_target(void *user, const json *params, dc_rpc_result *out) {
    sensor_state *s = (sensor_state *)user;
    json *c = json_object_get((json *)params, "celsius");
    if (c == NULL ||
        (json_typeof(c) != JSON_INT && json_typeof(c) != JSON_REAL)) {
        out->err_code = DC_ERR_INVALID_PARAMS;
        snprintf(out->err_msg, sizeof(out->err_msg), "celsius must be a number");
        return;
    }
    s->target_c = json_number(c);
    json *r = json_object();
    json_object_set(r, "ok", json_bool(1));
    out->result = r;
}

int main(int argc, char **argv) {
    dc_runtime_config cfg;
    memset(&cfg, 0, sizeof(cfg));
    cfg.device_ttl = 30;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--server") && i + 1 < argc) {
            cfg.server = argv[++i];
        } else if (!strcmp(argv[i], "--device-id") && i + 1 < argc) {
            cfg.device_id = argv[++i];
        } else if (!strcmp(argv[i], "--tenant") && i + 1 < argc) {
            cfg.tenant = argv[++i];
        } else if (!strcmp(argv[i], "--device-ttl") && i + 1 < argc) {
            cfg.device_ttl = atoi(argv[++i]);
        } else {
            fprintf(stderr, "unknown arg: %s\n", argv[i]);
            return 2;
        }
    }

    sensor_state state = {0.0, 20.0};

    dc_driver *d = dc_driver_new("temp_sensor");
    dc_driver_set_identity(d, "ACME", "T1", "0.1.0",
                           "Demo temperature sensor (C edge SDK)");
    dc_driver_set_location(d, "lab/bench-3");

    json *sch = json_object();
    json *props = json_object();
    json *cel = json_object();
    json_object_set(cel, "type", json_string("number"));
    json_object_set(props, "celsius", cel);
    json *req = json_array();
    json_array_append(req, json_string("celsius"));
    json_object_set(sch, "type", json_string("object"));
    json_object_set(sch, "properties", props);
    json_object_set(sch, "required", req);

    dc_driver_add_function(d, "get_reading", "Read current temperature.", NULL,
                           rpc_get_reading, &state);
    dc_driver_add_function(d, "set_target", "Set target temperature.", sch,
                           rpc_set_target, &state);
    dc_driver_add_event(d, "reading_changed", "Emitted when the reading moves.");

    dc_runtime *rt = dc_runtime_new(d, &cfg);
    if (rt == NULL || dc_runtime_start(rt) != 0) {
        fprintf(stderr, "[temp_sensor] failed to start\n");
        dc_runtime_free(rt);
        dc_driver_free(d);
        return 1;
    }
    fprintf(stderr, "[temp_sensor] serving as %s\n", dc_runtime_device_id(rt));
    dc_runtime_run(rt); /* blocks until SIGINT/SIGTERM */
    dc_runtime_free(rt);
    dc_driver_free(d);
    return 0;
}
