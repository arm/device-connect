/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 *
 * dc/json.h -- a tiny, dependency-free JSON reader/writer for the MHP
 * reference wire node.
 *
 * Design goals (per the MHP wire contract, specification/draft/wire_contract.md):
 *   - Serialize with optional lexicographically sorted object keys, so the
 *     manifest hash (section 7.6) is deterministic.
 *   - Never emit NaN / Infinity tokens: non-finite reals serialize as null
 *     (section 4.4, conformance clause C20).
 *   - Tolerate unknown members on parse so higher layers can ignore extra
 *     fields (clause C19).
 *
 * Numbers are stored as either a 64-bit integer or a double, chosen at parse
 * time by whether the token carried a '.', 'e' or 'E'. Constructed numbers
 * pick the form the caller asked for.
 *
 * Ownership: container mutators (json_array_append, json_object_set) take
 * ownership of the value passed in; json_free frees a value and everything it
 * transitively owns. Strings returned by accessors are owned by the node and
 * are valid until it is freed.
 *
 * ASCII-only source (per CLAUDE.md).
 */

#ifndef DC_JSON_H
#define DC_JSON_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    JSON_NULL = 0,
    JSON_BOOL,
    JSON_INT,
    JSON_REAL,
    JSON_STRING,
    JSON_ARRAY,
    JSON_OBJECT
} json_type;

typedef struct json json;

/* ---- constructors (all return a heap node, or NULL on OOM) ---- */
json *json_null(void);
json *json_bool(int b);
json *json_int(int64_t v);
json *json_real(double v);
json *json_string(const char *s);             /* NUL-terminated, copied */
json *json_string_n(const char *s, size_t n); /* n bytes, copied */
json *json_array(void);
json *json_object(void);

/* ---- container mutators (take ownership of val; key is copied) ---- */
int json_array_append(json *arr, json *val);              /* 0 ok, -1 fail */
int json_object_set(json *obj, const char *key, json *val); /* replaces dup key */

/* ---- type tests / accessors ---- */
json_type json_typeof(const json *j);
int json_is_null(const json *j);

/* number value as double regardless of int/real storage; 0 if not a number */
double json_number(const json *j);
/* integer value; for a real, the truncated value; 0 if not a number */
int64_t json_integer(const json *j);
int json_truthy(const json *j); /* JSON_BOOL value, or 0 */

/* string bytes (NUL-terminated) and length; NULL/0 if not a string */
const char *json_str(const json *j);
size_t json_strlen(const json *j);

size_t json_array_size(const json *j);
json *json_array_get(const json *j, size_t i); /* borrowed */

json *json_object_get(const json *j, const char *key); /* borrowed, or NULL */
size_t json_object_size(const json *j);
/* iterate object members by index; key/val borrowed */
const char *json_object_key_at(const json *j, size_t i);
json *json_object_val_at(const json *j, size_t i);

/* Deep-copy a value (and everything it owns). NULL on OOM or NULL input. */
json *json_clone(const json *j);

/* ---- parse / serialize / free ---- */
/*
 * Parse exactly one JSON value from buf[0..len). Trailing whitespace is
 * allowed; trailing non-whitespace is an error. Returns NULL on any syntax
 * error. If err is non-NULL it receives a short static description (do not
 * free).
 */
json *json_parse(const char *buf, size_t len, const char **err);

/*
 * Serialize to a freshly malloc'd NUL-terminated ASCII string (caller frees).
 * If sorted is non-zero, object members are emitted in ascending key order.
 * Returns NULL on OOM. *out_len, when non-NULL, receives strlen of the result.
 */
char *json_dumps(const json *j, int sorted, size_t *out_len);

void json_free(json *j);

#ifdef __cplusplus
}
#endif

#endif /* DC_JSON_H */
