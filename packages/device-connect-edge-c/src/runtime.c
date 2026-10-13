/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * dc/runtime.c -- Device Connect C edge SDK runtime.
 *
 * ASCII-only source.
 */

#include "dc/runtime.h"
#include "dc/jsonrpc.h"
#include "dc/security.h"
#include "dc/transport.h"
#include "dc/transport_nats.h"

#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

double dc_now(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (double)tv.tv_sec + (double)tv.tv_usec / 1e6;
}

static void iso_now(char *buf, size_t cap) {
    time_t t = time(NULL);
    struct tm tm_utc;
    gmtime_r(&t, &tm_utc);
    strftime(buf, cap, "%Y-%m-%dT%H:%M:%SZ", &tm_utc);
}

struct dc_runtime {
    dc_driver *driver; /* borrowed */
    dc_transport t;
    int have_transport;
    dc_security sec;
    char *temp_creds; /* temp chained creds to unlink, or NULL */
    char *server;     /* nats URL */
    char *tenant;
    char *device_id;
    int device_ttl;
    char *subj_cmd;
    char *subj_registry;
    char *subj_heartbeat;
    char *reg_id;
    int registered;
    double hb_interval;
    double last_heartbeat;
    unsigned reg_seq;
};

/* ------------------------------------------------------------------ */
/* credentials: accept *.creds.json (DC) or chained *.creds            */
/* ------------------------------------------------------------------ */

static char *read_file(const char *path, size_t *len) {
    FILE *f = fopen(path, "rb");
    if (f == NULL) {
        return NULL;
    }
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (n < 0) {
        fclose(f);
        return NULL;
    }
    char *buf = (char *)malloc((size_t)n + 1);
    if (buf == NULL) {
        fclose(f);
        return NULL;
    }
    size_t rd = fread(buf, 1, (size_t)n, f);
    fclose(f);
    buf[rd] = '\0';
    if (len) {
        *len = rd;
    }
    return buf;
}

/* If creds_path is a DC *.creds.json, convert to a temp nsc-chained .creds and
 * extract device_id/tenant. Returns the path to use for cnats (temp or the
 * original), or NULL on error. */
static char *prepare_creds(dc_runtime *r, const char *creds_path) {
    size_t len = 0;
    char *content = read_file(creds_path, &len);
    if (content == NULL) {
        return NULL;
    }
    /* chained .creds already? use as-is */
    if (strstr(content, "BEGIN NATS USER JWT") != NULL) {
        free(content);
        return strdup(creds_path);
    }
    const char *err = NULL;
    json *j = json_parse(content, len, &err);
    free(content);
    if (j == NULL) {
        return NULL;
    }
    json *nats = json_object_get(j, "nats");
    const char *jwt = nats ? json_str(json_object_get(nats, "jwt")) : NULL;
    const char *seed = nats ? json_str(json_object_get(nats, "nkey_seed"))
                            : NULL;
    const char *did = json_str(json_object_get(j, "device_id"));
    const char *tnt = json_str(json_object_get(j, "tenant"));
    if (r->device_id == NULL && did != NULL) {
        r->device_id = strdup(did);
    }
    if (r->tenant == NULL && tnt != NULL) {
        r->tenant = strdup(tnt);
    }
    char *out = NULL;
    if (jwt != NULL && seed != NULL) {
        char tmpl[] = "/tmp/dc-edge-creds-XXXXXX";
        int fd = mkstemp(tmpl);
        if (fd >= 0) {
            FILE *f = fdopen(fd, "w");
            if (f != NULL) {
                fprintf(f,
                        "-----BEGIN NATS USER JWT-----\n%s\n"
                        "------END NATS USER JWT------\n\n"
                        "-----BEGIN USER NKEY SEED-----\n%s\n"
                        "------END USER NKEY SEED------\n",
                        jwt, seed);
                fclose(f);
                out = strdup(tmpl);
                r->temp_creds = strdup(tmpl);
            } else {
                close(fd);
            }
        }
    }
    json_free(j);
    return out;
}

/* ------------------------------------------------------------------ */
/* subjects                                                            */
/* ------------------------------------------------------------------ */

