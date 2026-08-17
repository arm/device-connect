/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * dc/driver.c -- Device Connect C edge SDK driver implementation.
 *
 * ASCII-only source.
 */

#include "dc/driver.h"
#include "dc/jsonrpc.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    char *name;
    char *description;
    json *params_schema; /* owned or NULL */
    dc_rpc_fn fn;
    void *user;
} fn_entry;

typedef struct {
    char *name;
    char *description;
} ev_entry;

struct dc_driver {
    char *device_type;
    char *manufacturer;
    char *model;
    char *firmware_version;
    char *description;
    char *location;
    char *availability; /* default "available" */
    fn_entry *fns;
    size_t nfns, cfns;
    ev_entry *evs;
    size_t nevs, cevs;
};

static char *dupz(const char *s) { return s != NULL ? strdup(s) : NULL; }

dc_driver *dc_driver_new(const char *device_type) {
    dc_driver *d = (dc_driver *)calloc(1, sizeof(*d));
    if (d == NULL) {
        return NULL;
    }
    d->device_type = dupz(device_type != NULL ? device_type : "device");
    d->availability = strdup("available");
    if (d->device_type == NULL || d->availability == NULL) {
        dc_driver_free(d);
        return NULL;
    }
    return d;
}

void dc_driver_free(dc_driver *d) {
    if (d == NULL) {
        return;
    }
    for (size_t i = 0; i < d->nfns; i++) {
        free(d->fns[i].name);
        free(d->fns[i].description);
        json_free(d->fns[i].params_schema);
    }
    free(d->fns);
    for (size_t i = 0; i < d->nevs; i++) {
        free(d->evs[i].name);
        free(d->evs[i].description);
    }
    free(d->evs);
    free(d->device_type);
    free(d->manufacturer);
    free(d->model);
    free(d->firmware_version);
    free(d->description);
    free(d->location);
    free(d->availability);
    free(d);
}

int dc_driver_add_function(dc_driver *d, const char *name,
                           const char *description, json *params_schema,
                           dc_rpc_fn fn, void *user) {
    if (d == NULL || name == NULL || fn == NULL) {
        json_free(params_schema);
        return -1;
    }
    if (d->nfns == d->cfns) {
        size_t nc = d->cfns ? d->cfns * 2 : 8;
        fn_entry *ne = (fn_entry *)realloc(d->fns, nc * sizeof(fn_entry));
        if (ne == NULL) {
            json_free(params_schema);
            return -1;
        }
        d->fns = ne;
        d->cfns = nc;
    }
    fn_entry *e = &d->fns[d->nfns];
    e->name = strdup(name);
    e->description = dupz(description);
    e->params_schema = params_schema;
    e->fn = fn;
    e->user = user;
    if (e->name == NULL) {
        free(e->description);
        json_free(params_schema);
        return -1;
    }
    d->nfns++;
    return 0;
}

int dc_driver_add_event(dc_driver *d, const char *name,
                        const char *description) {
    if (d == NULL || name == NULL) {
        return -1;
    }
    if (d->nevs == d->cevs) {
        size_t nc = d->cevs ? d->cevs * 2 : 4;
        ev_entry *ne = (ev_entry *)realloc(d->evs, nc * sizeof(ev_entry));
        if (ne == NULL) {
            return -1;
        }
        d->evs = ne;
        d->cevs = nc;
    }
    d->evs[d->nevs].name = strdup(name);
    d->evs[d->nevs].description = dupz(description);
    if (d->evs[d->nevs].name == NULL) {
        return -1;
    }
    d->nevs++;
    return 0;
}

static void set_field(char **slot, const char *v) {
    if (v == NULL) {
        return;
    }
    free(*slot);
    *slot = strdup(v);
}

void dc_driver_set_identity(dc_driver *d, const char *manufacturer,
                            const char *model, const char *firmware_version,
                            const char *description) {
    if (d == NULL) {
        return;
    }
    set_field(&d->manufacturer, manufacturer);
    set_field(&d->model, model);
    set_field(&d->firmware_version, firmware_version);
    set_field(&d->description, description);
}

void dc_driver_set_location(dc_driver *d, const char *location) {
    if (d != NULL) {
        set_field(&d->location, location);
    }
}

