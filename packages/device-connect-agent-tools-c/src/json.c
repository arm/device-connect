/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * dc/json.c -- implementation of the tiny JSON reader/writer.
 *
 * ASCII-only source (per CLAUDE.md).
 */

#include "dc/json.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct json {
    json_type type;
    union {
        int boolean;
        int64_t inum;
        double rnum;
        struct {
            char *bytes;
            size_t len;
        } str;
        struct {
            json **items;
            size_t count;
            size_t cap;
        } arr;
        struct {
            char **keys;
            json **vals;
            size_t count;
            size_t cap;
        } obj;
    } u;
};

/* ------------------------------------------------------------------ */
/* constructors                                                        */
/* ------------------------------------------------------------------ */

static json *node_new(json_type t) {
    json *j = (json *)calloc(1, sizeof(*j));
    if (j != NULL) {
        j->type = t;
    }
    return j;
}

json *json_null(void) { return node_new(JSON_NULL); }

json *json_bool(int b) {
    json *j = node_new(JSON_BOOL);
    if (j != NULL) {
        j->u.boolean = b ? 1 : 0;
    }
    return j;
}

json *json_int(int64_t v) {
    json *j = node_new(JSON_INT);
    if (j != NULL) {
        j->u.inum = v;
    }
    return j;
}

json *json_real(double v) {
    json *j = node_new(JSON_REAL);
    if (j != NULL) {
        j->u.rnum = v;
    }
    return j;
}

json *json_string_n(const char *s, size_t n) {
    json *j = node_new(JSON_STRING);
    if (j == NULL) {
        return NULL;
    }
    j->u.str.bytes = (char *)malloc(n + 1);
    if (j->u.str.bytes == NULL) {
        free(j);
        return NULL;
    }
    if (n > 0 && s != NULL) {
        memcpy(j->u.str.bytes, s, n);
    }
    j->u.str.bytes[n] = '\0';
    j->u.str.len = n;
    return j;
}

json *json_string(const char *s) {
    return json_string_n(s, s != NULL ? strlen(s) : 0);
}

json *json_array(void) { return node_new(JSON_ARRAY); }
json *json_object(void) { return node_new(JSON_OBJECT); }

/* ------------------------------------------------------------------ */
/* container mutators                                                  */
/* ------------------------------------------------------------------ */

static int grow_ptrs(void ***p, size_t *cap, size_t need) {
    if (*cap >= need) {
        return 0;
    }
    size_t ncap = (*cap == 0) ? 4 : (*cap * 2);
    while (ncap < need) {
        ncap *= 2;
    }
    void **np = (void **)realloc(*p, ncap * sizeof(void *));
    if (np == NULL) {
        return -1;
    }
    *p = np;
    *cap = ncap;
    return 0;
}

int json_array_append(json *arr, json *val) {
    if (arr == NULL || arr->type != JSON_ARRAY || val == NULL) {
        json_free(val);
        return -1;
    }
    if (grow_ptrs((void ***)&arr->u.arr.items, &arr->u.arr.cap,
                  arr->u.arr.count + 1) != 0) {
        json_free(val);
        return -1;
    }
    arr->u.arr.items[arr->u.arr.count++] = val;
    return 0;
}

int json_object_set(json *obj, const char *key, json *val) {
    if (obj == NULL || obj->type != JSON_OBJECT || key == NULL || val == NULL) {
        json_free(val);
        return -1;
    }
    /* replace existing key, preserving slot */
    for (size_t i = 0; i < obj->u.obj.count; i++) {
        if (strcmp(obj->u.obj.keys[i], key) == 0) {
            json_free(obj->u.obj.vals[i]);
            obj->u.obj.vals[i] = val;
            return 0;
        }
    }
    if (grow_ptrs((void ***)&obj->u.obj.keys, &obj->u.obj.cap,
                  obj->u.obj.count + 1) != 0) {
        json_free(val);
        return -1;
    }
    /* keys and vals grow in lockstep; vals cap tracked via keys cap */
    json **nv = (json **)realloc(obj->u.obj.vals,
                                 obj->u.obj.cap * sizeof(json *));
    if (nv == NULL) {
        json_free(val);
        return -1;
    }
    obj->u.obj.vals = nv;
    char *kc = (char *)malloc(strlen(key) + 1);
    if (kc == NULL) {
        json_free(val);
        return -1;
    }
    strcpy(kc, key);
    obj->u.obj.keys[obj->u.obj.count] = kc;
    obj->u.obj.vals[obj->u.obj.count] = val;
    obj->u.obj.count++;
    return 0;
}