static char *fmt(const char *tmpl, const char *a, const char *b) {
    int n = snprintf(NULL, 0, tmpl, a, b);
    if (n < 0) {
        return NULL;
    }
    char *s = (char *)malloc((size_t)n + 1);
    if (s != NULL) {
        snprintf(s, (size_t)n + 1, tmpl, a, b);
    }
    return s;
}

/* ------------------------------------------------------------------ */
/* registration params                                                 */
/* ------------------------------------------------------------------ */

static json *build_register_params(dc_runtime *r) {
    json *p = json_object();
    if (p == NULL) {
        return NULL;
    }
    char ts[32];
    iso_now(ts, sizeof(ts));
    json *status = dc_driver_status(r->driver);
    if (status != NULL) {
        json_object_set(status, "ts", json_string(ts));
    }
    json_object_set(p, "device_id", json_string(r->device_id));
    json_object_set(p, "device_ttl", json_int(r->device_ttl));
    json_object_set(p, "capabilities", dc_driver_capabilities(r->driver));
    json_object_set(p, "identity", dc_driver_identity(r->driver));
    json_object_set(p, "status", status);
    return p;
}

static void do_register(dc_runtime *r) {
    json *params = build_register_params(r);
    if (params == NULL) {
        return;
    }
    char reqid[64];
    snprintf(reqid, sizeof(reqid), "reg-%s-%u", r->device_id, ++r->reg_seq);
    size_t blen = 0;
    char *bytes = dc_rpc_build_request(reqid, "registerDevice", params, &blen);
    if (bytes == NULL) {
        return;
    }
    uint8_t *reply = NULL;
    size_t rn = 0;
    int rc = r->t.request(r->t.impl, r->subj_registry, (const uint8_t *)bytes,
                          blen, &reply, &rn, 5000);
    free(bytes);
    if (rc != 0) {
        fprintf(stderr, "[dc-edge] registerDevice got no reply; serving anyway\n");
        return;
    }
    const char *err = NULL;
    json *env = json_parse((const char *)reply, rn, &err);
    free(reply);
    if (env != NULL) {
        json *result = json_object_get(env, "result");
        const char *id =
            result ? json_str(json_object_get(result, "device_registration_id"))
                   : NULL;
        if (id != NULL) {
            free(r->reg_id);
            r->reg_id = strdup(id);
            r->registered = 1;
            fprintf(stderr, "[dc-edge] registered: registration_id=%s\n", id);
        } else {
            fprintf(stderr, "[dc-edge] registerDevice error: %.*s\n", (int)rn,
                    (const char *)"(see reply)");
        }
        json_free(env);
    }
}

/* ------------------------------------------------------------------ */
/* command handler                                                     */
/* ------------------------------------------------------------------ */

static void on_cmd(void *user, const char *subject, const uint8_t *data,
                   size_t len, const char *reply) {
    (void)subject;
    dc_runtime *r = (dc_runtime *)user;
    if (reply == NULL) {
        return;
    }
    const char *err = NULL;
    json *root = json_parse((const char *)data, len, &err);
    char *env = NULL;
    size_t en = 0;
    if (root == NULL || json_typeof(root) != JSON_OBJECT) {
        env = dc_rpc_build_error(NULL, DC_ERR_PARSE, "parse error", &en);
    } else {
        const char *ver = json_str(json_object_get(root, "jsonrpc"));
        const char *id = json_str(json_object_get(root, "id"));
        const char *method = json_str(json_object_get(root, "method"));
        json *params = json_object_get(root, "params");
        if (ver == NULL || strcmp(ver, "2.0") != 0) {
            env = dc_rpc_build_error(id, DC_ERR_INVALID_REQ,
                                     "jsonrpc must be 2.0", &en);
        } else if (method == NULL) {
            env = dc_rpc_build_error(id, DC_ERR_INVALID_REQ, "missing method",
                                     &en);
        } else if (strcmp(method, "requestRegistration") == 0) {
            json *p = build_register_params(r);
            env = dc_rpc_build_response(id, p, &en);
        } else {
            char emsg[192];
            emsg[0] = '\0';
            json *result = NULL;
            int rc = dc_driver_call(r->driver, method, params, &result, emsg,
                                    sizeof(emsg));
            if (rc == 0) {
                if (result == NULL) {
                    result = json_object();
                }
                env = dc_rpc_build_response(id, result, &en);
            } else {
                env = dc_rpc_build_error(id, rc, emsg, &en);
            }
        }
    }
    json_free(root);
    if (env != NULL) {
        r->t.respond(r->t.impl, reply, (const uint8_t *)env, en);
        free(env);
    }
}

