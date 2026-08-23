#ifndef KILIX_PLAYALONG_KPA_JSON_H
#define KILIX_PLAYALONG_KPA_JSON_H

/*
 * Bounded read-only JSON for project documents.
 *
 * Every limit is fixed at parse time and every node lives in one caller-owned
 * arena, so a malformed or hostile document costs a bounded amount of memory
 * and no allocation at all after kpa_json_parse returns.  The parser accepts
 * the subset RFC 8259 defines and nothing else: no comments, no trailing
 * commas, no NaN or Infinity, no duplicate object keys, and no unpaired
 * surrogates.  Input must be valid UTF-8.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define KPA_JSON_MAX_DEPTH 32u

typedef enum kpa_json_type {
    KPA_JSON_NULL = 0,
    KPA_JSON_BOOL = 1,
    KPA_JSON_NUMBER = 2,
    KPA_JSON_STRING = 3,
    KPA_JSON_ARRAY = 4,
    KPA_JSON_OBJECT = 5
} kpa_json_type;

typedef enum kpa_json_result {
    KPA_JSON_OK = 0,
    KPA_JSON_INVALID_ARGUMENT = 1,
    KPA_JSON_SYNTAX = 2,
    KPA_JSON_DEPTH = 3,
    KPA_JSON_NO_SPACE = 4,      /* arena exhausted */
    KPA_JSON_UTF8 = 5,
    KPA_JSON_DUPLICATE_KEY = 6,
    KPA_JSON_RANGE = 7          /* a number the double type cannot hold */
} kpa_json_result;

/*
 * A node indexes into the arena rather than pointing into it, so an arena may
 * be moved or copied wholesale.  Index 0 is always the document root and is
 * never a child, which lets 0 double as "absent" in the accessors below.
 */
typedef uint32_t kpa_json_ref;

typedef struct kpa_json_node {
    kpa_json_type type;
    /* Strings borrow the caller's input buffer; they are not copied and are
     * not NUL terminated.  Escapes are decoded into the arena's scratch tail,
     * so a string with no escape costs nothing. */
    const char *text;
    size_t length;
    double number;
    bool boolean;
    /* Objects and arrays: first child, and for object members the key.  A
     * sibling chain avoids a second index array. */
    kpa_json_ref first_child;
    kpa_json_ref next_sibling;
    const char *key;
    size_t key_length;
    uint32_t child_count;
} kpa_json_node;

typedef struct kpa_json_document {
    kpa_json_node *nodes;
    uint32_t node_count;
    uint32_t node_capacity;
    char *scratch;
    size_t scratch_used;
    size_t scratch_capacity;
} kpa_json_document;

/*
 * The caller owns both buffers for the document's whole lifetime, as does it
 * own `text`.  Nothing here is freed and nothing here allocates.
 */
void kpa_json_document_init(kpa_json_document *document,
                            kpa_json_node *nodes, uint32_t node_capacity,
                            char *scratch, size_t scratch_capacity);

kpa_json_result kpa_json_parse(kpa_json_document *document,
                               const char *text, size_t length);

/* Human-readable name for logs and test failure messages. */
const char *kpa_json_result_name(kpa_json_result result);

const kpa_json_node *kpa_json_root(const kpa_json_document *document);
const kpa_json_node *kpa_json_at(const kpa_json_document *document,
                                 kpa_json_ref ref);

/* Object member by key; NULL when absent or when node is not an object. */
const kpa_json_node *kpa_json_member(const kpa_json_document *document,
                                     const kpa_json_node *node,
                                     const char *key);
/* Array element by index; NULL when out of range or not an array. */
const kpa_json_node *kpa_json_element(const kpa_json_document *document,
                                      const kpa_json_node *node,
                                      uint32_t index);

/*
 * Typed readers.  Each returns false and leaves *out untouched when the member
 * is absent or has the wrong type, so a caller can distinguish "missing" from
 * "present and zero" without inspecting the node.
 */
bool kpa_json_string(const kpa_json_document *document,
                     const kpa_json_node *node, const char *key,
                     const char **out, size_t *out_length);
bool kpa_json_string_copy(const kpa_json_document *document,
                          const kpa_json_node *node, const char *key,
                          char *out, size_t out_size);
bool kpa_json_number(const kpa_json_document *document,
                     const kpa_json_node *node, const char *key, double *out);
bool kpa_json_bool(const kpa_json_document *document,
                   const kpa_json_node *node, const char *key, bool *out);

/* True when the node is a string equal to `value` byte for byte. */
bool kpa_json_string_equals(const kpa_json_node *node, const char *value);

#ifdef __cplusplus
}
#endif

#endif