/* ------------------------------------------------------------------ */
/* accessors                                                           */
/* ------------------------------------------------------------------ */

json_type json_typeof(const json *j) { return j != NULL ? j->type : JSON_NULL; }

int json_is_null(const json *j) { return j == NULL || j->type == JSON_NULL; }

double json_number(const json *j) {
    if (j == NULL) {
        return 0.0;
    }
    if (j->type == JSON_INT) {
        return (double)j->u.inum;
    }
    if (j->type == JSON_REAL) {
        return j->u.rnum;
    }
    return 0.0;
}

int64_t json_integer(const json *j) {
    if (j == NULL) {
        return 0;
    }
    if (j->type == JSON_INT) {
        return j->u.inum;
    }
    if (j->type == JSON_REAL) {
        return (int64_t)j->u.rnum;
    }
    return 0;
}

int json_truthy(const json *j) {
    return (j != NULL && j->type == JSON_BOOL) ? j->u.boolean : 0;
}

const char *json_str(const json *j) {
    return (j != NULL && j->type == JSON_STRING) ? j->u.str.bytes : NULL;
}

size_t json_strlen(const json *j) {
    return (j != NULL && j->type == JSON_STRING) ? j->u.str.len : 0;
}

size_t json_array_size(const json *j) {
    return (j != NULL && j->type == JSON_ARRAY) ? j->u.arr.count : 0;
}

json *json_array_get(const json *j, size_t i) {
    if (j == NULL || j->type != JSON_ARRAY || i >= j->u.arr.count) {
        return NULL;
    }
    return j->u.arr.items[i];
}

json *json_object_get(const json *j, const char *key) {
    if (j == NULL || j->type != JSON_OBJECT || key == NULL) {
        return NULL;
    }
    for (size_t i = 0; i < j->u.obj.count; i++) {
        if (strcmp(j->u.obj.keys[i], key) == 0) {
            return j->u.obj.vals[i];
        }
    }
    return NULL;
}

size_t json_object_size(const json *j) {
    return (j != NULL && j->type == JSON_OBJECT) ? j->u.obj.count : 0;
}

const char *json_object_key_at(const json *j, size_t i) {
    if (j == NULL || j->type != JSON_OBJECT || i >= j->u.obj.count) {
        return NULL;
    }
    return j->u.obj.keys[i];
}

json *json_object_val_at(const json *j, size_t i) {
    if (j == NULL || j->type != JSON_OBJECT || i >= j->u.obj.count) {
        return NULL;
    }
    return j->u.obj.vals[i];
}

json *json_clone(const json *j) {
    if (j == NULL) {
        return NULL;
    }
    switch (j->type) {
    case JSON_NULL:
        return json_null();
    case JSON_BOOL:
        return json_bool(j->u.boolean);
    case JSON_INT:
        return json_int(j->u.inum);
    case JSON_REAL:
        return json_real(j->u.rnum);
    case JSON_STRING:
        return json_string_n(j->u.str.bytes, j->u.str.len);
    case JSON_ARRAY: {
        json *a = json_array();
        if (a == NULL) {
            return NULL;
        }
        for (size_t i = 0; i < j->u.arr.count; i++) {
            json *c = json_clone(j->u.arr.items[i]);
            if (c == NULL || json_array_append(a, c) != 0) {
                json_free(a);
                return NULL;
            }
        }
        return a;
    }
    case JSON_OBJECT: {
        json *o = json_object();
        if (o == NULL) {
            return NULL;
        }
        for (size_t i = 0; i < j->u.obj.count; i++) {
            json *c = json_clone(j->u.obj.vals[i]);
            if (c == NULL || json_object_set(o, j->u.obj.keys[i], c) != 0) {
                json_free(o);
                return NULL;
            }
        }
        return o;
    }
    default:
        return NULL;
    }
}

/* ------------------------------------------------------------------ */
/* free                                                                */
/* ------------------------------------------------------------------ */

