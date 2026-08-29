/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * dc_agent.c -- a small CLI over the C agent-tools, mirroring the dc-portalctl
 * discover/invoke surface. Reads DEVICE_CONNECT_PORTAL_URL / _TOKEN from the
 * environment.
 *
 *   dc_agent fleet
 *   dc_agent list [device_type] [location]
 *   dc_agent functions <device_id>
 *   dc_agent invoke <device_id> <function> [json_params] [reason]
 *
 * ASCII-only source.
 */

#include "dc/agent_tools.h"
#include "dc/http.h"
#include "dc/json.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int dump(json *j, long status) {
    if (j == NULL) {
        fprintf(stderr, "request failed (http=%ld)\n", status);
        return 1;
    }
    char *s = json_dumps(j, 1, NULL);
    printf("%s\n", s ? s : "(null)");
    free(s);
    json_free(j);
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr,
                "usage: dc_agent fleet | list [type] [loc] | functions <id> | "
                "invoke <id> <fn> [json_params] [reason]\n");
        return 2;
    }
    dc_http_global_init();
    dc_agent a;
    memset(&a, 0, sizeof(a));
    if (dc_agent_resolve(&a) != 0) {
        fprintf(stderr,
                "set DEVICE_CONNECT_PORTAL_URL and DEVICE_CONNECT_PORTAL_TOKEN\n");
        dc_http_global_cleanup();
        return 2;
    }

    long status = 0;
    int rc = 1;
    const char *cmd = argv[1];
    if (!strcmp(cmd, "fleet")) {
        rc = dump(dc_describe_fleet(&a, &status), status);
    } else if (!strcmp(cmd, "list")) {
        const char *type = argc > 2 ? argv[2] : NULL;
        const char *loc = argc > 3 ? argv[3] : NULL;
        rc = dump(dc_list_devices(&a, type, loc, &status), status);
    } else if (!strcmp(cmd, "functions") && argc > 2) {
        rc = dump(dc_get_device_functions(&a, argv[2], &status), status);
    } else if (!strcmp(cmd, "invoke") && argc > 3) {
        const char *id = argv[2];
        const char *fn = argv[3];
        const char *pj = argc > 4 ? argv[4] : "{}";
        const char *reason = argc > 5 ? argv[5] : "dc_agent CLI invoke";
        const char *err = NULL;
        json *params = json_parse(pj, strlen(pj), &err);
        if (params == NULL) {
            fprintf(stderr, "bad json params: %s\n", err ? err : "?");
            dc_http_global_cleanup();
            return 2;
        }
        rc = dump(dc_invoke_device(&a, id, fn, params, reason, &status),
                  status);
        json_free(params);
    } else {
        fprintf(stderr, "unknown command or missing args: %s\n", cmd);
        rc = 2;
    }
    dc_http_global_cleanup();
    return rc;
}
