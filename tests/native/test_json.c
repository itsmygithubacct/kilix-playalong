/*
 * Tests for the bounded JSON reader.
 *
 * The number cases carry the exact bit pattern python3 produces for the same
 * literal, because "close enough" in a timestamp is a lyric on the wrong beat
 * and a locale-sensitive parser fails that way silently.  The rejection cases
 * are grouped by what an attacker would be trying to do with them rather than
 * by which line of the parser catches them.
 */

#include "kilix_playalong/kpa_json.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CHECK(condition)                                                      \
    do {                                                                      \
        if (!(condition)) {                                                   \
            (void)fprintf(stderr, "%s:%d: check failed: %s\n",                \
                          __FILE__, __LINE__, #condition);                    \
            return false;                                                     \
        }                                                                     \
    } while (false)

/* The deterministic suite is compiled out of the libFuzzer target, which
 * wants the parser and the walker and nothing else. */
#ifndef KPA_FUZZ_JSON

#define TEST_NODES 256u
#define TEST_SCRATCH 1024u

typedef struct test_arena {
    kpa_json_document document;
    kpa_json_node nodes[TEST_NODES];
    char scratch[TEST_SCRATCH];
} test_arena;

static kpa_json_result parse_bytes(test_arena *arena, const char *text,
                                   size_t length)
{
    kpa_json_document_init(&arena->document, arena->nodes, TEST_NODES,
                           arena->scratch, TEST_SCRATCH);
    return kpa_json_parse(&arena->document, text, length);
}

static kpa_json_result parse_text(test_arena *arena, const char *text)
{
    return parse_bytes(arena, text, strlen(text));
}

static bool expect_code(test_arena *arena, const char *text,
                        kpa_json_result expected, int line)
{
    const kpa_json_result actual = parse_text(arena, text);

    if (actual != expected) {
        (void)fprintf(stderr,
                      "%s:%d: parse(%s) -> %s, expected %s\n",
                      __FILE__, line, text, kpa_json_result_name(actual),
                      kpa_json_result_name(expected));
        return false;
    }
    /* A rejected document must be unreadable, not half readable. */
    if (actual != KPA_JSON_OK && kpa_json_root(&arena->document) != NULL) {
        (void)fprintf(stderr, "%s:%d: rejected document still has a root\n",
                      __FILE__, line);
        return false;
    }
    return true;
}

static bool expect_bytes(test_arena *arena, const unsigned char *bytes,
                         size_t length, kpa_json_result expected, int line)
{
    const kpa_json_result actual =
        parse_bytes(arena, (const char *)bytes, length);

    if (actual != expected) {
        (void)fprintf(stderr, "%s:%d: parse of %zu bytes -> %s, expected %s\n",
                      __FILE__, line, length, kpa_json_result_name(actual),
                      kpa_json_result_name(expected));
        return false;
    }
    return true;
}

#define EXPECT(text, code) CHECK(expect_code(arena, (text), (code), __LINE__))
#define EXPECT_BYTES(bytes, length, code)                                     \
    CHECK(expect_bytes(arena, (bytes), (length), (code), __LINE__))

/* ------------------------------------------------------------- basics */

static bool test_result_names(void)
{
    CHECK(strcmp(kpa_json_result_name(KPA_JSON_OK), "ok") == 0);
    CHECK(strcmp(kpa_json_result_name(KPA_JSON_INVALID_ARGUMENT),
                 "invalid argument") == 0);
    CHECK(strcmp(kpa_json_result_name(KPA_JSON_SYNTAX), "syntax") == 0);
    CHECK(strcmp(kpa_json_result_name(KPA_JSON_DEPTH), "too deep") == 0);
    CHECK(strcmp(kpa_json_result_name(KPA_JSON_NO_SPACE), "no space") == 0);
    CHECK(strcmp(kpa_json_result_name(KPA_JSON_UTF8), "invalid utf-8") == 0);
    CHECK(strcmp(kpa_json_result_name(KPA_JSON_DUPLICATE_KEY),
                 "duplicate key") == 0);
    CHECK(strcmp(kpa_json_result_name(KPA_JSON_RANGE), "out of range") == 0);
    CHECK(strcmp(kpa_json_result_name((kpa_json_result)99), "unknown") == 0);
    return true;
}

static bool test_invalid_argument(test_arena *arena)
{
    kpa_json_document document;
    kpa_json_node node;
    char scratch[4];

    CHECK(kpa_json_parse(NULL, "1", 1u) == KPA_JSON_INVALID_ARGUMENT);

    kpa_json_document_init(&arena->document, arena->nodes, TEST_NODES,
                           arena->scratch, TEST_SCRATCH);
    CHECK(kpa_json_parse(&arena->document, NULL, 0u) ==
          KPA_JSON_INVALID_ARGUMENT);
    CHECK(kpa_json_parse(&arena->document, NULL, 8u) ==
          KPA_JSON_INVALID_ARGUMENT);

    kpa_json_document_init(&document, NULL, 8u, scratch, sizeof scratch);
    CHECK(document.node_capacity == 0u);
    CHECK(kpa_json_parse(&document, "1", 1u) == KPA_JSON_INVALID_ARGUMENT);

    kpa_json_document_init(&document, &node, 0u, scratch, sizeof scratch);
    CHECK(kpa_json_parse(&document, "1", 1u) == KPA_JSON_INVALID_ARGUMENT);

    /* A scratch capacity that does not match its pointer is a caller bug and
     * must be caught before it becomes a write through NULL. */
    kpa_json_document_init(&document, &node, 1u, NULL, 0u);
    document.scratch_capacity = 64u;
    CHECK(kpa_json_parse(&document, "1", 1u) == KPA_JSON_INVALID_ARGUMENT);

    kpa_json_document_init(&document, NULL, 0u, NULL, 0u);
    CHECK(kpa_json_root(&document) == NULL);
    CHECK(kpa_json_root(NULL) == NULL);
    CHECK(kpa_json_at(NULL, 0u) == NULL);
    return true;
}

static bool test_empty_and_whitespace(test_arena *arena)
{
    EXPECT("", KPA_JSON_SYNTAX);
    EXPECT(" ", KPA_JSON_SYNTAX);
    EXPECT("\t\n\r ", KPA_JSON_SYNTAX);
    EXPECT("\n\n", KPA_JSON_SYNTAX);
    CHECK(parse_bytes(arena, "", 0u) == KPA_JSON_SYNTAX);
    CHECK(kpa_json_root(&arena->document) == NULL);
    /* Whitespace is exactly the four bytes RFC 8259 names. */
    EXPECT("\f1", KPA_JSON_SYNTAX);
    EXPECT("\v1", KPA_JSON_SYNTAX);
    EXPECT(" \t\r\n 1 \t\r\n ", KPA_JSON_OK);
    return true;
}

static bool test_top_level_scalars(test_arena *arena)
{
    const kpa_json_node *root;

    EXPECT("42", KPA_JSON_OK);
    root = kpa_json_root(&arena->document);
    CHECK(root != NULL && root->type == KPA_JSON_NUMBER);
    CHECK(root->number == 42.0);
    CHECK(arena->document.node_count == 1u);
    CHECK(kpa_json_at(&arena->document, 0u) == root);
    CHECK(kpa_json_at(&arena->document, 1u) == NULL);

    EXPECT("\"bare\"", KPA_JSON_OK);
    root = kpa_json_root(&arena->document);
    CHECK(root != NULL && root->type == KPA_JSON_STRING);
    CHECK(kpa_json_string_equals(root, "bare"));
    CHECK(!kpa_json_string_equals(root, "bar"));
    CHECK(!kpa_json_string_equals(root, "bares"));
    CHECK(!kpa_json_string_equals(NULL, "bare"));
    CHECK(!kpa_json_string_equals(root, NULL));

    EXPECT("true", KPA_JSON_OK);
    root = kpa_json_root(&arena->document);
    CHECK(root != NULL && root->type == KPA_JSON_BOOL && root->boolean);
    EXPECT("false", KPA_JSON_OK);
    root = kpa_json_root(&arena->document);
    CHECK(root != NULL && root->type == KPA_JSON_BOOL && !root->boolean);
    EXPECT("null", KPA_JSON_OK);
    root = kpa_json_root(&arena->document);
    CHECK(root != NULL && root->type == KPA_JSON_NULL);
    EXPECT("\"\"", KPA_JSON_OK);
    root = kpa_json_root(&arena->document);
    CHECK(root != NULL && root->length == 0u);
    CHECK(kpa_json_string_equals(root, ""));

    EXPECT("[]", KPA_JSON_OK);
    CHECK(kpa_json_root(&arena->document)->child_count == 0u);
    EXPECT("{}", KPA_JSON_OK);
    CHECK(kpa_json_root(&arena->document)->child_count == 0u);
    return true;
}

/* ------------------------------------------------------------- syntax */

static bool test_syntax_rejections(test_arena *arena)
{
    /* Extensions other parsers accept and this one must not. */
    EXPECT("{\"a\":1,}", KPA_JSON_SYNTAX);
    EXPECT("[1,2,]", KPA_JSON_SYNTAX);
    EXPECT("[,]", KPA_JSON_SYNTAX);
    EXPECT("{,}", KPA_JSON_SYNTAX);
    EXPECT("// comment\n1", KPA_JSON_SYNTAX);
    EXPECT("1 // comment", KPA_JSON_SYNTAX);
    EXPECT("/* comment */ 1", KPA_JSON_SYNTAX);
    EXPECT("[1 /* c */, 2]", KPA_JSON_SYNTAX);
    EXPECT("NaN", KPA_JSON_SYNTAX);
    EXPECT("Infinity", KPA_JSON_SYNTAX);
    EXPECT("-Infinity", KPA_JSON_SYNTAX);
    EXPECT("[NaN]", KPA_JSON_SYNTAX);
    EXPECT("0x10", KPA_JSON_SYNTAX);
    EXPECT("0X10", KPA_JSON_SYNTAX);
    EXPECT("'single'", KPA_JSON_SYNTAX);
    EXPECT("{'a':1}", KPA_JSON_SYNTAX);
    EXPECT("{a:1}", KPA_JSON_SYNTAX);
    EXPECT("{\"a\":1 \"b\":2}", KPA_JSON_SYNTAX);
    EXPECT("+1", KPA_JSON_SYNTAX);
    EXPECT("[+1]", KPA_JSON_SYNTAX);
    EXPECT("01", KPA_JSON_SYNTAX);
    EXPECT("-01", KPA_JSON_SYNTAX);
    EXPECT("[01]", KPA_JSON_SYNTAX);
    EXPECT("00", KPA_JSON_SYNTAX);
    EXPECT("1.", KPA_JSON_SYNTAX);
    EXPECT(".5", KPA_JSON_SYNTAX);
    EXPECT("-.5", KPA_JSON_SYNTAX);
    EXPECT("1.e5", KPA_JSON_SYNTAX);
    EXPECT("1e", KPA_JSON_SYNTAX);
    EXPECT("1e+", KPA_JSON_SYNTAX);
    EXPECT("1e-", KPA_JSON_SYNTAX);
    EXPECT("1.2.3", KPA_JSON_SYNTAX);
    EXPECT("1e2e3", KPA_JSON_SYNTAX);
    EXPECT("-", KPA_JSON_SYNTAX);
    EXPECT("--1", KPA_JSON_SYNTAX);
    EXPECT("1_000", KPA_JSON_SYNTAX);

    /* Structure. */
    EXPECT("[", KPA_JSON_SYNTAX);
    EXPECT("]", KPA_JSON_SYNTAX);
    EXPECT("{", KPA_JSON_SYNTAX);
    EXPECT("}", KPA_JSON_SYNTAX);
    EXPECT("[1", KPA_JSON_SYNTAX);
    EXPECT("[1}", KPA_JSON_SYNTAX);
    EXPECT("{\"a\":1]", KPA_JSON_SYNTAX);
    EXPECT("{\"a\"}", KPA_JSON_SYNTAX);
    EXPECT("{\"a\":}", KPA_JSON_SYNTAX);
    EXPECT("{:1}", KPA_JSON_SYNTAX);
    EXPECT("{\"a\":1,,\"b\":2}", KPA_JSON_SYNTAX);
    EXPECT("[[]", KPA_JSON_SYNTAX);
    EXPECT("[]]", KPA_JSON_SYNTAX);
    EXPECT("1 2", KPA_JSON_SYNTAX);
    EXPECT("[1][2]", KPA_JSON_SYNTAX);
    EXPECT("{} {}", KPA_JSON_SYNTAX);
    EXPECT("truex", KPA_JSON_SYNTAX);
    EXPECT("tru", KPA_JSON_SYNTAX);
    EXPECT("TRUE", KPA_JSON_SYNTAX);
    EXPECT("nul", KPA_JSON_SYNTAX);
    EXPECT("undefined", KPA_JSON_SYNTAX);

    /* Strings. */
    EXPECT("\"unterminated", KPA_JSON_SYNTAX);
    EXPECT("\"bad\\escape\"", KPA_JSON_SYNTAX);
    EXPECT("\"\\x41\"", KPA_JSON_SYNTAX);
    EXPECT("\"trailing\\", KPA_JSON_SYNTAX);
    EXPECT("\"\\u00\"", KPA_JSON_SYNTAX);
    EXPECT("\"\\u00g0\"", KPA_JSON_SYNTAX);
    EXPECT("\"\\u 041\"", KPA_JSON_SYNTAX);
    EXPECT("\"\\u041\"", KPA_JSON_SYNTAX);
    EXPECT("\"\\U0041\"", KPA_JSON_SYNTAX);

    /* Raw control characters are not string content. */
    {
        unsigned char control[] = {'"', 'a', 0x00u, 'b', '"'};
        unsigned char newline[] = {'"', 0x0au, '"'};
        unsigned char tab[] = {'"', 0x09u, '"'};
        unsigned char unit[] = {'"', 0x1fu, '"'};
        unsigned char space[] = {'"', 0x20u, '"'};
        unsigned char del[] = {'"', 0x7fu, '"'};

        EXPECT_BYTES(control, sizeof control, KPA_JSON_SYNTAX);
        EXPECT_BYTES(newline, sizeof newline, KPA_JSON_SYNTAX);
        EXPECT_BYTES(tab, sizeof tab, KPA_JSON_SYNTAX);
        EXPECT_BYTES(unit, sizeof unit, KPA_JSON_SYNTAX);
        EXPECT_BYTES(space, sizeof space, KPA_JSON_OK);
        EXPECT_BYTES(del, sizeof del, KPA_JSON_OK);
    }

    /* Well formed, for contrast. */
    EXPECT("[1,2,3]", KPA_JSON_OK);
    EXPECT("{\"a\":1,\"b\":[true,null,\"x\"]}", KPA_JSON_OK);
    EXPECT(" [ 1 , { \"a\" : [ ] } ] ", KPA_JSON_OK);
    return true;
}

/* -------------------------------------------------------------- depth */

static bool nested_document(char *buffer, size_t size, uint32_t depth,
                            char open, char close)
{
    size_t index;

    if ((size_t)depth * 2u + 1u > size) return false;
    for (index = 0u; index < depth; index++) buffer[index] = open;
    for (index = 0u; index < depth; index++) buffer[depth + index] = close;
    buffer[depth * 2u] = '\0';
    return true;
}

static bool test_depth(test_arena *arena)
{
    char buffer[512];

    CHECK(nested_document(buffer, sizeof buffer, KPA_JSON_MAX_DEPTH, '[',
                          ']'));
    EXPECT(buffer, KPA_JSON_OK);
    CHECK(arena->document.node_count == KPA_JSON_MAX_DEPTH);

    CHECK(nested_document(buffer, sizeof buffer, KPA_JSON_MAX_DEPTH + 1u,
                          '[', ']'));
    EXPECT(buffer, KPA_JSON_DEPTH);

    CHECK(nested_document(buffer, sizeof buffer, KPA_JSON_MAX_DEPTH + 200u,
                          '[', ']'));
    EXPECT(buffer, KPA_JSON_DEPTH);

    /* Unbalanced but deep: the limit must bite before the missing brackets
     * are ever noticed, which is what keeps the cost bounded. */
    {
        size_t index;
        for (index = 0u; index < 400u; index++) buffer[index] = '[';
        buffer[400] = '\0';
        EXPECT(buffer, KPA_JSON_DEPTH);
    }

    /* Objects nest through the same stack. */
    {
        size_t position = 0u;
        uint32_t level;
        for (level = 0u; level < KPA_JSON_MAX_DEPTH; level++) {
            memcpy(buffer + position, "{\"a\":", 5u);
            position += 5u;
        }
        buffer[position++] = '1';
        for (level = 0u; level < KPA_JSON_MAX_DEPTH; level++)
            buffer[position++] = '}';
        buffer[position] = '\0';
        EXPECT(buffer, KPA_JSON_OK);

        position = 0u;
        for (level = 0u; level < KPA_JSON_MAX_DEPTH + 1u; level++) {
            memcpy(buffer + position, "{\"a\":", 5u);
            position += 5u;
        }
        buffer[position++] = '1';
        for (level = 0u; level < KPA_JSON_MAX_DEPTH + 1u; level++)
            buffer[position++] = '}';
        buffer[position] = '\0';
        EXPECT(buffer, KPA_JSON_DEPTH);
    }

    /* Mixed containers, exactly at the limit and one past it. */
    {
        size_t index;
        for (index = 0u; index < KPA_JSON_MAX_DEPTH; index++)
            buffer[index] = (index % 2u == 0u) ? '[' : '[';
        for (index = 0u; index < KPA_JSON_MAX_DEPTH; index++)
            buffer[KPA_JSON_MAX_DEPTH + index] = ']';
        buffer[KPA_JSON_MAX_DEPTH * 2u] = '\0';
        EXPECT(buffer, KPA_JSON_OK);
    }
    return true;
}

/* ----------------------------------------------------- duplicate keys */

/*
 * {"k00":0,...,"k<count-1>":<count-1>,"<last>":0} in `buffer`, or false if it
 * would not fit; the caller supplies `last` to make the final member either a
 * repeat of an earlier key or a fresh one.
 */
static bool members_document(char *buffer, size_t size, unsigned int count,
                             const char *last)
{
    size_t position = 0u;
    unsigned int index;
    int written;

    if (size < 2u) return false;
    buffer[position++] = '{';
    for (index = 0u; index < count; index++) {
        written = snprintf(buffer + position, size - position,
                           "\"k%02u\":%u,", index, index);
        if (written < 0 || (size_t)written >= size - position) return false;
        position += (size_t)written;
    }
    written = snprintf(buffer + position, size - position, "\"%s\":0}", last);
    return written >= 0 && (size_t)written < size - position;
}

static bool test_duplicate_keys(test_arena *arena)
{
    EXPECT("{\"a\":1,\"a\":2}", KPA_JSON_DUPLICATE_KEY);
    EXPECT("{\"duration\":161.0,\"duration\":1800}", KPA_JSON_DUPLICATE_KEY);
    EXPECT("{\"a\":1,\"b\":2,\"a\":3}", KPA_JSON_DUPLICATE_KEY);
    EXPECT("{\"\":1,\"\":2}", KPA_JSON_DUPLICATE_KEY);
    /* Same bytes, different spelling: escapes are compared after decoding. */
    EXPECT("{\"a\":1,\"\\u0061\":2}", KPA_JSON_DUPLICATE_KEY);
    EXPECT("{\"\\u0061\":1,\"a\":2}", KPA_JSON_DUPLICATE_KEY);
    EXPECT("{\"caf\\u00e9\":1,\"caf\xc3\xa9\":2}", KPA_JSON_DUPLICATE_KEY);
    EXPECT("{\"a\":{\"b\":1,\"b\":2}}", KPA_JSON_DUPLICATE_KEY);
    EXPECT("{\"a\":[{\"b\":1,\"b\":2}]}", KPA_JSON_DUPLICATE_KEY);

    /* The same key in sibling objects is not a duplicate. */
    EXPECT("{\"a\":{\"k\":1},\"b\":{\"k\":2}}", KPA_JSON_OK);
    EXPECT("[{\"k\":1},{\"k\":2}]", KPA_JSON_OK);
    EXPECT("{\"a\":1,\"A\":2}", KPA_JSON_OK);
    EXPECT("{\"a\":1,\"ab\":2,\"b\":3}", KPA_JSON_OK);

    /* Many members, one of which repeats late: the filter must not lose it. */
    {
        char buffer[1024];

        CHECK(members_document(buffer, sizeof buffer, 60u, "k37"));
        EXPECT(buffer, KPA_JSON_DUPLICATE_KEY);
        CHECK(members_document(buffer, sizeof buffer, 60u, "z"));
        EXPECT(buffer, KPA_JSON_OK);
        CHECK(kpa_json_root(&arena->document)->child_count == 61u);

        /* The hash filter is 64 bits wide, so an object with more members
         * than that saturates it and every key falls through to the scan. */
        CHECK(members_document(buffer, sizeof buffer, 90u, "k07"));
        EXPECT(buffer, KPA_JSON_DUPLICATE_KEY);
        CHECK(members_document(buffer, sizeof buffer, 90u, "zz"));
        EXPECT(buffer, KPA_JSON_OK);
        CHECK(kpa_json_root(&arena->document)->child_count == 91u);
    }
    return true;
}

/* --------------------------------------------------------------- utf-8 */

static bool test_utf8_rejections(test_arena *arena)
{
    static const struct {
        const char *name;
        unsigned char bytes[8];
        size_t length;
    } cases[] = {
        {"stray continuation 0x80", {'"', 0x80u, '"'}, 3u},
        {"stray continuation 0xbf", {'"', 0xbfu, '"'}, 3u},
        {"overlong lead 0xc0", {'"', 0xc0u, 0x80u, '"'}, 4u},
        {"overlong lead 0xc1", {'"', 0xc1u, 0xbfu, '"'}, 4u},
        {"two byte truncated", {'"', 0xc2u, '"'}, 3u},
        {"two byte truncated at end", {'"', 'a', 0xc2u}, 3u},
        {"two byte bad tail", {'"', 0xc2u, 0x41u, '"'}, 4u},
        {"three byte overlong e0 80", {'"', 0xe0u, 0x80u, 0x80u, '"'}, 5u},
        {"three byte overlong e0 9f", {'"', 0xe0u, 0x9fu, 0xbfu, '"'}, 5u},
        {"surrogate u+d800", {'"', 0xedu, 0xa0u, 0x80u, '"'}, 5u},
        {"surrogate u+dbff", {'"', 0xedu, 0xafu, 0xbfu, '"'}, 5u},
        {"surrogate u+dc00", {'"', 0xedu, 0xb0u, 0x80u, '"'}, 5u},
        {"surrogate u+dfff", {'"', 0xedu, 0xbfu, 0xbfu, '"'}, 5u},
        {"three byte truncated one", {'"', 0xe1u, '"'}, 3u},
        {"three byte truncated two", {'"', 0xe1u, 0x80u, '"'}, 4u},
        {"three byte bad tail", {'"', 0xe1u, 0x80u, 0x41u, '"'}, 5u},
        {"four byte overlong f0 80", {'"', 0xf0u, 0x80u, 0x80u, 0x80u, '"'},
         6u},
        {"four byte overlong f0 8f", {'"', 0xf0u, 0x8fu, 0xbfu, 0xbfu, '"'},
         6u},
        {"above u+10ffff", {'"', 0xf4u, 0x90u, 0x80u, 0x80u, '"'}, 6u},
        {"lead 0xf5", {'"', 0xf5u, 0x80u, 0x80u, 0x80u, '"'}, 6u},
        {"lead 0xfe", {'"', 0xfeu, '"'}, 3u},
        {"lead 0xff", {'"', 0xffu, '"'}, 3u},
        {"four byte truncated", {'"', 0xf0u, 0x9fu, 0x98u, '"'}, 5u},
        {"outside a string", {0xffu}, 1u},
        {"in a key", {'{', '"', 0x80u, '"', ':', '1', '}'}, 7u},
        {"after a valid document", {'1', 0xc3u}, 2u}
    };
    static const struct {
        const char *name;
        unsigned char bytes[10];
        size_t length;
    } accepted[] = {
        {"u+00a9", {'"', 0xc2u, 0xa9u, '"'}, 4u},
        {"u+20ac", {'"', 0xe2u, 0x82u, 0xacu, '"'}, 5u},
        {"u+d7ff", {'"', 0xedu, 0x9fu, 0xbfu, '"'}, 5u},
        {"u+e000", {'"', 0xeeu, 0x80u, 0x80u, '"'}, 5u},
        {"u+10000", {'"', 0xf0u, 0x90u, 0x80u, 0x80u, '"'}, 6u},
        {"u+1f600", {'"', 0xf0u, 0x9fu, 0x98u, 0x80u, '"'}, 6u},
        {"u+10ffff", {'"', 0xf4u, 0x8fu, 0xbfu, 0xbfu, '"'}, 6u}
    };
    size_t index;

    for (index = 0u; index < sizeof cases / sizeof cases[0]; index++) {
        const kpa_json_result actual =
            parse_bytes(arena, (const char *)cases[index].bytes,
                        cases[index].length);
        if (actual != KPA_JSON_UTF8) {
            (void)fprintf(stderr, "%s:%d: %s -> %s, expected invalid utf-8\n",
                          __FILE__, __LINE__, cases[index].name,
                          kpa_json_result_name(actual));
            return false;
        }
    }
    for (index = 0u; index < sizeof accepted / sizeof accepted[0]; index++) {
        const kpa_json_result actual =
            parse_bytes(arena, (const char *)accepted[index].bytes,
                        accepted[index].length);
        if (actual != KPA_JSON_OK) {
            (void)fprintf(stderr, "%s:%d: %s -> %s, expected ok\n", __FILE__,
                          __LINE__, accepted[index].name,
                          kpa_json_result_name(actual));
            return false;
        }
        CHECK(kpa_json_root(&arena->document)->length ==
              accepted[index].length - 2u);
    }
    return true;
}

/* ------------------------------------------------------------ escapes */

static bool string_is(const kpa_json_node *node, const char *expected,
                      size_t length)
{
    return node != NULL && node->type == KPA_JSON_STRING &&
           node->length == length &&
           memcmp(node->text, expected, length) == 0;
}

static bool test_escapes(test_arena *arena)
{
    EXPECT("\"\\\"\\\\\\/\\b\\f\\n\\r\\t\"", KPA_JSON_OK);
    CHECK(string_is(kpa_json_root(&arena->document),
                    "\"\\/\x08\x0c\x0a\x0d\x09", 8u));

    EXPECT("\"\\u0041\"", KPA_JSON_OK);
    CHECK(string_is(kpa_json_root(&arena->document), "A", 1u));
    EXPECT("\"\\u00e9\"", KPA_JSON_OK);
    CHECK(string_is(kpa_json_root(&arena->document), "\xc3\xa9", 2u));
    EXPECT("\"\\u00E9\"", KPA_JSON_OK);
    CHECK(string_is(kpa_json_root(&arena->document), "\xc3\xa9", 2u));
    EXPECT("\"\\u007f\"", KPA_JSON_OK);
    CHECK(string_is(kpa_json_root(&arena->document), "\x7f", 1u));
    EXPECT("\"\\u0080\"", KPA_JSON_OK);
    CHECK(string_is(kpa_json_root(&arena->document), "\xc2\x80", 2u));
    EXPECT("\"\\u07ff\"", KPA_JSON_OK);
    CHECK(string_is(kpa_json_root(&arena->document), "\xdf\xbf", 2u));
    EXPECT("\"\\u0800\"", KPA_JSON_OK);
    CHECK(string_is(kpa_json_root(&arena->document), "\xe0\xa0\x80", 3u));
    EXPECT("\"\\u20ac\"", KPA_JSON_OK);
    CHECK(string_is(kpa_json_root(&arena->document), "\xe2\x82\xac", 3u));
    EXPECT("\"\\uffff\"", KPA_JSON_OK);
    CHECK(string_is(kpa_json_root(&arena->document), "\xef\xbf\xbf", 3u));
    EXPECT("\"\\ud7ff\"", KPA_JSON_OK);
    CHECK(string_is(kpa_json_root(&arena->document), "\xed\x9f\xbf", 3u));

    /* A NUL is legal string content and must survive as one byte. */
    EXPECT("\"a\\u0000b\"", KPA_JSON_OK);
    CHECK(string_is(kpa_json_root(&arena->document), "a\0b", 3u));

    /* Surrogate pairs. */
    EXPECT("\"\\ud83d\\ude00\"", KPA_JSON_OK);
    CHECK(string_is(kpa_json_root(&arena->document), "\xf0\x9f\x98\x80", 4u));
    EXPECT("\"\\ud800\\udc00\"", KPA_JSON_OK);
    CHECK(string_is(kpa_json_root(&arena->document), "\xf0\x90\x80\x80", 4u));
    EXPECT("\"\\udbff\\udfff\"", KPA_JSON_OK);
    CHECK(string_is(kpa_json_root(&arena->document), "\xf4\x8f\xbf\xbf", 4u));
    EXPECT("\"a\\ud83d\\ude00b\"", KPA_JSON_OK);
    CHECK(string_is(kpa_json_root(&arena->document), "a\xf0\x9f\x98\x80""b",
                    6u));

    /* Unpaired halves are not text and never reach the arena. */
    EXPECT("\"\\ud800\"", KPA_JSON_UTF8);
    EXPECT("\"\\udbff\"", KPA_JSON_UTF8);
    EXPECT("\"\\udc00\"", KPA_JSON_UTF8);
    EXPECT("\"\\udfff\"", KPA_JSON_UTF8);
    EXPECT("\"\\ud800\\u0041\"", KPA_JSON_UTF8);
    EXPECT("\"\\ud800\\ud800\"", KPA_JSON_UTF8);
    EXPECT("\"\\ud800A\"", KPA_JSON_UTF8);
    EXPECT("\"\\ud800\\\\\"", KPA_JSON_UTF8);
    EXPECT("\"\\udc00\\ud800\"", KPA_JSON_UTF8);
    EXPECT("\"\\ud83d\"", KPA_JSON_UTF8);
    EXPECT("\"\\ud800\\u00\"", KPA_JSON_SYNTAX);

    /* Escapes work in keys too. */
    EXPECT("{\"\\u00e9\":1}", KPA_JSON_OK);
    {
        const kpa_json_node *member =
            kpa_json_member(&arena->document,
                            kpa_json_root(&arena->document), "\xc3\xa9");
        CHECK(member != NULL && member->number == 1.0);
    }
    return true;
}

/* ----------------------------------------------- borrowing vs scratch */

static bool test_string_borrowing(test_arena *arena)
{
    static const char document[] =
        "{\"plain\":\"no escapes here\",\"escaped\":\"one\\nescape\"}";
    const kpa_json_node *root;
    const kpa_json_node *plain;
    const kpa_json_node *escaped;

    CHECK(parse_bytes(arena, document, sizeof document - 1u) == KPA_JSON_OK);
    root = kpa_json_root(&arena->document);
    plain = kpa_json_member(&arena->document, root, "plain");
    escaped = kpa_json_member(&arena->document, root, "escaped");
    CHECK(plain != NULL && escaped != NULL);

    /* An unescaped string costs no arena: it points into the caller's own
     * buffer, which is the whole reason scratch stays empty for a typical
     * manifest. */
    CHECK(plain->text >= document && plain->text < document + sizeof document);
    CHECK(plain->key >= document && plain->key < document + sizeof document);
    CHECK(escaped->key >= document &&
          escaped->key < document + sizeof document);
    CHECK(escaped->text >= arena->scratch &&
          escaped->text < arena->scratch + TEST_SCRATCH);
    CHECK(string_is(escaped, "one\nescape", 10u));
    CHECK(arena->document.scratch_used == 10u);

    /* A document with no escape at all must not touch scratch, and must parse
     * with no scratch buffer provided. */
    CHECK(parse_text(arena, "{\"a\":\"b\",\"c\":[\"d\",\"e\"]}") ==
          KPA_JSON_OK);
    CHECK(arena->document.scratch_used == 0u);
    {
        kpa_json_document document_without_scratch;
        kpa_json_node nodes[8];

        kpa_json_document_init(&document_without_scratch, nodes, 8u, NULL, 0u);
        CHECK(kpa_json_parse(&document_without_scratch, "[\"a\",\"b\"]", 9u) ==
              KPA_JSON_OK);
        CHECK(document_without_scratch.scratch_used == 0u);
        CHECK(kpa_json_parse(&document_without_scratch, "[\"\\n\"]", 6u) ==
              KPA_JSON_NO_SPACE);
    }
    return true;
}

/* ------------------------------------------------------------ numbers */

struct number_case {
    const char *text;
    uint64_t bits;
};

/*
 * Every case below is the literal on the left and the exact bit pattern
 * python3 gives for it:
 *
 *   struct.unpack('<Q', struct.pack('<d', float(text)))[0]
 *
 * Included on purpose: subnormals, both ends of the normal range, -0.0,
 * mantissas far longer than a double can hold, exact halfway cases, and
 * the plain timestamps and bar counts a real project file is made of.
 */
static const struct number_case number_cases[] = {
    {"0", UINT64_C(0x0000000000000000)},
    {"-0", UINT64_C(0x8000000000000000)},
    {"0.0", UINT64_C(0x0000000000000000)},
    {"-0.0", UINT64_C(0x8000000000000000)},
    {"1", UINT64_C(0x3ff0000000000000)},
    {"-1", UINT64_C(0xbff0000000000000)},
    {"40", UINT64_C(0x4044000000000000)},
    {"161.0", UINT64_C(0x4064200000000000)},
    {"1800", UINT64_C(0x409c200000000000)},
    {"25.755", UINT64_C(0x4039c147ae147ae1)},
    {"3.5", UINT64_C(0x400c000000000000)},
    {"0.1", UINT64_C(0x3fb999999999999a)},
    {"0.2", UINT64_C(0x3fc999999999999a)},
    {"0.3", UINT64_C(0x3fd3333333333333)},
    {"1.0e-3", UINT64_C(0x3f50624dd2f1a9fc)},
    {"2.5", UINT64_C(0x4004000000000000)},
    {"-2.5", UINT64_C(0xc004000000000000)},
    {"0.5", UINT64_C(0x3fe0000000000000)},
    {"1e2", UINT64_C(0x4059000000000000)},
    {"1E2", UINT64_C(0x4059000000000000)},
    {"1e+2", UINT64_C(0x4059000000000000)},
    {"1e-2", UINT64_C(0x3f847ae147ae147b)},
    {"9007199254740991", UINT64_C(0x433fffffffffffff)},
    {"9007199254740992", UINT64_C(0x4340000000000000)},
    {"9007199254740993", UINT64_C(0x4340000000000000)},
    {"4503599627370497", UINT64_C(0x4330000000000001)},
    {"1e22", UINT64_C(0x4480f0cf064dd592)},
    {"1e23", UINT64_C(0x44b52d02c7e14af6)},
    {"1e-22", UINT64_C(0x3b5e392010175ee6)},
    {"1e-23", UINT64_C(0x3b282db34012b251)},
    {"1e308", UINT64_C(0x7fe1ccf385ebc8a0)},
    {"1e-308", UINT64_C(0x000730d67819e8d2)},
    {"2.2250738585072014e-308", UINT64_C(0x0010000000000000)},
    {"2.2250738585072011e-308", UINT64_C(0x000fffffffffffff)},
    {"5e-324", UINT64_C(0x0000000000000001)},
    {"4.9406564584124654e-324", UINT64_C(0x0000000000000001)},
    {"1e-320", UINT64_C(0x00000000000007e8)},
    {"1.7976931348623157e308", UINT64_C(0x7fefffffffffffff)},
    {"1.7976931348623155e308", UINT64_C(0x7feffffffffffffe)},
    {"3.141592653589793238462643383279502884197169"
     "39937510582097494459230781640628"
     , UINT64_C(0x400921fb54442d18)},
    {"2.718281828459045235360287471352662497757247"
     "09369995957496696762772407663035"
     , UINT64_C(0x4005bf0a8b145769)},
    {"1.000000000000000055511151231257827021181583"
     "404541015625"
     , UINT64_C(0x3ff0000000000000)},
    {"0.000000000000000000000000000000000000000000"
     "000000000001"
     , UINT64_C(0x34b8851a0b548ea4)},
    {"12345678901234567890123456789012345678901234"
     "5678901234567890"
     , UINT64_C(0x4c33aaf504e4bc1e)},
    {"0.4999999999999999999999999999999999999999",
     UINT64_C(0x3fe0000000000000)},
    {"0.5000000000000000000000000000000000000001",
     UINT64_C(0x3fe0000000000000)},
    {"-1.5e-5", UINT64_C(0xbeef75104d551d69)},
    {"6.02214076e23", UINT64_C(0x44dfe185ca57c517)},
    {"1.602176634e-19", UINT64_C(0x3c07a4da290c1653)},
    {"1234.5678", UINT64_C(0x40934a456d5cfaad)},
    {"-0.000725", UINT64_C(0xbf47c1bda5119ce0)},
    {"161.00000000000000000000000000000000001", UINT64_C(0x4064200000000000)},
    {"1800e-3", UINT64_C(0x3ffccccccccccccd)},
    {"0e999999999999999999", UINT64_C(0x0000000000000000)},
    {"-0e5", UINT64_C(0x8000000000000000)},
    {"2.2250738585072012e-308", UINT64_C(0x0010000000000000)},
    {"7.4109846876186982e+108", UINT64_C(0x56893e9a2ebf2853)},
};

static uint64_t double_bits(double value)
{
    uint64_t bits;

    memcpy(&bits, &value, sizeof bits);
    return bits;
}

static bool test_numbers(test_arena *arena)
{
    size_t index;

    for (index = 0u; index < sizeof number_cases / sizeof number_cases[0];
         index++) {
        const kpa_json_result result =
            parse_text(arena, number_cases[index].text);
        const kpa_json_node *root;
        uint64_t bits;

        if (result != KPA_JSON_OK) {
            (void)fprintf(stderr, "%s:%d: %s -> %s\n", __FILE__, __LINE__,
                          number_cases[index].text,
                          kpa_json_result_name(result));
            return false;
        }
        root = kpa_json_root(&arena->document);
        CHECK(root != NULL && root->type == KPA_JSON_NUMBER);
        bits = double_bits(root->number);
        if (bits != number_cases[index].bits) {
            (void)fprintf(stderr,
                          "%s:%d: %s -> %016llx, python says %016llx\n",
                          __FILE__, __LINE__, number_cases[index].text,
                          (unsigned long long)bits,
                          (unsigned long long)number_cases[index].bits);
            return false;
        }
    }

    /* The same values nested, so the container path shares the scanner. */
    EXPECT("{\"start\":25.755,\"bars\":40,\"tempo\":161.0,\"end\":1800}",
           KPA_JSON_OK);
    {
        const kpa_json_node *root = kpa_json_root(&arena->document);
        double value = 0.0;

        CHECK(kpa_json_number(&arena->document, root, "start", &value));
        CHECK(double_bits(value) == UINT64_C(0x4039c147ae147ae1));
        CHECK(kpa_json_number(&arena->document, root, "bars", &value));
        CHECK(value == 40.0);
        CHECK(kpa_json_number(&arena->document, root, "tempo", &value));
        CHECK(value == 161.0);
        CHECK(kpa_json_number(&arena->document, root, "end", &value));
        CHECK(value == 1800.0);
    }
    return true;
}

/*
 * A significand longer than the 800 digits the converter keeps must still
 * round correctly: digits it drops only ever make the stored value smaller,
 * and the truncation flag is what turns "exactly halfway" back into "above
 * halfway" so the tie breaks the way the real number demands.  All three
 * expectations are python3's.
 */
static bool test_long_significands(test_arena *arena)
{
    static const char midpoint[] =
        "1.00000000000000011102230246251565404236316680908203125";
    char buffer[1024];
    size_t position;

    /* The exact midpoint between 1.0 and the next double ties to even. */
    EXPECT(midpoint, KPA_JSON_OK);
    CHECK(double_bits(kpa_json_root(&arena->document)->number) ==
          UINT64_C(0x3ff0000000000000));

    /* One unit at the 855th significant digit puts it above the midpoint,
     * and every digit past the 800th is dropped before the decision. */
    position = sizeof midpoint - 1u;
    memcpy(buffer, midpoint, position);
    memset(buffer + position, '0', 800u);
    position += 800u;
    buffer[position++] = '1';
    buffer[position] = '\0';
    CHECK(parse_bytes(arena, buffer, position) == KPA_JSON_OK);
    CHECK(double_bits(kpa_json_root(&arena->document)->number) ==
          UINT64_C(0x3ff0000000000001));

    /* The same length just below the midpoint stays put. */
    position = sizeof midpoint - 2u;
    memcpy(buffer, midpoint, position);
    memset(buffer + position, '0', 800u);
    position += 800u;
    buffer[position++] = '4';
    buffer[position] = '\0';
    CHECK(parse_bytes(arena, buffer, position) == KPA_JSON_OK);
    CHECK(double_bits(kpa_json_root(&arena->document)->number) ==
          UINT64_C(0x3ff0000000000000));

    /* 1699 nines is one hair under 1.0 and must not round past it. */
    buffer[0] = '0';
    buffer[1] = '.';
    memset(buffer + 2, '9', 900u);
    buffer[902] = '\0';
    CHECK(parse_bytes(arena, buffer, 902u) == KPA_JSON_OK);
    CHECK(kpa_json_root(&arena->document)->number == 1.0);
    return true;
}

static bool test_number_range(test_arena *arena)
{
    EXPECT("1e309", KPA_JSON_RANGE);
    EXPECT("-1e309", KPA_JSON_RANGE);
    EXPECT("1e400", KPA_JSON_RANGE);
    EXPECT("1.8e308", KPA_JSON_RANGE);
    EXPECT("[1e309]", KPA_JSON_RANGE);
    EXPECT("{\"a\":1e309}", KPA_JSON_RANGE);
    /* The exact midpoint above DBL_MAX ties to even, and even here is the
     * infinity a double cannot hold; one unit below it is DBL_MAX itself.
     * python3 agrees on both: inf and 1.7976931348623157e+308. */
    EXPECT("17976931348623158079372897140530341507993413271003782693617377898"
           "04449682927647509466490179775872070963302864166928879109465555478"
           "51940402630657488671505820681908902000708383676273854845817711531"
           "76447573027006985557136695962284291481986083493647529271907416844"
           "4365510704342711559699508093042880177904174497792",
           KPA_JSON_RANGE);
    EXPECT("17976931348623158079372897140530341507993413271003782693617377898"
           "04449682927647509466490179775872070963302864166928879109465555478"
           "51940402630657488671505820681908902000708383676273854845817711531"
           "76447573027006985557136695962284291481986083493647529271907416844"
           "4365510704342711559699508093042880177904174497791",
           KPA_JSON_OK);
    CHECK(double_bits(kpa_json_root(&arena->document)->number) ==
          UINT64_C(0x7fefffffffffffff));
    EXPECT("1e-400", KPA_JSON_RANGE);
    EXPECT("-1e-400", KPA_JSON_RANGE);
    EXPECT("1e-330", KPA_JSON_RANGE);
    EXPECT("2e-324", KPA_JSON_RANGE);
    EXPECT("1e999999999999999999999", KPA_JSON_RANGE);
    EXPECT("1e-999999999999999999999", KPA_JSON_RANGE);
    /* A zero significand has no magnitude to be out of range. */
    EXPECT("0e999999999999999999999", KPA_JSON_OK);
    EXPECT("0.000e-999999999999", KPA_JSON_OK);
    CHECK(kpa_json_root(&arena->document)->number == 0.0);
    /* The smallest subnormal is representable; half of it is not. */
    EXPECT("5e-324", KPA_JSON_OK);
    CHECK(double_bits(kpa_json_root(&arena->document)->number) == 1u);
    EXPECT("2.4703282292062327e-324", KPA_JSON_RANGE);
    EXPECT("2.4703282292062328e-324", KPA_JSON_OK);
    CHECK(double_bits(kpa_json_root(&arena->document)->number) == 1u);
    return true;
}

/* ----------------------------------------------------------- the arena */

static kpa_json_result parse_sized(const char *text, uint32_t node_capacity,
                                   size_t scratch_capacity,
                                   kpa_json_document *document,
                                   kpa_json_node *nodes, char *scratch)
{
    kpa_json_document_init(document, nodes, node_capacity, scratch,
                           scratch_capacity);
    return kpa_json_parse(document, text, strlen(text));
}

static bool test_arena_exhaustion(void)
{
    kpa_json_document document;
    kpa_json_node nodes[64];
    char scratch[64];
    uint32_t capacity;

    /* Node capacity, one boundary at a time. */
    CHECK(parse_sized("1", 1u, 0u, &document, nodes, NULL) == KPA_JSON_OK);
    CHECK(parse_sized("[]", 1u, 0u, &document, nodes, NULL) == KPA_JSON_OK);
    CHECK(parse_sized("[1]", 2u, 0u, &document, nodes, NULL) == KPA_JSON_OK);
    CHECK(parse_sized("[1]", 1u, 0u, &document, nodes, NULL) ==
          KPA_JSON_NO_SPACE);
    CHECK(parse_sized("[1,2,3]", 4u, 0u, &document, nodes, NULL) ==
          KPA_JSON_OK);
    CHECK(document.node_count == 4u);
    CHECK(parse_sized("[1,2,3]", 3u, 0u, &document, nodes, NULL) ==
          KPA_JSON_NO_SPACE);
    CHECK(parse_sized("{\"a\":{\"b\":[1,2]}}", 5u, 0u, &document, nodes,
                      NULL) == KPA_JSON_OK);
    for (capacity = 1u; capacity < 5u; capacity++)
        CHECK(parse_sized("{\"a\":{\"b\":[1,2]}}", capacity, 0u, &document,
                          nodes, NULL) == KPA_JSON_NO_SPACE);
    /* Exhaustion leaves the document unusable rather than half built. */
    CHECK(document.node_count == 0u);
    CHECK(kpa_json_root(&document) == NULL);
    CHECK(kpa_json_at(&document, 0u) == NULL);

    /* Scratch capacity, one boundary at a time.  Only escaped text spends
     * it, so each of these sizes is the exact decoded length. */
    CHECK(parse_sized("[\"\\n\"]", 8u, 1u, &document, nodes, scratch) ==
          KPA_JSON_OK);
    CHECK(document.scratch_used == 1u);
    CHECK(parse_sized("[\"\\n\"]", 8u, 0u, &document, nodes, scratch) ==
          KPA_JSON_NO_SPACE);
    CHECK(parse_sized("[\"a\\nb\"]", 8u, 3u, &document, nodes, scratch) ==
          KPA_JSON_OK);
    CHECK(parse_sized("[\"a\\nb\"]", 8u, 2u, &document, nodes, scratch) ==
          KPA_JSON_NO_SPACE);
    CHECK(parse_sized("[\"\\u00e9\"]", 8u, 2u, &document, nodes, scratch) ==
          KPA_JSON_OK);
    CHECK(parse_sized("[\"\\u00e9\"]", 8u, 1u, &document, nodes, scratch) ==
          KPA_JSON_NO_SPACE);
    CHECK(parse_sized("[\"\\u20ac\"]", 8u, 3u, &document, nodes, scratch) ==
          KPA_JSON_OK);
    CHECK(parse_sized("[\"\\u20ac\"]", 8u, 2u, &document, nodes, scratch) ==
          KPA_JSON_NO_SPACE);
    CHECK(parse_sized("[\"\\ud83d\\ude00\"]", 8u, 4u, &document, nodes,
                      scratch) == KPA_JSON_OK);
    CHECK(parse_sized("[\"\\ud83d\\ude00\"]", 8u, 3u, &document, nodes,
                      scratch) == KPA_JSON_NO_SPACE);
    /* The prefix before the first escape is copied too. */
    CHECK(parse_sized("[\"abcd\\n\"]", 8u, 5u, &document, nodes, scratch) ==
          KPA_JSON_OK);
    CHECK(parse_sized("[\"abcd\\n\"]", 8u, 4u, &document, nodes, scratch) ==
          KPA_JSON_NO_SPACE);
    /* An escaped key spends scratch on the same terms. */
    CHECK(parse_sized("{\"\\u0061\":1}", 8u, 1u, &document, nodes, scratch) ==
          KPA_JSON_OK);
    CHECK(parse_sized("{\"\\u0061\":1}", 8u, 0u, &document, nodes, scratch) ==
          KPA_JSON_NO_SPACE);
    /* Two escaped strings share one arena, so the second sees the first. */
    CHECK(parse_sized("[\"\\n\",\"\\n\"]", 8u, 2u, &document, nodes,
                      scratch) == KPA_JSON_OK);
    CHECK(parse_sized("[\"\\n\",\"\\n\"]", 8u, 1u, &document, nodes,
                      scratch) == KPA_JSON_NO_SPACE);
    CHECK(document.scratch_used == 0u);
    CHECK(kpa_json_root(&document) == NULL);
    return true;
}

/* --------------------------------------------------------- accessors */

static bool test_accessors(test_arena *arena)
{
    static const char document[] =
        "{\"zero\":0,\"false\":false,\"empty\":\"\",\"null\":null,"
        "\"name\":\"kilix\",\"list\":[10,20,30],\"nested\":{\"k\":1},"
        "\"nul\":\"a\\u0000b\"}";
    const kpa_json_node *root;
    const kpa_json_node *list;
    double number = 12345.0;
    bool flag = true;
    const char *text = NULL;
    size_t length = 999u;
    char buffer[16];

    CHECK(parse_bytes(arena, document, sizeof document - 1u) == KPA_JSON_OK);
    root = kpa_json_root(&arena->document);
    CHECK(root != NULL && root->type == KPA_JSON_OBJECT);
    CHECK(root->child_count == 8u);

    /* Present and zero is not the same answer as missing, and the caller can
     * tell the two apart without ever looking at the node. */
    CHECK(kpa_json_number(&arena->document, root, "zero", &number));
    CHECK(number == 0.0);
    number = 12345.0;
    CHECK(!kpa_json_number(&arena->document, root, "absent", &number));
    CHECK(number == 12345.0);
    CHECK(!kpa_json_number(&arena->document, root, "false", &number));
    CHECK(number == 12345.0);
    CHECK(!kpa_json_number(&arena->document, root, "null", &number));
    CHECK(number == 12345.0);
    CHECK(!kpa_json_number(&arena->document, root, "nested", &number));
    CHECK(number == 12345.0);

    CHECK(kpa_json_bool(&arena->document, root, "false", &flag));
    CHECK(!flag);
    flag = true;
    CHECK(!kpa_json_bool(&arena->document, root, "absent", &flag));
    CHECK(flag);
    CHECK(!kpa_json_bool(&arena->document, root, "zero", &flag));
    CHECK(flag);

    CHECK(kpa_json_string(&arena->document, root, "empty", &text, &length));
    CHECK(length == 0u);
    text = NULL;
    length = 999u;
    CHECK(!kpa_json_string(&arena->document, root, "absent", &text, &length));
    CHECK(text == NULL && length == 999u);
    CHECK(!kpa_json_string(&arena->document, root, "zero", &text, &length));
    CHECK(text == NULL && length == 999u);
    CHECK(kpa_json_string(&arena->document, root, "name", &text, &length));
    CHECK(length == 5u && memcmp(text, "kilix", 5u) == 0);

    CHECK(kpa_json_string_copy(&arena->document, root, "name", buffer,
                               sizeof buffer));
    CHECK(strcmp(buffer, "kilix") == 0);
    CHECK(kpa_json_string_copy(&arena->document, root, "name", buffer, 6u));
    CHECK(strcmp(buffer, "kilix") == 0);
    /* No room for the terminator is a failure, not a shortened string. */
    CHECK(!kpa_json_string_copy(&arena->document, root, "name", buffer, 5u));
    CHECK(!kpa_json_string_copy(&arena->document, root, "name", buffer, 0u));
    CHECK(!kpa_json_string_copy(&arena->document, root, "name", NULL, 8u));
    CHECK(!kpa_json_string_copy(&arena->document, root, "absent", buffer,
                                sizeof buffer));
    /* An embedded NUL cannot be handed over as a C string at all. */
    CHECK(!kpa_json_string_copy(&arena->document, root, "nul", buffer,
                                sizeof buffer));
    CHECK(kpa_json_string_copy(&arena->document, root, "empty", buffer,
                               sizeof buffer));
    CHECK(buffer[0] == '\0');

    list = kpa_json_member(&arena->document, root, "list");
    CHECK(list != NULL && list->type == KPA_JSON_ARRAY);
    CHECK(list->child_count == 3u);
    CHECK(kpa_json_element(&arena->document, list, 0u)->number == 10.0);
    CHECK(kpa_json_element(&arena->document, list, 1u)->number == 20.0);
    CHECK(kpa_json_element(&arena->document, list, 2u)->number == 30.0);
    CHECK(kpa_json_element(&arena->document, list, 3u) == NULL);
    CHECK(kpa_json_element(&arena->document, list, UINT32_MAX) == NULL);
    CHECK(kpa_json_element(&arena->document, root, 0u) == NULL);
    CHECK(kpa_json_element(&arena->document, NULL, 0u) == NULL);
    CHECK(kpa_json_element(NULL, list, 0u) == NULL);

    CHECK(kpa_json_member(&arena->document, root, "absent") == NULL);
    CHECK(kpa_json_member(&arena->document, list, "0") == NULL);
    CHECK(kpa_json_member(&arena->document, root, NULL) == NULL);
    CHECK(kpa_json_member(&arena->document, NULL, "name") == NULL);
    CHECK(kpa_json_member(NULL, root, "name") == NULL);
    /* A key prefix must not match, in either direction. */
    CHECK(kpa_json_member(&arena->document, root, "nam") == NULL);
    CHECK(kpa_json_member(&arena->document, root, "names") == NULL);
    CHECK(kpa_json_member(&arena->document, root, "nested") != NULL);

    CHECK(!kpa_json_number(&arena->document, root, "zero", NULL));
    CHECK(!kpa_json_bool(&arena->document, root, "false", NULL));
    CHECK(!kpa_json_string(&arena->document, root, "name", NULL, &length));
    CHECK(!kpa_json_string(&arena->document, root, "name", &text, NULL));

    /* Sibling order is document order. */
    {
        kpa_json_ref child = root->first_child;
        const kpa_json_node *first = kpa_json_at(&arena->document, child);
        CHECK(first != NULL && first->key_length == 4u);
        CHECK(memcmp(first->key, "zero", 4u) == 0);
    }
    return true;
}

#endif /* KPA_FUZZ_JSON */

/* --------------------------------------------------------------- fuzz */

/*
 * Walk every node the parser produced and read every byte of every string it
 * points at.  A borrowed string that outran the caller's buffer, or a scratch
 * offset that outran the arena, is a read past the end of a heap allocation
 * here, which is what the sanitizer is for.  `sink` exists so the reads
 * cannot be optimized away.
 */
static bool walk_document(const kpa_json_document *document, uint64_t *sink)
{
    uint64_t total = 0u;
    uint32_t index;

    for (index = 0u; index < document->node_count; index++) {
        const kpa_json_node *node = &document->nodes[index];
        size_t offset;
        uint32_t seen = 0u;
        kpa_json_ref child;

        if (node->first_child >= document->node_count) return false;
        if (node->next_sibling >= document->node_count) return false;
        if (index == 0u && (node->key != NULL || node->key_length != 0u))
            return false;
        if (node->type != KPA_JSON_STRING && node->text != NULL) return false;
        if (node->type != KPA_JSON_OBJECT && node->type != KPA_JSON_ARRAY &&
            (node->first_child != 0u || node->child_count != 0u))
            return false;
        for (offset = 0u; offset < node->key_length; offset++)
            total += (unsigned char)node->key[offset];
        if (node->type == KPA_JSON_STRING)
            for (offset = 0u; offset < node->length; offset++)
                total += (unsigned char)node->text[offset];
        for (child = node->first_child; child != 0u; seen++) {
            if (child >= document->node_count) return false;
            if (seen > document->node_count) return false;
            child = document->nodes[child].next_sibling;
        }
        if (seen != node->child_count) return false;
    }
    *sink += total;
    return true;
}

static bool result_is_defined(kpa_json_result result)
{
    switch (result) {
    case KPA_JSON_OK:
    case KPA_JSON_INVALID_ARGUMENT:
    case KPA_JSON_SYNTAX:
    case KPA_JSON_DEPTH:
    case KPA_JSON_NO_SPACE:
    case KPA_JSON_UTF8:
    case KPA_JSON_DUPLICATE_KEY:
    case KPA_JSON_RANGE:
        return true;
    default:
        break;
    }
    return false;
}

/*
 * One parse against buffers sized exactly to the arena the caller asked for,
 * so any overrun of the nodes array, the scratch tail or the document text is
 * a heap overflow rather than a quiet write into slack space.
 */
static bool fuzz_one(const unsigned char *bytes, size_t length,
                     uint32_t node_capacity, size_t scratch_capacity,
                     kpa_json_result *out_result, uint64_t *sink)
{
    kpa_json_document document;
    kpa_json_node *nodes = malloc(node_capacity * sizeof *nodes);
    char *scratch = (scratch_capacity > 0u) ? malloc(scratch_capacity) : NULL;
    char *text = malloc(length > 0u ? length : 1u);
    bool ok = false;

    if (nodes != NULL && text != NULL &&
        (scratch_capacity == 0u || scratch != NULL)) {
        kpa_json_result result;

        if (length > 0u) memcpy(text, bytes, length);
        kpa_json_document_init(&document, nodes, node_capacity, scratch,
                               scratch_capacity);
        result = kpa_json_parse(&document, text, length);
        ok = result_is_defined(result);
        if (ok && result != KPA_JSON_OK)
            ok = document.node_count == 0u &&
                 kpa_json_root(&document) == NULL;
        if (ok && result == KPA_JSON_OK)
            ok = document.node_count > 0u && walk_document(&document, sink);
        if (out_result != NULL) *out_result = result;
    }
    free(nodes);
    free(scratch);
    free(text);
    return ok;
}

#ifdef KPA_FUZZ_JSON

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    uint64_t sink = 0u;
    uint32_t node_capacity = 1u + (uint32_t)(size % 128u);
    size_t scratch_capacity = size;

    if (!fuzz_one(data, size, node_capacity, scratch_capacity, NULL, &sink))
        abort();
    if (!fuzz_one(data, size, 1u, 0u, NULL, &sink)) abort();
    return 0;
}