void json_free(json *j) {
    if (j == NULL) {
        return;
    }
    switch (j->type) {
    case JSON_STRING:
        free(j->u.str.bytes);
        break;
    case JSON_ARRAY:
        for (size_t i = 0; i < j->u.arr.count; i++) {
            json_free(j->u.arr.items[i]);
        }
        free(j->u.arr.items);
        break;
    case JSON_OBJECT:
        for (size_t i = 0; i < j->u.obj.count; i++) {
            free(j->u.obj.keys[i]);
            json_free(j->u.obj.vals[i]);
        }
        free(j->u.obj.keys);
        free(j->u.obj.vals);
        break;
    default:
        break;
    }
    free(j);
}

/* ------------------------------------------------------------------ */
/* growable output buffer                                              */
/* ------------------------------------------------------------------ */

typedef struct {
    char *buf;
    size_t len;
    size_t cap;
    int err;
} sbuf;

static void sbuf_reserve(sbuf *s, size_t extra) {
    if (s->err) {
        return;
    }
    if (s->len + extra + 1 <= s->cap) {
        return;
    }
    size_t ncap = (s->cap == 0) ? 64 : s->cap;
    while (s->len + extra + 1 > ncap) {
        ncap *= 2;
    }
    char *nb = (char *)realloc(s->buf, ncap);
    if (nb == NULL) {
        s->err = 1;
        return;
    }
    s->buf = nb;
    s->cap = ncap;
}

static void sbuf_putc(sbuf *s, char c) {
    sbuf_reserve(s, 1);
    if (s->err) {
        return;
    }
    s->buf[s->len++] = c;
}

static void sbuf_put(sbuf *s, const char *p, size_t n) {
    sbuf_reserve(s, n);
    if (s->err) {
        return;
    }
    memcpy(s->buf + s->len, p, n);
    s->len += n;
}

static void sbuf_puts(sbuf *s, const char *p) { sbuf_put(s, p, strlen(p)); }

/* ------------------------------------------------------------------ */
/* serialization                                                       */
/* ------------------------------------------------------------------ */

static void dump_string(sbuf *s, const char *p, size_t n) {
    static const char hexd[] = "0123456789abcdef";
    sbuf_putc(s, '"');
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)p[i];
        switch (c) {
        case '"':
            sbuf_put(s, "\\\"", 2);
            break;
        case '\\':
            sbuf_put(s, "\\\\", 2);
            break;
        case '\b':
            sbuf_put(s, "\\b", 2);
            break;
        case '\f':
            sbuf_put(s, "\\f", 2);
            break;
        case '\n':
            sbuf_put(s, "\\n", 2);
            break;
        case '\r':
            sbuf_put(s, "\\r", 2);
            break;
        case '\t':
            sbuf_put(s, "\\t", 2);
            break;
        default:
            if (c < 0x20) {
                char esc[6];
                esc[0] = '\\';
                esc[1] = 'u';
                esc[2] = '0';
                esc[3] = '0';
                esc[4] = hexd[(c >> 4) & 0xf];
                esc[5] = hexd[c & 0xf];
                sbuf_put(s, esc, 6);
            } else {
                /* bytes >= 0x20, including UTF-8 continuation bytes, pass
                 * through verbatim (already valid JSON). */
                sbuf_putc(s, (char)c);
            }
            break;
        }
    }
    sbuf_putc(s, '"');
}

static void dump_real(sbuf *s, double v) {
    if (!isfinite(v)) {
        /* C20 / section 4.4: never emit NaN or Infinity. */
        sbuf_puts(s, "null");
        return;
    }
    char tmp[40];
    /* %.17g round-trips an IEEE-754 double exactly. */
    int n = snprintf(tmp, sizeof(tmp), "%.17g", v);
    if (n < 0) {
        s->err = 1;
        return;
    }
    sbuf_put(s, tmp, (size_t)n);
}

/* index permutation for sorted object emission */
static int cmp_keys(const void *a, const void *b, const char **keys) {
    size_t ia = *(const size_t *)a;
    size_t ib = *(const size_t *)b;
    return strcmp(keys[ia], keys[ib]);
}

/* qsort_r is non-portable; do a simple insertion sort on the index array
 * (object sizes here are tiny -- a handful of members). */
static void sort_indices(size_t *idx, size_t n, char **keys) {
    for (size_t i = 1; i < n; i++) {
        size_t cur = idx[i];
        size_t j = i;
        while (j > 0 && strcmp(keys[idx[j - 1]], keys[cur]) > 0) {
            idx[j] = idx[j - 1];
            j--;
        }
        idx[j] = cur;
    }
    (void)cmp_keys;
}