/* ------------------------------------------------------------------ */
/* lifecycle                                                           */
/* ------------------------------------------------------------------ */

dc_runtime *dc_runtime_new(dc_driver *driver, const dc_runtime_config *cfg) {
    if (driver == NULL || cfg == NULL) {
        return NULL;
    }
    dc_runtime *r = (dc_runtime *)calloc(1, sizeof(*r));
    if (r == NULL) {
        return NULL;
    }
    r->driver = driver;
    r->device_ttl =
        (cfg->device_ttl > 0) ? cfg->device_ttl : DC_DEFAULT_DEVICE_TTL;
    r->device_id = cfg->device_id ? strdup(cfg->device_id) : NULL;
    r->tenant = cfg->tenant ? strdup(cfg->tenant) : NULL;
    dc_security_init(&r->sec);

    const char *creds = cfg->creds_file ? cfg->creds_file
                                        : getenv("NATS_CREDENTIALS_FILE");
    if (creds != NULL) {
        char *use = prepare_creds(r, creds);
        if (use != NULL) {
            r->sec.creds_file = use; /* owned by sec; freed in security_free */
        }
    }
    if (r->tenant == NULL) {
        r->tenant = strdup("default");
    }
    if (r->device_id == NULL) {
        fprintf(stderr, "[dc-edge] no device_id (set config or use a "
                        ".creds.json that carries one)\n");
        dc_runtime_free(r);
        return NULL;
    }
    r->subj_cmd = fmt("device-connect.%s.%s.cmd", r->tenant, r->device_id);
    r->subj_heartbeat =
        fmt("device-connect.%s.%s.heartbeat", r->tenant, r->device_id);
    r->subj_registry = fmt("device-connect.%s.registry", r->tenant, "");
    /* fmt() with one %s ignores the 2nd arg harmlessly for registry */
    r->hb_interval = (r->device_ttl > 3) ? (double)r->device_ttl / 3.0 : 1.0;
    r->server = cfg->server ? strdup(cfg->server) : NULL;
    if (r->subj_cmd == NULL || r->subj_heartbeat == NULL ||
        r->subj_registry == NULL) {
        dc_runtime_free(r);
        return NULL;
    }
    return r;
}

void dc_runtime_free(dc_runtime *r) {
    if (r == NULL) {
        return;
    }
    if (r->have_transport) {
        r->t.close(r->t.impl);
    }
    if (r->temp_creds != NULL) {
        unlink(r->temp_creds);
        free(r->temp_creds);
    }
    dc_security_free(&r->sec);
    free(r->server);
    free(r->tenant);
    free(r->device_id);
    free(r->subj_cmd);
    free(r->subj_heartbeat);
    free(r->subj_registry);
    free(r->reg_id);
    free(r);
}

int dc_runtime_start(dc_runtime *r) {
    if (r == NULL) {
        return -1;
    }
    const char *server = r->server ? r->server : getenv("NATS_URL");
    if (server == NULL) {
        server = "nats://127.0.0.1:4222";
    }
    dc_nats_config nc;
    memset(&nc, 0, sizeof(nc));
    nc.url = server;
    nc.security = &r->sec;
    if (dc_transport_nats_create(&r->t, &nc) != 0) {
        return -1;
    }
    r->have_transport = 1;
    if (r->t.subscribe(r->t.impl, r->subj_cmd, on_cmd, r) != 0) {
        return -1;
    }
    do_register(r);
    return 0;
}