#else

#define FUZZ_ITERATIONS 200000u
#define FUZZ_MAX_BYTES 512u

static uint64_t fuzz_random(uint64_t *state)
{
    uint64_t value = *state;

    value ^= value << 13;
    value ^= value >> 7;
    value ^= value << 17;
    *state = value;
    return value;
}

/*
 * A seeded mutation loop stands in for a real fuzzer here: the same 200000
 * documents every run, so a failure is reproducible from the iteration
 * number alone, and no fuzzer needs to be installed for the gate to mean
 * something.  KPA_FUZZ_JSON above is the same body wired to libFuzzer.
 */
static bool test_fuzz_mutations(void)
{
    /* The corpus is chosen so that a handful of byte edits can reach every
     * result code: one document sits one container short of the depth limit,
     * and one has near-miss keys a single edit turns into a duplicate. */
    static const char *const seeds[] = {
        "{\"title\":\"caf\\u00e9 \\ud83d\\ude00\",\"bpm\":161.0,"
        "\"sections\":[{\"start\":25.755,\"bars\":40,\"loop\":true},"
        "{\"start\":1800,\"bars\":8,\"loop\":false,\"tag\":null}],"
        "\"nested\":[[[1,2,3],[]],{\"k\":\"v\"}],\"empty\":{}}",
        "[0,-1,1e308,5e-324,0.1,[[[[[[1]]]]]],\"\\u0000\\\\\\\"\",true]",
        "{\"a\":{\"b\":{\"c\":{\"d\":[{\"e\":\"\\ud800\\udc00\"}]}}}}",
        "[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[9.5]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]",
        "{\"aa\":1,\"ab\":2,\"ac\":[3],\"ad\":{\"ba\":4,\"bb\":5}}"
    };
    static const unsigned char interesting[] = {
        '{', '}', '[', ']', '"', ':', ',', '\\', 'u', 'e', 'E', '.', '-', '+',
        '0', '1', '9', 't', 'f', 'n', ' ', 0x00u, 0x1fu, 0x7fu, 0x80u, 0xbfu,
        0xc0u, 0xc2u, 0xe0u, 0xedu, 0xf0u, 0xf4u, 0xf5u, 0xffu
    };
    unsigned char buffer[FUZZ_MAX_BYTES];
    unsigned long counts[8] = {0ul, 0ul, 0ul, 0ul, 0ul, 0ul, 0ul, 0ul};
    uint64_t state = UINT64_C(0x243f6a8885a308d3);
    uint64_t sink = 0u;
    unsigned long iteration;

    for (iteration = 0ul; iteration < FUZZ_ITERATIONS; iteration++) {
        const char *seed =
            seeds[fuzz_random(&state) % (sizeof seeds / sizeof seeds[0])];
        size_t length = strlen(seed);
        unsigned int mutation;
        unsigned int mutation_count;
        uint32_t node_capacity;
        size_t scratch_capacity;
        kpa_json_result result = KPA_JSON_OK;

        if (length > FUZZ_MAX_BYTES - 1u) return false;
        memcpy(buffer, seed, length);
        mutation_count = 1u + (unsigned int)(fuzz_random(&state) % 4u);
        for (mutation = 0u; mutation < mutation_count; mutation++) {
            const uint64_t roll = fuzz_random(&state);
            const size_t at = (length > 0u) ? (size_t)(roll % length) : 0u;

            switch ((roll >> 40) % 6u) {
            case 0:
                if (length > 0u)
                    buffer[at] = (unsigned char)(fuzz_random(&state) & 0xffu);
                break;
            case 1:
                if (length > 0u)
                    buffer[at] = interesting[fuzz_random(&state) %
                                             sizeof interesting];
                break;
            case 2:
                length = at;
                break;
            case 3:
                if (length + 1u < FUZZ_MAX_BYTES) {
                    memmove(buffer + at + 1u, buffer + at, length - at);
                    buffer[at] = interesting[fuzz_random(&state) %
                                             sizeof interesting];
                    length++;
                }
                break;
            case 4:
                /* A run, so nesting can outgrow the depth limit and a key can
                 * grow into a copy of one already in the same object. */
                {
                    const unsigned char fill =
                        interesting[fuzz_random(&state) % sizeof interesting];
                    size_t run = (size_t)(fuzz_random(&state) % 40u) + 1u;

                    if (run > FUZZ_MAX_BYTES - 1u - length)
                        run = FUZZ_MAX_BYTES - 1u - length;
                    memmove(buffer + at + run, buffer + at, length - at);
                    memset(buffer + at, fill, run);
                    length += run;
                }
                break;
            default:
                if (length > 0u) {
                    memmove(buffer + at, buffer + at + 1u, length - at - 1u);
                    length--;
                }
                break;
            }
        }
        node_capacity = 1u + (uint32_t)(fuzz_random(&state) % 96u);
        scratch_capacity = (size_t)(fuzz_random(&state) % 64u);
        if (!fuzz_one(buffer, length, node_capacity, scratch_capacity,
                      &result, &sink)) {
            (void)fprintf(stderr,
                          "%s:%d: fuzz iteration %lu failed (%zu bytes, "
                          "%u nodes, %zu scratch)\n",
                          __FILE__, __LINE__, iteration, length,
                          node_capacity, scratch_capacity);
            return false;
        }
        counts[(size_t)result]++;
    }
    (void)printf("fuzz: %u mutations, no crash; results", FUZZ_ITERATIONS);
    {
        size_t code;
        for (code = 0u; code < 8u; code++)
            (void)printf(" %s=%lu",
                         kpa_json_result_name((kpa_json_result)code),
                         counts[code]);
    }
    (void)printf(" (checksum %llu)\n", (unsigned long long)sink);
    /* Every code reachable from a mutated document must actually appear, or
     * the loop is not exercising what its name claims. */
    if (counts[KPA_JSON_OK] == 0ul || counts[KPA_JSON_SYNTAX] == 0ul ||
        counts[KPA_JSON_UTF8] == 0ul || counts[KPA_JSON_NO_SPACE] == 0ul ||
        counts[KPA_JSON_DUPLICATE_KEY] == 0ul ||
        counts[KPA_JSON_DEPTH] == 0ul || counts[KPA_JSON_RANGE] == 0ul)
        return false;
    return true;
}

int main(void)
{
    test_arena *arena = malloc(sizeof *arena);
    bool ok;

    if (arena == NULL) return EXIT_FAILURE;
    ok = test_result_names() && test_invalid_argument(arena) &&
         test_empty_and_whitespace(arena) && test_top_level_scalars(arena) &&
         test_syntax_rejections(arena) && test_depth(arena) &&
         test_duplicate_keys(arena) && test_utf8_rejections(arena) &&
         test_escapes(arena) && test_string_borrowing(arena) &&
         test_numbers(arena) && test_number_range(arena) &&
         test_long_significands(arena) &&
         test_arena_exhaustion() && test_accessors(arena) &&
         test_fuzz_mutations();
    free(arena);
    if (!ok) return EXIT_FAILURE;
    (void)printf("ok: kpa_json (%zu number cases, %u fuzz mutations)\n",
                 sizeof number_cases / sizeof number_cases[0],
                 FUZZ_ITERATIONS);
    return 0;
}

#endif