void dc_driver_set_availability(dc_driver *d, const char *availability) {
    if (d != NULL) {
        set_field(&d->availability, availability);
    }
}

const char *dc_driver_device_type(const dc_driver *d) {
    return d != NULL ? d->device_type : NULL;
}

json *dc_driver_capabilities(const dc_driver *d) {
    if (d == NULL) {
        return NULL;
    }
    json *caps = json_object();
    json *fns = json_array();
    json *evs = json_array();
    if (caps == NULL || fns == NULL || evs == NULL) {
        json_free(caps);
        json_free(fns);
        json_free(evs);
        return NULL;
    }
    for (size_t i = 0; i < d->nfns; i++) {
        json *f = json_object();
        json *schema = (d->fns[i].params_schema != NULL)
                           ? json_clone(d->fns[i].params_schema)
                           : json_object();
        if (f == NULL || schema == NULL) {
            json_free(f);
            json_free(schema);
            json_free(caps);
            json_free(fns);
            json_free(evs);
            return NULL;
        }
        json_object_set(f, "name", json_string(d->fns[i].name));
        json_object_set(f, "description",
                        json_string(d->fns[i].description
                                        ? d->fns[i].description
                                        : ""));
        json_object_set(f, "parameters", schema);
        json_object_set(f, "tags", json_array());
        json_array_append(fns, f);
    }
    for (size_t i = 0; i < d->nevs; i++) {
        json *e = json_object();
        if (e == NULL) {
            json_free(caps);
            json_free(fns);
            json_free(evs);
            return NULL;
        }
        json_object_set(e, "name", json_string(d->evs[i].name));
        json_object_set(e, "description",
                        json_string(d->evs[i].description
                                        ? d->evs[i].description
                                        : ""));
        json_array_append(evs, e);
    }
    json_object_set(caps, "description",
                    json_string(d->description ? d->description : ""));
    json_object_set(caps, "functions", fns);
    json_object_set(caps, "events", evs);
    return caps;
}

json *dc_driver_identity(const dc_driver *d) {
    if (d == NULL) {
        return NULL;
    }
    json *id = json_object();
    if (id == NULL) {
        return NULL;
    }
    json_object_set(id, "device_type", json_string(d->device_type));
    if (d->manufacturer) {
        json_object_set(id, "manufacturer", json_string(d->manufacturer));
    }
    if (d->model) {
        json_object_set(id, "model", json_string(d->model));
    }
    if (d->firmware_version) {
        json_object_set(id, "firmware_version",
                        json_string(d->firmware_version));
    }
    if (d->description) {
        json_object_set(id, "description", json_string(d->description));
    }
    return id;
}

json *dc_driver_status(const dc_driver *d) {
    if (d == NULL) {
        return NULL;
    }
    json *st = json_object();
    if (st == NULL) {
        return NULL;
    }
    json_object_set(st, "availability", json_string(d->availability));
    json_object_set(st, "online", json_bool(1));
    if (d->location) {
        json_object_set(st, "location", json_string(d->location));
    }
    json_object_set(st, "busy_score", json_real(0.0));
    return st;
}

static const fn_entry *find_fn(const dc_driver *d, const char *name) {
    for (size_t i = 0; i < d->nfns; i++) {
        if (strcmp(d->fns[i].name, name) == 0) {
            return &d->fns[i];
        }
    }
    return NULL;
}

int dc_driver_call(const dc_driver *d, const char *function, const json *params,
                   json **result_out, char *errmsg, size_t errcap) {
    if (result_out != NULL) {
        *result_out = NULL;
    }
    if (d == NULL || function == NULL) {
        return DC_ERR_INVALID_PARAMS;
    }
    const fn_entry *e = find_fn(d, function);
    if (e == NULL) {
        if (errmsg) {
            snprintf(errmsg, errcap, "no such function: %s", function);
        }
        return DC_ERR_METHOD_NF;
    }
    dc_rpc_result out;
    memset(&out, 0, sizeof(out));
    e->fn(e->user, params, &out);
    if (out.err_code != 0) {
        if (errmsg) {
            snprintf(errmsg, errcap, "%s",
                     out.err_msg[0] ? out.err_msg : "function error");
        }
        json_free(out.result);
        return out.err_code;
    }
    if (result_out != NULL) {
        *result_out = out.result;
    } else {
        json_free(out.result);
    }
    return 0;
}