void dc_runtime_tick(dc_runtime *r, double now) {
    if (r == NULL) {
        return;
    }
    /* reconnect re-arm: re-register on a reconnect after an outage >= ttl */
    double outage = 0.0;
    if (dc_transport_nats_poll_reconnect(&r->t, &outage)) {
        if (!r->registered || outage >= (double)r->device_ttl) {
            do_register(r);
        }
    }
    if (now - r->last_heartbeat >= r->hb_interval || r->last_heartbeat == 0.0) {
        json *beat = json_object();
        if (beat != NULL) {
            json_object_set(beat, "device_id", json_string(r->device_id));
            json_object_set(beat, "ts", json_real(now));
            size_t bn = 0;
            char *s = json_dumps(beat, 0, &bn);
            json_free(beat);
            if (s != NULL) {
                r->t.publish(r->t.impl, r->subj_heartbeat, (const uint8_t *)s,
                             bn);
                free(s);
            }
        }
        r->last_heartbeat = now;
    }
}

static volatile sig_atomic_t g_stop;
static void on_sig(int s) {
    (void)s;
    g_stop = 1;
}

void dc_runtime_run(dc_runtime *r) {
    if (r == NULL) {
        return;
    }
    signal(SIGINT, on_sig);
    signal(SIGTERM, on_sig);
    g_stop = 0;
    struct timespec ts = {0, 50 * 1000000L};
    while (!g_stop) {
        dc_runtime_tick(r, dc_now());
        nanosleep(&ts, NULL);
    }
    dc_runtime_stop(r);
}

void dc_runtime_stop(dc_runtime *r) {
    if (r == NULL || !r->have_transport) {
        return;
    }
    /* announce departure via a presence-style offline note on heartbeat ts=0 */
    json *beat = json_object();
    if (beat != NULL) {
        json_object_set(beat, "device_id", json_string(r->device_id));
        json_object_set(beat, "departing", json_bool(1));
        size_t bn = 0;
        char *s = json_dumps(beat, 0, &bn);
        json_free(beat);
        if (s != NULL) {
            r->t.publish(r->t.impl, r->subj_heartbeat, (const uint8_t *)s, bn);
            free(s);
        }
    }
}

json *dc_runtime_invoke_remote(dc_runtime *r, const char *device_id,
                               const char *function, json *params,
                               int timeout_ms) {
    if (r == NULL || device_id == NULL || function == NULL) {
        json_free(params);
        return NULL;
    }
    char reqid[64];
    snprintf(reqid, sizeof(reqid), "d2d-%u", ++r->reg_seq);
    size_t blen = 0;
    char *bytes = dc_rpc_build_request(reqid, function,
                                       params ? params : json_object(), &blen);
    if (bytes == NULL) {
        return NULL;
    }
    char *subj = fmt("device-connect.%s.%s.cmd", r->tenant, device_id);
    uint8_t *reply = NULL;
    size_t rn = 0;
    int rc = -1;
    if (subj != NULL) {
        rc = r->t.request(r->t.impl, subj, (const uint8_t *)bytes, blen, &reply,
                          &rn, timeout_ms > 0 ? timeout_ms : 5000);
    }
    free(bytes);
    free(subj);
    if (rc != 0) {
        return NULL;
    }
    const char *err = NULL;
    json *env = json_parse((const char *)reply, rn, &err);
    free(reply);
    return env;
}

void dc_runtime_emit(dc_runtime *r, const char *event, json *params) {
    if (r == NULL || event == NULL) {
        json_free(params);
        return;
    }
    char *subj = fmt("device-connect.%s.%s", r->tenant, r->device_id);
    /* append .event.{name} */
    char *full = NULL;
    if (subj != NULL) {
        int n = snprintf(NULL, 0, "%s.event.%s", subj, event);
        full = (char *)malloc((size_t)n + 1);
        if (full != NULL) {
            snprintf(full, (size_t)n + 1, "%s.event.%s", subj, event);
        }
    }
    free(subj);
    if (full == NULL) {
        json_free(params);
        return;
    }
    size_t bn = 0;
    char *s = dc_rpc_build_notification(event, params ? params : json_object(),
                                        &bn);
    if (s != NULL) {
        r->t.publish(r->t.impl, full, (const uint8_t *)s, bn);
        free(s);
    }
    free(full);
}

const char *dc_runtime_device_id(const dc_runtime *r) {
    return r ? r->device_id : NULL;
}
const char *dc_runtime_registration_id(const dc_runtime *r) {
    return r ? r->reg_id : NULL;
}