static void dump_value(sbuf *s, const json *j, int sorted) {
    if (j == NULL) {
        sbuf_puts(s, "null");
        return;
    }
    switch (j->type) {
    case JSON_NULL:
        sbuf_puts(s, "null");
        break;
    case JSON_BOOL:
        sbuf_puts(s, j->u.boolean ? "true" : "false");
        break;
    case JSON_INT: {
        char tmp[32];
        int n = snprintf(tmp, sizeof(tmp), "%lld", (long long)j->u.inum);
        if (n < 0) {
            s->err = 1;
        } else {
            sbuf_put(s, tmp, (size_t)n);
        }
        break;
    }
    case JSON_REAL:
        dump_real(s, j->u.rnum);
        break;
    case JSON_STRING:
        dump_string(s, j->u.str.bytes, j->u.str.len);
        break;
    case JSON_ARRAY:
        sbuf_putc(s, '[');
        for (size_t i = 0; i < j->u.arr.count; i++) {
            if (i > 0) {
                sbuf_putc(s, ',');
            }
            dump_value(s, j->u.arr.items[i], sorted);
        }
        sbuf_putc(s, ']');
        break;
    case JSON_OBJECT: {
        sbuf_putc(s, '{');
        size_t n = j->u.obj.count;
        if (sorted && n > 1) {
            size_t stackidx[16];
            size_t *idx = stackidx;
            if (n > 16) {
                idx = (size_t *)malloc(n * sizeof(size_t));
                if (idx == NULL) {
                    s->err = 1;
                    break;
                }
            }
            for (size_t i = 0; i < n; i++) {
                idx[i] = i;
            }
            sort_indices(idx, n, j->u.obj.keys);
            for (size_t i = 0; i < n; i++) {
                if (i > 0) {
                    sbuf_putc(s, ',');
                }
                size_t k = idx[i];
                dump_string(s, j->u.obj.keys[k], strlen(j->u.obj.keys[k]));
                sbuf_putc(s, ':');
                dump_value(s, j->u.obj.vals[k], sorted);
            }
            if (idx != stackidx) {
                free(idx);
            }
        } else {
            for (size_t i = 0; i < n; i++) {
                if (i > 0) {
                    sbuf_putc(s, ',');
                }
                dump_string(s, j->u.obj.keys[i], strlen(j->u.obj.keys[i]));
                sbuf_putc(s, ':');
                dump_value(s, j->u.obj.vals[i], sorted);
            }
        }
        sbuf_putc(s, '}');
        break;
    }
    default:
        sbuf_puts(s, "null");
        break;
    }
}

char *json_dumps(const json *j, int sorted, size_t *out_len) {
    sbuf s;
    s.buf = NULL;
    s.len = 0;
    s.cap = 0;
    s.err = 0;
    dump_value(&s, j, sorted);
    if (s.err) {
        free(s.buf);
        return NULL;
    }
    sbuf_reserve(&s, 1);
    if (s.err) {
        free(s.buf);
        return NULL;
    }
    s.buf[s.len] = '\0';
    if (out_len != NULL) {
        *out_len = s.len;
    }
    return s.buf;
}

/* ------------------------------------------------------------------ */
/* parser                                                              */
/* ------------------------------------------------------------------ */

typedef struct {
    const char *p;
    const char *end;
    const char *err;
} pstate;

static void skip_ws(pstate *st) {
    while (st->p < st->end) {
        char c = *st->p;
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
            st->p++;
        } else {
            break;
        }
    }
}

static json *parse_value(pstate *st);

static int parse_hex4(pstate *st, unsigned *out) {
    if (st->end - st->p < 4) {
        return -1;
    }
    unsigned v = 0;
    for (int i = 0; i < 4; i++) {
        char c = st->p[i];
        v <<= 4;
        if (c >= '0' && c <= '9') {
            v |= (unsigned)(c - '0');
        } else if (c >= 'a' && c <= 'f') {
            v |= (unsigned)(c - 'a' + 10);
        } else if (c >= 'A' && c <= 'F') {
            v |= (unsigned)(c - 'A' + 10);
        } else {
            return -1;
        }
    }
    st->p += 4;
    *out = v;
    return 0;
}

