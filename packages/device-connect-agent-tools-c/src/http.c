/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * dc/http.c -- libcurl wrapper for the portal HTTP API.
 *
 * ASCII-only source.
 */

#include "dc/http.h"

#include <curl/curl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    char *buf;
    size_t len;
    size_t cap;
} growbuf;

static size_t write_cb(char *ptr, size_t size, size_t nmemb, void *userdata) {
    size_t n = size * nmemb;
    growbuf *g = (growbuf *)userdata;
    if (g->len + n + 1 > g->cap) {
        size_t nc = g->cap ? g->cap : 256;
        while (g->len + n + 1 > nc) {
            nc *= 2;
        }
        char *nb = (char *)realloc(g->buf, nc);
        if (nb == NULL) {
            return 0;
        }
        g->buf = nb;
        g->cap = nc;
    }
    memcpy(g->buf + g->len, ptr, n);
    g->len += n;
    g->buf[g->len] = '\0';
    return n;
}

int dc_http_global_init(void) {
    return curl_global_init(CURL_GLOBAL_DEFAULT) == CURLE_OK ? 0 : -1;
}

void dc_http_global_cleanup(void) { curl_global_cleanup(); }

static int do_request(const char *url, const char *token, const char *body,
                      dc_http_response *resp) {
    if (resp == NULL) {
        return -1;
    }
    resp->status = 0;
    resp->body = NULL;
    resp->len = 0;

    CURL *curl = curl_easy_init();
    if (curl == NULL) {
        return -1;
    }
    growbuf g = {NULL, 0, 0};
    struct curl_slist *hdrs = NULL;
    char auth[1024];
    if (token != NULL) {
        snprintf(auth, sizeof(auth), "Authorization: Bearer %s", token);
        hdrs = curl_slist_append(hdrs, auth);
    }
    if (body != NULL) {
        hdrs = curl_slist_append(hdrs, "Content-Type: application/json");
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body);
    }
    curl_easy_setopt(curl, CURLOPT_URL, url);
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, hdrs);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &g);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 30L);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "dc-agent-tools-c/0.1");

    CURLcode rc = curl_easy_perform(curl);
    int ret = -1;
    if (rc == CURLE_OK) {
        long code = 0;
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &code);
        resp->status = code;
        resp->body = g.buf;
        resp->len = g.len;
        g.buf = NULL;
        ret = 0;
    } else {
        fprintf(stderr, "[dc-agent] http: %s\n", curl_easy_strerror(rc));
    }
    free(g.buf);
    curl_slist_free_all(hdrs);
    curl_easy_cleanup(curl);
    return ret;
}

int dc_http_get(const char *url, const char *token, dc_http_response *resp) {
    return do_request(url, token, NULL, resp);
}

int dc_http_post(const char *url, const char *token, const char *json_body,
                 dc_http_response *resp) {
    return do_request(url, token, json_body, resp);
}

void dc_http_response_free(dc_http_response *resp) {
    if (resp != NULL) {
        free(resp->body);
        resp->body = NULL;
        resp->len = 0;
    }
}
