/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * dc/transport_nats.c -- NATS backend (cnats).
 *
 * Built only when DC_WITH_NATS is defined; otherwise a stub. cnats is a
 * fresh from-scratch integration; if any logic is later ported from another
 * implementation, flag the file for co-attribution review (see PR #189).
 *
 * ASCII-only source (per CLAUDE.md).
 */

#include "dc/transport_nats.h"

#include <stdio.h>

#ifndef DC_WITH_NATS

/* ---- stub: library was built without the NATS C client ---- */
int dc_transport_nats_create(dc_transport *t,
                                   const dc_nats_config *cfg) {
    (void)t;
    (void)cfg;
    fprintf(stderr,
            "[dc-edge] NATS transport unavailable: rebuild with "
            "DC_WITH_NATS=1 and the NATS C client installed.\n");
    return -1;
}

int dc_transport_nats_poll_reconnect(dc_transport *t,
                                           double *outage_s) {
    (void)t;
    (void)outage_s;
    return 0;
}

#else /* DC_WITH_NATS */


#include <nats/nats.h>
#include <stdlib.h>
#include <string.h>

/* per-subscription bridge from the cnats handler to dc_msg_cb */
typedef struct sub_bridge {
    dc_msg_cb cb;
    void *user;
    natsSubscription *sub;
    struct sub_bridge *next;
} sub_bridge;

typedef struct {
    natsConnection *conn;
    natsOptions *opts;
    sub_bridge *bridges;
    /* reconnect tracking, written by cnats callback threads */
    volatile int reconnect_pending;
    volatile double disconnect_ts;
    volatile double last_outage;
    int connected_flag;
} nats_impl;

static void on_disconnected(natsConnection *nc, void *closure) {
    (void)nc;
    nats_impl *im = (nats_impl *)closure;
    im->disconnect_ts = dc_now();
    im->connected_flag = 0;
}

static void on_reconnected(natsConnection *nc, void *closure) {
    (void)nc;
    nats_impl *im = (nats_impl *)closure;
    double now = dc_now();
    im->last_outage = (im->disconnect_ts > 0.0) ? (now - im->disconnect_ts)
                                                 : 0.0;
    im->reconnect_pending = 1;
    im->connected_flag = 1;
}

static void on_closed(natsConnection *nc, void *closure) {
    (void)nc;
    nats_impl *im = (nats_impl *)closure;
    im->connected_flag = 0;
}

static void msg_handler(natsConnection *nc, natsSubscription *sub,
                        natsMsg *msg, void *closure) {
    (void)nc;
    (void)sub;
    sub_bridge *b = (sub_bridge *)closure;
    const char *reply = natsMsg_GetReply(msg); /* NULL if none */
    b->cb(b->user, natsMsg_GetSubject(msg),
          (const uint8_t *)natsMsg_GetData(msg),
          (size_t)natsMsg_GetDataLength(msg), reply);
    natsMsg_Destroy(msg);
}

static int nats_publish(void *impl, const char *subject, const uint8_t *data,
                        size_t len) {
    nats_impl *im = (nats_impl *)impl;
    natsStatus s = natsConnection_Publish(im->conn, subject, data, (int)len);
    if (s != NATS_OK) {
        return -1;
    }
    /* Flush so low-rate fire-and-forget publishes (presence, heartbeat) leave
     * the client buffer promptly rather than waiting on the flusher; otherwise
     * a registry can miss heartbeats and reap an otherwise-live device. */
    natsConnection_Flush(im->conn);
    return 0;
}

static int nats_subscribe(void *impl, const char *pattern, dc_msg_cb cb,
                          void *user) {
    nats_impl *im = (nats_impl *)impl;
    sub_bridge *b = (sub_bridge *)calloc(1, sizeof(*b));
    if (b == NULL) {
        return -1;
    }
    b->cb = cb;
    b->user = user;
    natsStatus s =
        natsConnection_Subscribe(&b->sub, im->conn, pattern, msg_handler, b);
    if (s != NATS_OK) {
        free(b);
        return -1;
    }
    b->next = im->bridges;
    im->bridges = b;
    return 0;
}

static int nats_request(void *impl, const char *subject, const uint8_t *data,
                        size_t len, uint8_t **out, size_t *out_len,
                        int timeout_ms) {
    nats_impl *im = (nats_impl *)impl;
    natsMsg *reply = NULL;
    natsStatus s = natsConnection_Request(&reply, im->conn, subject, data,
                                          (int)len, timeout_ms);
    if (s != NATS_OK || reply == NULL) {
        return -1;
    }
    int dlen = natsMsg_GetDataLength(reply);
    uint8_t *buf = (uint8_t *)malloc(dlen > 0 ? (size_t)dlen : 1);
    if (buf == NULL) {
        natsMsg_Destroy(reply);
        return -1;
    }
    if (dlen > 0) {
        memcpy(buf, natsMsg_GetData(reply), (size_t)dlen);
    }
    natsMsg_Destroy(reply);
    *out = buf;
    *out_len = (size_t)dlen;
    return 0;
}

static int nats_respond(void *impl, const char *reply, const uint8_t *data,
                        size_t len) {
    return nats_publish(impl, reply, data, len);
}

static int nats_connected(void *impl) {
    return ((nats_impl *)impl)->connected_flag;
}

static void nats_close(void *impl) {
    nats_impl *im = (nats_impl *)impl;
    if (im == NULL) {
        return;
    }
    sub_bridge *b = im->bridges;
    while (b != NULL) {
        sub_bridge *n = b->next;
        if (b->sub != NULL) {
            natsSubscription_Destroy(b->sub);
        }
        free(b);
        b = n;
    }
    if (im->conn != NULL) {
        natsConnection_Destroy(im->conn);
    }
    if (im->opts != NULL) {
        natsOptions_Destroy(im->opts);
    }
    free(im);
}

static int apply_security(natsOptions *opts, dc_security *sec) {
    if (sec == NULL) {
        return 0;
    }
    natsStatus s = NATS_OK;
    if (sec->tls_enable || sec->tls_ca != NULL) {
        s = natsOptions_SetSecure(opts, true);
        if (s != NATS_OK) {
            return -1;
        }
        if (sec->tls_ca != NULL) {
            s = natsOptions_LoadCATrustedCertificates(opts, sec->tls_ca);
            if (s != NATS_OK) {
                return -1;
            }
        }
        if (sec->tls_cert != NULL && sec->tls_key != NULL) {
            s = natsOptions_LoadCertificatesChain(opts, sec->tls_cert,
                                                  sec->tls_key);
            if (s != NATS_OK) {
                return -1;
            }
        }
        if (!sec->verify_hostname) {
            /* best-effort: skip hostname verification when explicitly off */
            natsOptions_SkipServerVerification(opts, true);
        }
    }
    if (sec->creds_file != NULL) {
        s = natsOptions_SetUserCredentialsFromFiles(opts, sec->creds_file,
                                                    NULL);
        if (s != NATS_OK) {
            return -1;
        }
    } else if (sec->jwt != NULL && sec->nkey_seed != NULL) {
        /* inline JWT + seed: cnats expects a chained .creds; for inline use a
         * seed file is the supported path. We surface a clear error so the
         * operator supplies NATS_CREDENTIALS_FILE instead. */
        fprintf(stderr,
                "[dc-edge] inline NATS_JWT+NATS_NKEY_SEED not wired; supply "
                "NATS_CREDENTIALS_FILE (a .creds file) instead.\n");
        return -1;
    }
    return 0;
}

int dc_transport_nats_create(dc_transport *t,
                                   const dc_nats_config *cfg) {
    if (t == NULL || cfg == NULL || cfg->url == NULL) {
        return -1;
    }
    nats_impl *im = (nats_impl *)calloc(1, sizeof(*im));
    if (im == NULL) {
        return -1;
    }
    natsStatus s = natsOptions_Create(&im->opts);
    if (s != NATS_OK) {
        free(im);
        return -1;
    }
    int wait = (cfg->reconnect_wait_ms > 0) ? cfg->reconnect_wait_ms : 250;
    int jitter =
        (cfg->reconnect_jitter_ms > 0) ? cfg->reconnect_jitter_ms : 250;
    natsOptions_SetURL(im->opts, cfg->url);
    natsOptions_SetMaxReconnect(im->opts, -1); /* unlimited */
    natsOptions_SetReconnectWait(im->opts, wait);
    natsOptions_SetReconnectJitter(im->opts, jitter, jitter);
    natsOptions_SetDisconnectedCB(im->opts, on_disconnected, im);
    natsOptions_SetReconnectedCB(im->opts, on_reconnected, im);
    natsOptions_SetClosedCB(im->opts, on_closed, im);
    if (apply_security(im->opts, cfg->security) != 0) {
        natsOptions_Destroy(im->opts);
        free(im);
        return -1;
    }
    s = natsConnection_Connect(&im->conn, im->opts);
    if (s != NATS_OK) {
        natsOptions_Destroy(im->opts);
        free(im);
        return -1;
    }
    im->connected_flag = 1;

    t->impl = im;
    t->publish = nats_publish;
    t->subscribe = nats_subscribe;
    t->request = nats_request;
    t->respond = nats_respond;
    t->connected = nats_connected;
    t->close = nats_close;
    return 0;
}

int dc_transport_nats_poll_reconnect(dc_transport *t,
                                           double *outage_s) {
    if (t == NULL || t->impl == NULL || t->publish != nats_publish) {
        return 0; /* not a NATS transport */
    }
    nats_impl *im = (nats_impl *)t->impl;
    if (im->reconnect_pending) {
        im->reconnect_pending = 0;
        if (outage_s != NULL) {
            *outage_s = im->last_outage;
        }
        return 1;
    }
    return 0;
}

#endif /* DC_WITH_NATS */