static void utf8_encode(sbuf *s, unsigned cp) {
    if (cp < 0x80) {
        sbuf_putc(s, (char)cp);
    } else if (cp < 0x800) {
        sbuf_putc(s, (char)(0xC0 | (cp >> 6)));
        sbuf_putc(s, (char)(0x80 | (cp & 0x3F)));
    } else if (cp < 0x10000) {
        sbuf_putc(s, (char)(0xE0 | (cp >> 12)));
        sbuf_putc(s, (char)(0x80 | ((cp >> 6) & 0x3F)));
        sbuf_putc(s, (char)(0x80 | (cp & 0x3F)));
    } else {
        sbuf_putc(s, (char)(0xF0 | (cp >> 18)));
        sbuf_putc(s, (char)(0x80 | ((cp >> 12) & 0x3F)));
        sbuf_putc(s, (char)(0x80 | ((cp >> 6) & 0x3F)));
        sbuf_putc(s, (char)(0x80 | (cp & 0x3F)));
    }
}

/* parse a string token (leading '"' already consumed) into a sbuf */
static int parse_string_raw(pstate *st, sbuf *out) {
    while (st->p < st->end) {
        unsigned char c = (unsigned char)*st->p++;
        if (c == '"') {
            return 0;
        }
        if (c == '\\') {
            if (st->p >= st->end) {
                break;
            }
            char e = *st->p++;
            switch (e) {
            case '"':
                sbuf_putc(out, '"');
                break;
            case '\\':
                sbuf_putc(out, '\\');
                break;
            case '/':
                sbuf_putc(out, '/');
                break;
            case 'b':
                sbuf_putc(out, '\b');
                break;
            case 'f':
                sbuf_putc(out, '\f');
                break;
            case 'n':
                sbuf_putc(out, '\n');
                break;
            case 'r':
                sbuf_putc(out, '\r');
                break;
            case 't':
                sbuf_putc(out, '\t');
                break;
            case 'u': {
                unsigned cp = 0;
                if (parse_hex4(st, &cp) != 0) {
                    return -1;
                }
                if (cp >= 0xD800 && cp <= 0xDBFF) {
                    /* high surrogate; expect \uXXXX low surrogate */
                    if (st->end - st->p >= 2 && st->p[0] == '\\' &&
                        st->p[1] == 'u') {
                        st->p += 2;
                        unsigned lo = 0;
                        if (parse_hex4(st, &lo) != 0) {
                            return -1;
                        }
                        if (lo >= 0xDC00 && lo <= 0xDFFF) {
                            cp = 0x10000 + ((cp - 0xD800) << 10) +
                                 (lo - 0xDC00);
                        } else {
                            return -1;
                        }
                    } else {
                        return -1;
                    }
                }
                utf8_encode(out, cp);
                break;
            }
            default:
                return -1;
            }
        } else if (c < 0x20) {
            return -1; /* unescaped control char */
        } else {
            sbuf_putc(out, (char)c);
        }
        if (out->err) {
            return -1;
        }
    }
    return -1; /* unterminated */
}

static json *parse_string(pstate *st) {
    sbuf out;
    out.buf = NULL;
    out.len = 0;
    out.cap = 0;
    out.err = 0;
    if (parse_string_raw(st, &out) != 0) {
        free(out.buf);
        st->err = "bad string";
        return NULL;
    }
    json *j = json_string_n(out.buf != NULL ? out.buf : "", out.len);
    free(out.buf);
    if (j == NULL) {
        st->err = "oom";
    }
    return j;
}

static json *parse_number(pstate *st) {
    const char *start = st->p;
    int is_real = 0;
    if (st->p < st->end && *st->p == '-') {
        st->p++;
    }
    while (st->p < st->end) {
        char c = *st->p;
        if (c >= '0' && c <= '9') {
            st->p++;
        } else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
            is_real = 1;
            st->p++;
        } else {
            break;
        }
    }
    size_t n = (size_t)(st->p - start);
    if (n == 0) {
        st->err = "bad number";
        return NULL;
    }
    char tmp[64];
    if (n >= sizeof(tmp)) {
        is_real = 1; /* very long; treat as real */
    }
    char *buf = tmp;
    char *heap = NULL;
    if (n >= sizeof(tmp)) {
        heap = (char *)malloc(n + 1);
        if (heap == NULL) {
            st->err = "oom";
            return NULL;
        }
        buf = heap;
    }
    memcpy(buf, start, n);
    buf[n] = '\0';
    json *j;
    if (is_real) {
        j = json_real(strtod(buf, NULL));
    } else {
        long long v = strtoll(buf, NULL, 10);
        j = json_int((int64_t)v);
    }
    if (heap != NULL) {
        free(heap);
    }
    if (j == NULL) {
        st->err = "oom";
    }
    return j;
}

static int match_lit(pstate *st, const char *lit) {
    size_t n = strlen(lit);
    if ((size_t)(st->end - st->p) < n) {
        return -1;
    }
    if (memcmp(st->p, lit, n) != 0) {
        return -1;
    }
    st->p += n;
    return 0;
}

static json *parse_array(pstate *st) {
    json *arr = json_array();
    if (arr == NULL) {
        st->err = "oom";
        return NULL;
    }
    skip_ws(st);
    if (st->p < st->end && *st->p == ']') {
        st->p++;
        return arr;
    }
    for (;;) {
        json *v = parse_value(st);
        if (v == NULL) {
            json_free(arr);
            return NULL;
        }
        if (json_array_append(arr, v) != 0) {
            st->err = "oom";
            json_free(arr);
            return NULL;
        }
        skip_ws(st);
        if (st->p >= st->end) {
            st->err = "unterminated array";
            json_free(arr);
            return NULL;
        }
        char c = *st->p++;
        if (c == ',') {
            skip_ws(st);
            continue;
        }
        if (c == ']') {
            return arr;
        }
        st->err = "expected , or ]";
        json_free(arr);
        return NULL;
    }
}

static json *parse_object(pstate *st) {
    json *obj = json_object();
    if (obj == NULL) {
        st->err = "oom";
        return NULL;
    }
    skip_ws(st);
    if (st->p < st->end && *st->p == '}') {
        st->p++;
        return obj;
    }
    for (;;) {
        skip_ws(st);
        if (st->p >= st->end || *st->p != '"') {
            st->err = "expected object key";
            json_free(obj);
            return NULL;
        }
        st->p++; /* consume opening quote */
        sbuf key;
        key.buf = NULL;
        key.len = 0;
        key.cap = 0;
        key.err = 0;
        if (parse_string_raw(st, &key) != 0) {
            free(key.buf);
            st->err = "bad key";
            json_free(obj);
            return NULL;
        }
        skip_ws(st);
        if (st->p >= st->end || *st->p != ':') {
            free(key.buf);
            st->err = "expected :";
            json_free(obj);
            return NULL;
        }
        st->p++;
        json *v = parse_value(st);
        if (v == NULL) {
            free(key.buf);
            json_free(obj);
            return NULL;
        }
        if (json_object_set(obj, key.buf != NULL ? key.buf : "", v) != 0) {
            free(key.buf);
            st->err = "oom";
            json_free(obj);
            return NULL;
        }
        free(key.buf);
        skip_ws(st);
        if (st->p >= st->end) {
            st->err = "unterminated object";
            json_free(obj);
            return NULL;
        }
        char c = *st->p++;
        if (c == ',') {
            continue;
        }
        if (c == '}') {
            return obj;
        }
        st->err = "expected , or }";
        json_free(obj);
        return NULL;
    }
}

static json *parse_value(pstate *st) {
    skip_ws(st);
    if (st->p >= st->end) {
        st->err = "unexpected end";
        return NULL;
    }
    char c = *st->p;
    switch (c) {
    case '{':
        st->p++;
        return parse_object(st);
    case '[':
        st->p++;
        return parse_array(st);
    case '"':
        st->p++;
        return parse_string(st);
    case 't':
        if (match_lit(st, "true") == 0) {
            return json_bool(1);
        }
        st->err = "bad literal";
        return NULL;
    case 'f':
        if (match_lit(st, "false") == 0) {
            return json_bool(0);
        }
        st->err = "bad literal";
        return NULL;
    case 'n':
        if (match_lit(st, "null") == 0) {
            return json_null();
        }
        st->err = "bad literal";
        return NULL;
    default:
        if (c == '-' || (c >= '0' && c <= '9')) {
            return parse_number(st);
        }
        st->err = "unexpected token";
        return NULL;
    }
}

json *json_parse(const char *buf, size_t len, const char **err) {
    pstate st;
    st.p = buf;
    st.end = buf + len;
    st.err = NULL;
    json *j = parse_value(&st);
    if (j == NULL) {
        if (err != NULL) {
            *err = st.err != NULL ? st.err : "parse error";
        }
        return NULL;
    }
    skip_ws(&st);
    if (st.p != st.end) {
        if (err != NULL) {
            *err = "trailing data";
        }
        json_free(j);
        return NULL;
    }
    if (err != NULL) {
        *err = NULL;
    }
    return j;
}
