/*
 * Tests for the UTF-8 terminal-cell overlay.
 *
 * Two things are worth saying about how this is written.  First, the UTF-8
 * acceptance test does not compare against a second copy of the same range
 * table: it decodes naively, re-encodes, and demands the bytes come back
 * identical, which is what makes it an independent oracle for overlong
 * forms.  Second, no lyric, stem, tab or media of any kind appears here;
 * every fixture is a code point synthesised at run time.
 */
#include "kilix_playalong/kpa_cells.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define CHECK(condition)                                                   \
    do {                                                                   \
        if (!(condition)) {                                                \
            (void)fprintf(stderr, "%s:%d: check failed: %s\n",             \
                          __FILE__, __LINE__, #condition);                 \
            return false;                                                  \
        }                                                                  \
    } while (false)

#define CHECK_CODE(condition, code)                                        \
    do {                                                                   \
        if (!(condition)) {                                                \
            (void)fprintf(stderr, "%s:%d: U+%04lX: %s\n",                  \
                          __FILE__, __LINE__, (unsigned long)(code),       \
                          #condition);                                     \
            return false;                                                  \
        }                                                                  \
    } while (false)

/* ------------------------------------------------- independent oracle */

/* Straightforward encoder; 0 for surrogates and anything past U+10FFFF. */
static size_t encode_utf8(uint32_t code, unsigned char *out)
{
    if (code > 0x10FFFFu || (code >= 0xD800u && code <= 0xDFFFu)) return 0u;
    if (code < 0x80u) {
        out[0] = (unsigned char)code;
        return 1u;
    }
    if (code < 0x800u) {
        out[0] = (unsigned char)(0xC0u | (code >> 6));
        out[1] = (unsigned char)(0x80u | (code & 0x3Fu));
        return 2u;
    }
    if (code < 0x10000u) {
        out[0] = (unsigned char)(0xE0u | (code >> 12));
        out[1] = (unsigned char)(0x80u | ((code >> 6) & 0x3Fu));
        out[2] = (unsigned char)(0x80u | (code & 0x3Fu));
        return 3u;
    }
    out[0] = (unsigned char)(0xF0u | (code >> 18));
    out[1] = (unsigned char)(0x80u | ((code >> 12) & 0x3Fu));
    out[2] = (unsigned char)(0x80u | ((code >> 6) & 0x3Fu));
    out[3] = (unsigned char)(0x80u | (code & 0x3Fu));
    return 4u;
}

/*
 * Bytes consumed by a well-formed sequence at the head, 0 otherwise.  The
 * re-encode comparison is doing the real work: an overlong form decodes to
 * a code point whose canonical encoding is shorter, so the memcmp fails
 * without this oracle ever knowing what "overlong" means.
 */
static size_t reference_step(const unsigned char *bytes, size_t available)
{
    unsigned char again[4];
    uint32_t code;
    size_t needed;

    if (available == 0u) return 0u;
    if (bytes[0] < 0x80u) {
        needed = 1u;
        code = bytes[0];
    } else if ((bytes[0] & 0xE0u) == 0xC0u) {
        needed = 2u;
        code = (uint32_t)(bytes[0] & 0x1Fu);
    } else if ((bytes[0] & 0xF0u) == 0xE0u) {
        needed = 3u;
        code = (uint32_t)(bytes[0] & 0x0Fu);
    } else if ((bytes[0] & 0xF8u) == 0xF0u) {
        needed = 4u;
        code = (uint32_t)(bytes[0] & 0x07u);
    } else {
        return 0u;   /* 0x80..0xBF continuations and the 5/6-byte leads */
    }
    if (needed > available) return 0u;
    for (size_t index = 1u; index < needed; ++index) {
        if ((bytes[index] & 0xC0u) != 0x80u) return 0u;
        code = (code << 6) | (uint32_t)(bytes[index] & 0x3Fu);
    }
    if (encode_utf8(code, again) != needed) return 0u;
    return memcmp(again, bytes, needed) == 0 ? needed : 0u;
}

static bool reference_valid_string(const unsigned char *bytes, size_t count)
{
    size_t offset = 0u;

    while (offset < count) {
        const size_t consumed = reference_step(bytes + offset,
                                               count - offset);
        if (consumed == 0u) return false;
        offset += consumed;
    }
    return true;
}

static size_t count_occurrences(const unsigned char *haystack, size_t length,
                                const char *needle)
{
    const size_t needle_length = strlen(needle);
    size_t found = 0u;

    if (needle_length == 0u || needle_length > length) return 0u;
    for (size_t offset = 0u; offset + needle_length <= length; ++offset)
        if (memcmp(haystack + offset, needle, needle_length) == 0) ++found;
    return found;
}

static size_t count_byte(const unsigned char *haystack, size_t length,
                         unsigned char wanted)
{
    size_t found = 0u;

    for (size_t offset = 0u; offset < length; ++offset)
        if (haystack[offset] == wanted) ++found;
    return found;
}

/* ------------------------------------------------------ utf-8 validity */

typedef struct utf8_case {
    const char *bytes;
    size_t length;
    bool valid;
    const char *what;
} utf8_case;

static bool test_utf8_matrix(void)
{
    static const utf8_case cases[] = {
        {"", 0u, true, "empty"},
        {"abc", 3u, true, "ascii"},
        {"a\0b", 3u, true, "an embedded NUL is well-formed UTF-8"},
        {"\x7F", 1u, true, "DEL is well-formed UTF-8"},
        {"\x1B[31m", 5u, true, "an escape sequence is well-formed UTF-8"},
        {"\xC2\xA9", 2u, true, "U+00A9"},
        {"\xC2\x80", 2u, true, "U+0080, shortest 2-byte form"},
        {"\xC3\xA9", 2u, true, "U+00E9"},
        {"\xDF\xBF", 2u, true, "U+07FF, longest 2-byte form"},
        {"\xE0\xA0\x80", 3u, true, "U+0800, shortest 3-byte form"},
        {"\xE4\xB8\x96", 3u, true, "U+4E16"},
        {"\xED\x9F\xBF", 3u, true, "U+D7FF, last before the surrogates"},
        {"\xEE\x80\x80", 3u, true, "U+E000, first after the surrogates"},
        {"\xEF\xB7\x90", 3u, true, "U+FDD0, a noncharacter still encodes"},
        {"\xEF\xBB\xBF", 3u, true, "U+FEFF"},
        {"\xEF\xBF\xBF", 3u, true, "U+FFFF, longest 3-byte form"},
        {"\xF0\x90\x80\x80", 4u, true, "U+10000, shortest 4-byte form"},
        {"\xF0\x9F\x98\x80", 4u, true, "U+1F600"},
        {"\xF4\x8F\xBF\xBF", 4u, true, "U+10FFFF, the last code point"},

        {"\x80", 1u, false, "lone continuation byte"},
        {"\xBF", 1u, false, "lone continuation byte"},
        {"\xC0\x80", 2u, false, "overlong NUL"},
        {"\xC0\xAF", 2u, false, "overlong solidus"},
        {"\xC1\xBF", 2u, false, "overlong U+007F"},
        {"\xC2", 1u, false, "truncated 2-byte form"},
        {"\xC3\x28", 2u, false, "2-byte form with a bad continuation"},
        {"\xE0\x80\x80", 3u, false, "overlong 3-byte NUL"},
        {"\xE0\x9F\xBF", 3u, false, "overlong U+07FF"},
        {"\xE4\xB8", 2u, false, "truncated 3-byte form"},
        {"\xE4\x28\x96", 3u, false, "3-byte form with a bad continuation"},
        {"\xED\xA0\x80", 3u, false, "U+D800 high surrogate"},
        {"\xED\xAF\xBF", 3u, false, "U+DBFF high surrogate"},
        {"\xED\xB0\x80", 3u, false, "U+DC00 low surrogate"},
        {"\xED\xBF\xBF", 3u, false, "U+DFFF low surrogate"},
        {"\xED\xA0\xBD\xED\xB8\x80", 6u, false, "CESU-8 surrogate pair"},
        {"\xF0\x80\x80\x80", 4u, false, "overlong 4-byte NUL"},
        {"\xF0\x8F\xBF\xBF", 4u, false, "overlong U+FFFF"},
        {"\xF0\x9F\x98", 3u, false, "truncated 4-byte form"},
        {"\xF4\x90\x80\x80", 4u, false, "U+110000, past the last code point"},
        {"\xF5\x80\x80\x80", 4u, false, "F5 can never lead"},
        {"\xF7\xBF\xBF\xBF", 4u, false, "F7 can never lead"},
        {"\xF8\x88\x80\x80\x80", 5u, false, "5-byte form"},
        {"\xFC\x84\x80\x80\x80\x80", 6u, false, "6-byte form"},
        {"\xFE", 1u, false, "FE never appears in UTF-8"},
        {"\xFF", 1u, false, "FF never appears in UTF-8"},
        {"ab\xC3", 3u, false, "truncated tail after valid text"},
        {"\xC3\xA9\x80", 3u, false, "stray continuation after a pair"}
    };
    const size_t count = sizeof cases / sizeof cases[0];

    for (size_t index = 0u; index < count; ++index) {
        const utf8_case *item = &cases[index];
        const bool got = kpa_cells_valid_utf8(item->bytes, item->length);

        if (got != item->valid) {
            (void)fprintf(stderr, "%s:%d: %s: expected %s, got %s\n",
                          __FILE__, __LINE__, item->what,
                          item->valid ? "valid" : "invalid",
                          got ? "valid" : "invalid");
            return false;
        }
        /* The oracle has to agree with the table, on every row. */
        if (got != reference_valid_string(
                       (const unsigned char *)item->bytes, item->length)) {
            (void)fprintf(stderr, "%s:%d: %s: oracle disagrees\n",
                          __FILE__, __LINE__, item->what);
            return false;
        }
    }
    CHECK(kpa_cells_valid_utf8(NULL, 0u));
    CHECK(!kpa_cells_valid_utf8(NULL, 4u));
    return true;
}

static bool test_utf8_exhaustive_parity(void)
{
    static const unsigned char probes[] = {
        0x00u, 0x01u, 0x41u, 0x7Fu, 0x80u, 0x81u, 0x8Fu, 0x90u, 0x9Fu,
        0xA0u, 0xBEu, 0xBFu, 0xC0u, 0xC1u, 0xC2u, 0xDFu, 0xE0u, 0xE1u,
        0xECu, 0xEDu, 0xEEu, 0xEFu, 0xF0u, 0xF1u, 0xF4u, 0xF5u, 0xF7u,
        0xF8u, 0xFCu, 0xFEu, 0xFFu
    };
    const size_t probe_count = sizeof probes / sizeof probes[0];
    unsigned char sequence[4];

    /* Every one- and two-byte string, exhaustively. */
    for (unsigned int b0 = 0u; b0 < 256u; ++b0) {
        sequence[0] = (unsigned char)b0;
        CHECK(kpa_cells_valid_utf8((const char *)sequence, 1u) ==
              reference_valid_string(sequence, 1u));
        for (unsigned int b1 = 0u; b1 < 256u; ++b1) {
            sequence[1] = (unsigned char)b1;
            CHECK(kpa_cells_valid_utf8((const char *)sequence, 2u) ==
                  reference_valid_string(sequence, 2u));
        }
    }
    /* Three-byte strings over every lead and every second byte. */
    for (unsigned int b0 = 0u; b0 < 256u; ++b0) {
        sequence[0] = (unsigned char)b0;
        for (unsigned int b1 = 0u; b1 < 256u; ++b1) {
            sequence[1] = (unsigned char)b1;
            for (size_t index = 0u; index < probe_count; ++index) {
                sequence[2] = probes[index];
                CHECK(kpa_cells_valid_utf8((const char *)sequence, 3u) ==
                      reference_valid_string(sequence, 3u));
            }
        }
    }
    /*
     * Four-byte strings: the second byte exhaustively, because E0/ED/F0/F4
     * put their overlong and out-of-range bounds there and nowhere else.
     */
    for (size_t lead = 0u; lead < probe_count; ++lead) {
        sequence[0] = probes[lead];
        for (unsigned int b1 = 0u; b1 < 256u; ++b1) {
            sequence[1] = (unsigned char)b1;
            for (size_t second = 0u; second < probe_count; ++second) {
                sequence[2] = probes[second];
                for (size_t third = 0u; third < probe_count; ++third) {
                    sequence[3] = probes[third];
                    CHECK(kpa_cells_valid_utf8((const char *)sequence, 4u) ==
                          reference_valid_string(sequence, 4u));
                }
            }
        }
    }
    return true;
}

/* -------------------------------------------------------------- width */

typedef struct width_case {
    const char *bytes;
    size_t length;
    int width;
    const char *what;
} width_case;

static bool test_width(void)
{
    static const width_case cases[] = {
        {"", 0u, 0, "empty"},
        {"hello", 5u, 5, "ascii"},
        {" ", 1u, 1, "space"},
        {"caf\xC3\xA9", 5u, 4, "latin-1 accent"},
        {"\xC2\xA0", 2u, 1, "no-break space"},
        {"\xC2\xAD", 2u, 1, "soft hyphen counts one, by documented choice"},
        {"\xCE\xB1\xCE\xB2\xCE\xB3", 6u, 3, "greek"},
        {"\xD0\xB4\xD0\xB0", 4u, 2, "cyrillic"},
        {"\xE4\xB8\x96\xE7\x95\x8C", 6u, 4, "CJK is wide"},
        {"\xED\x95\x9C\xEA\xB5\xAD", 6u, 4, "hangul syllables are wide"},
        {"\xE1\x84\x80", 3u, 2, "hangul jamo U+1100 is wide"},
        {"\xEF\xBC\xA1", 3u, 2, "fullwidth U+FF21 is wide"},
        {"e\xCC\x81", 3u, 1, "e plus combining acute"},
        {"e\xCC\x81\xCC\xA7", 5u, 1, "e plus two combining marks"},
        {"\xCC\x81", 2u, 0, "a bare combining mark is zero"},
        {"\xE3\x82\x99", 3u, 0, "U+3099 is Mn even though EAW says wide"},
        {"\xE2\x80\x8B", 3u, 0, "U+200B zero width space"},
        {"\xE2\x80\x8C", 3u, 0, "U+200C ZWNJ"},
        {"\xE2\x80\x8D", 3u, 0, "U+200D ZWJ"},
        {"\xEF\xBB\xBF", 3u, 0, "U+FEFF"},
        {"\xEF\xB8\x8F", 3u, 0, "U+FE0F variation selector"},
        {"\xF0\x9F\x98\x80", 4u, 2, "emoji is wide"},
        {"\xF0\x9F\x91\xA8\xE2\x80\x8D\xF0\x9F\x91\xA9", 11u, 4,
         "a ZWJ pair sums per code point, no grapheme clustering"},
        {"\xF0\x9F\x87\xBA\xF0\x9F\x87\xB8", 8u, 2,
         "regional indicators are narrow, so a flag is two columns"},

        {"\x1B", 1u, -1, "ESC is refused"},
        {"\n", 1u, -1, "newline is refused"},
        {"\r", 1u, -1, "carriage return is refused"},
        {"\t", 1u, -1, "tab is refused"},
        {"\x7F", 1u, -1, "DEL is refused"},
        {"a\0b", 3u, -1, "an embedded NUL is refused"},
        {"\xC2\x80", 2u, -1, "U+0080 C1 is refused"},
        {"\xC2\x85", 2u, -1, "U+0085 NEL is refused"},
        {"\xC2\x9B", 2u, -1, "U+009B, the C1 CSI, is refused"},
        {"\xC2\x9F", 2u, -1, "U+009F is refused"},
        {"a\x1B[31mb", 7u, -1, "an embedded escape refuses the whole line"},
        {"\xC0\x80", 2u, -1, "invalid UTF-8"},
        {"\xED\xA0\x80", 3u, -1, "a surrogate"},
        {"ab\xC3", 3u, -1, "a truncated tail"}
    };
    const size_t count = sizeof cases / sizeof cases[0];

    for (size_t index = 0u; index < count; ++index) {
        const width_case *item = &cases[index];
        const int got = kpa_cells_width(item->bytes, item->length);

        if (got != item->width) {
            (void)fprintf(stderr, "%s:%d: %s: expected %d, got %d\n",
                          __FILE__, __LINE__, item->what, item->width, got);
            return false;
        }
    }
    CHECK(kpa_cells_width(NULL, 0u) == 0);
    CHECK(kpa_cells_width(NULL, 3u) == -1);
    /* U+00A0 is not a control even though it sits next to the C1 block. */
    CHECK(kpa_cells_width("\xC2\xA0", 2u) == 1);
    return true;
}

static bool test_repertoire_sweep(void)
{
    unsigned char bytes[4];

    for (uint32_t code = 0u; code <= 0x10FFFFu; ++code) {
        const char *text = (const char *)bytes;
        size_t length;
        int width;

        if (code >= 0xD800u && code <= 0xDFFFu) continue;
        length = encode_utf8(code, bytes);
        CHECK_CODE(length >= 1u && length <= 4u, code);
        CHECK_CODE(kpa_cells_valid_utf8(text, length), code);
        width = kpa_cells_width(text, length);
        if (code < 0x20u || (code >= 0x7Fu && code <= 0x9Fu)) {
            CHECK_CODE(width == -1, code);
            CHECK_CODE(kpa_cells_fit(text, length, 80) == 0u, code);
        } else if (width == 0) {
            CHECK_CODE(kpa_cells_fit(text, length, 0) == 0u, code);
            if (code == 0x200Cu || code == 0x200Du)
                CHECK_CODE(kpa_cells_fit(text, length, 1) == 0u, code);
            else
                CHECK_CODE(kpa_cells_fit(text, length, 1) == length, code);
        } else {
            CHECK_CODE(width == 1 || width == 2, code);
            CHECK_CODE(kpa_cells_fit(text, length, width) == length, code);
            CHECK_CODE(kpa_cells_fit(text, length, width - 1) == 0u, code);
        }
    }
    return true;
}

/* ---------------------------------------------------------------- fit */

typedef struct fit_case {
    const char *bytes;
    size_t length;
    int columns;
    size_t fitted;
    const char *what;
} fit_case;

static bool test_fit(void)
{
    /* "a" U+4E16 "b": one narrow, one wide, one narrow. */
    static const char wide_text[] = "a\xE4\xB8\x96" "b";
    /* "a" "e" U+0301 "b": a base that carries a combining mark. */
    static const char mark_text[] = "ae\xCC\x81" "b";
    /* U+1F468 U+200D U+1F469: a joiner between two wide code points. */
    static const char joined[] = "\xF0\x9F\x91\xA8\xE2\x80\x8D"
                                 "\xF0\x9F\x91\xA9";
    static const fit_case cases[] = {
        {"hello", 5u, 0, 0u, "zero columns fits nothing"},
        {"hello", 5u, -3, 0u, "negative columns fits nothing"},
        {"hello", 5u, 3, 3u, "ascii prefix"},
        {"hello", 5u, 5, 5u, "exactly"},
        {"hello", 5u, 99, 5u, "more room than text"},
        {"", 0u, 10, 0u, "empty"},

        {wide_text, 5u, 0, 0u, "wide: nothing"},
        {wide_text, 5u, 1, 1u, "wide: the narrow lead only"},
        {wide_text, 5u, 2, 1u, "wide: one column short, never half a glyph"},
        {wide_text, 5u, 3, 4u, "wide: lead plus the wide glyph"},
        {wide_text, 5u, 4, 5u, "wide: everything"},
        {wide_text, 5u, 9, 5u, "wide: room to spare"},

        {mark_text, 5u, 1, 1u, "mark: the first base only"},
        {mark_text, 5u, 2, 4u, "mark: the mark rides with its base"},
        {mark_text, 5u, 3, 5u, "mark: everything"},
        {"\xCC\x81" "a", 3u, 1, 3u, "a leading mark has no base to strand"},

        {joined, 11u, 1, 0u, "joined: no room for the first glyph"},
        {joined, 11u, 2, 4u, "joined: a dangling ZWJ is dropped"},
        {joined, 11u, 3, 4u, "joined: still dangling at three columns"},
        {joined, 11u, 4, 11u, "joined: everything"},
        {"a\xE2\x80\x8C", 4u, 9, 1u, "a dangling ZWNJ is dropped too"},

        {"ab\nc", 4u, 10, 2u, "stops at a control byte"},
        {"ab\x1B[31m", 7u, 10, 2u, "stops at an escape"},
        {"ab\xC3", 3u, 10, 2u, "stops at a truncated tail"},
        {"ab\xC0\x80" "c", 5u, 10, 2u, "stops at an overlong form"},
        {"ab\xED\xA0\x80" "c", 6u, 10, 2u, "stops at a surrogate"}
    };
    const size_t count = sizeof cases / sizeof cases[0];

    for (size_t index = 0u; index < count; ++index) {
        const fit_case *item = &cases[index];
        const size_t got = kpa_cells_fit(item->bytes, item->length,
                                         item->columns);

        if (got != item->fitted) {
            (void)fprintf(stderr, "%s:%d: %s: expected %zu, got %zu\n",
                          __FILE__, __LINE__, item->what, item->fitted, got);
            return false;
        }
    }
    CHECK(kpa_cells_fit(NULL, 0u, 10) == 0u);
    CHECK(kpa_cells_fit(NULL, 5u, 10) == 0u);
    return true;
}

static bool test_fit_is_maximal(void)
{
    /* No joiners here, so the greedy prefix is exactly the longest one. */
    static const char mixed[] = "a\xE4\xB8\x96" "e\xCC\x81"
                                "\xF0\x9F\x98\x80" "z\xCE\xB1";
    const size_t length = sizeof mixed - 1u;
    const unsigned char *bytes = (const unsigned char *)mixed;
    size_t previous = 0u;

    CHECK(kpa_cells_width(mixed, length) == 8);
    for (int columns = 0; columns <= 12; ++columns) {
        const size_t fitted = kpa_cells_fit(mixed, length, columns);
        const int width = kpa_cells_width(mixed, fitted);

        CHECK(fitted <= length);
        CHECK(fitted >= previous);                  /* monotone in columns */
        CHECK(reference_valid_string(bytes, fitted));
        CHECK(kpa_cells_valid_utf8(mixed, fitted));
        CHECK(width >= 0 && (columns <= 0 || width <= columns));
        if (fitted < length) {
            const size_t next = reference_step(bytes + fitted,
                                               length - fitted);
            CHECK(next > 0u);
            /* One more code point would have overflowed the budget. */
            CHECK(kpa_cells_width(mixed, fitted + next) > columns);
        }
        previous = fitted;
    }
    return true;
}

/* ------------------------------------------------------------- writer */

typedef struct capture {
    int read_fd;
    int write_fd;
    size_t length;
    unsigned char bytes[65536];
} capture;

static bool capture_open(capture *state)
{
    int fds[2];

    state->read_fd = -1;
    state->write_fd = -1;
    state->length = 0u;
    if (pipe(fds) != 0) return false;
    state->read_fd = fds[0];
    state->write_fd = fds[1];
    return true;
}

/* Closes the write end, then drains to EOF so nothing extra can hide. */
static bool capture_finish(capture *state)
{
    if (close(state->write_fd) != 0) return false;
    state->write_fd = -1;
    for (;;) {
        ssize_t count;

        if (state->length >= sizeof state->bytes) return false;
        count = read(state->read_fd, state->bytes + state->length,
                     sizeof state->bytes - state->length);
        if (count > 0) {
            state->length += (size_t)count;
            continue;
        }
        if (count == 0) break;
        if (errno == EINTR) continue;
        return false;
    }
    if (close(state->read_fd) != 0) return false;
    state->read_fd = -1;
    return true;
}

static void capture_abandon(capture *state)
{
    if (state->write_fd >= 0) (void)close(state->write_fd);
    if (state->read_fd >= 0) (void)close(state->read_fd);
    state->write_fd = -1;
    state->read_fd = -1;
}

static bool test_writer_golden(void)
{
    static const char expected[] =
        "\033[?2026h"
        "\033[2;3H" "\033[38;2;255;128;0m" "hi" "\033[0m\033[K"
        "\033[4;1H\033[0m\033[8X"
        "\033[5;1H" "\033[38;2;0;0;0m" "\xE4\xB8\x96\xE7\x95\x8C"
        "\033[0m\033[K"
        "\033[?2026l";
    const size_t expected_length = sizeof expected - 1u;
    capture *state = calloc(1u, sizeof *state);
    kpa_cells_writer *writer;
    bool ok;

    CHECK(state != NULL);
    if (!capture_open(state)) {
        free(state);
        return false;
    }
    writer = kpa_cells_create(state->write_fd);
    if (writer == NULL) {
        capture_abandon(state);
        free(state);
        return false;
    }
    kpa_cells_begin(writer);
    kpa_cells_row(writer, 2, 3, 10, "hi", 2u, 0xFF8000u);
    kpa_cells_clear_row(writer, 4, 8);
    kpa_cells_row(writer, 5, 1, 4, "\xE4\xB8\x96\xE7\x95\x8C", 6u, 0u);
    kpa_cells_end(writer);
    kpa_cells_destroy(writer);
    ok = capture_finish(state);
    if (ok) {
        ok = state->length == expected_length &&
             memcmp(state->bytes, expected, expected_length) == 0 &&
             /* erase-to-end-of-line on every drawn row, and only there */
             count_occurrences(state->bytes, state->length, "\033[K") == 2u &&
             /* the synchronised-update pair brackets the frame once */
             count_occurrences(state->bytes, state->length,
                               "\033[?2026h") == 1u &&
             count_occurrences(state->bytes, state->length,
                               "\033[?2026l") == 1u &&
             memcmp(state->bytes, "\033[?2026h", 8u) == 0 &&
             memcmp(state->bytes + state->length - 8u,
                    "\033[?2026l", 8u) == 0;
        if (!ok)
            (void)fprintf(stderr, "%s:%d: golden frame mismatch (%zu bytes)\n",
                          __FILE__, __LINE__, state->length);
    }
    free(state);
    return ok;
}

static bool test_writer_every_row_is_erased(void)
{
    capture *state = calloc(1u, sizeof *state);
    kpa_cells_writer *writer;
    bool ok;

    CHECK(state != NULL);
    if (!capture_open(state)) {
        free(state);
        return false;
    }
    writer = kpa_cells_create(state->write_fd);
    if (writer == NULL) {
        capture_abandon(state);
        free(state);
        return false;
    }
    kpa_cells_begin(writer);
    for (int row = 1; row <= 7; ++row)
        kpa_cells_row(writer, row, 1, 20, "row", 3u, 0x112233u);
    kpa_cells_end(writer);
    kpa_cells_destroy(writer);
    ok = capture_finish(state);
    if (ok)
        ok = count_occurrences(state->bytes, state->length, "\033[K") == 7u &&
             count_occurrences(state->bytes, state->length, "\033[0m") == 7u &&
             count_occurrences(state->bytes, state->length,
                               "\033[?2026h") == 1u &&
             count_occurrences(state->bytes, state->length,
                               "\033[?2026l") == 1u;
    free(state);
    return ok;
}

/*
 * Drive one row and compare against the frame that the already-safe prefix
 * would have produced.  The ESC count is the real assertion: six is what a
 * one-row frame costs, so any escape smuggled in through the text shows up
 * as a seventh.
 */
static bool check_sanitised_row(const char *text, size_t length,
                                const char *expected_text,
                                size_t expected_text_length,
                                const char *what)
{
    static const char prefix[] = "\033[?2026h\033[1;1H\033[38;2;0;255;0m";
    static const char suffix[] = "\033[0m\033[K\033[?2026l";
    const size_t prefix_length = sizeof prefix - 1u;
    const size_t suffix_length = sizeof suffix - 1u;
    capture *state = calloc(1u, sizeof *state);
    kpa_cells_writer *writer;
    bool ok;

    if (state == NULL) return false;
    if (!capture_open(state)) {
        free(state);
        return false;
    }
    writer = kpa_cells_create(state->write_fd);
    if (writer == NULL) {
        capture_abandon(state);
        free(state);
        return false;
    }
    kpa_cells_begin(writer);
    kpa_cells_row(writer, 1, 1, 40, text, length, 0x00FF00u);
    kpa_cells_end(writer);
    kpa_cells_destroy(writer);
    ok = capture_finish(state);
    if (ok) {
        ok = state->length ==
                 prefix_length + expected_text_length + suffix_length &&
             memcmp(state->bytes, prefix, prefix_length) == 0 &&
             memcmp(state->bytes + prefix_length, expected_text,
                    expected_text_length) == 0 &&
             memcmp(state->bytes + prefix_length + expected_text_length,
                    suffix, suffix_length) == 0 &&
             count_byte(state->bytes, state->length, 0x1Bu) == 6u;
        if (!ok)
            (void)fprintf(stderr, "%s:%d: %s: %zu bytes, %zu escapes\n",
                          __FILE__, __LINE__, what, state->length,
                          count_byte(state->bytes, state->length, 0x1Bu));
    }
    free(state);
    return ok;
}

static bool test_writer_refuses_text_escapes(void)
{
    CHECK(check_sanitised_row("safe\x1B[31mred", 12u, "safe", 4u,
                              "CSI in the text"));
    CHECK(check_sanitised_row("ok\xC2\x9B" "0c", 6u, "ok", 2u,
                              "C1 CSI in the text"));
    CHECK(check_sanitised_row("line\r\nmore", 10u, "line", 4u,
                              "CR LF in the text"));
    CHECK(check_sanitised_row("tab\there", 8u, "tab", 3u,
                              "tab in the text"));
    CHECK(check_sanitised_row("nul\0after", 9u, "nul", 3u,
                              "NUL in the text"));
    CHECK(check_sanitised_row("bad\xC0\x80" "tail", 9u, "bad", 3u,
                              "overlong form in the text"));
    CHECK(check_sanitised_row("bad\xED\xA0\x80" "tail", 10u, "bad", 3u,
                              "surrogate in the text"));
    CHECK(check_sanitised_row("cut\xE4\xB8", 5u, "cut", 3u,
                              "truncated tail in the text"));
    CHECK(check_sanitised_row("\x1B]0;title\x07", 10u, "", 0u,
                              "an OSC leading the text"));
    /* A clean line still comes through whole, marks and all. */
    CHECK(check_sanitised_row("e\xCC\x81\xE4\xB8\x96", 6u,
                              "e\xCC\x81\xE4\xB8\x96", 6u, "a clean line"));
    return true;
}

static bool test_writer_truncates_on_a_boundary(void)
{
    /*
     * Three-byte code points on purpose: the buffer bound is a power of
     * two, so a boundary-respecting truncation cannot land on it and the
     * emitted length being a multiple of three is real evidence.
     */
    enum { repeats = 4000u };
    static const char prefix[] =
        "\033[?2026h\033[1;1H\033[38;2;255;255;255m";
    static const char suffix[] = "\033[0m\033[K\033[?2026l";
    const size_t prefix_length = sizeof prefix - 1u;
    const size_t suffix_length = sizeof suffix - 1u;
    const size_t text_length = (size_t)repeats * 3u;
    capture *state = calloc(1u, sizeof *state);
    char *text = malloc(text_length);
    kpa_cells_writer *writer;
    size_t emitted = 0u;
    bool ok;

    if (state == NULL || text == NULL) {
        free(state);
        free(text);
        return false;
    }
    for (size_t index = 0u; index < (size_t)repeats; ++index)
        (void)memcpy(text + index * 3u, "\xE4\xB8\x96", 3u);
    if (!capture_open(state)) {
        free(state);
        free(text);
        return false;
    }
    writer = kpa_cells_create(state->write_fd);
    if (writer == NULL) {
        capture_abandon(state);
        free(state);
        free(text);
        return false;
    }
    kpa_cells_begin(writer);
    kpa_cells_row(writer, 1, 1, 1000000, text, text_length, 0xFFFFFFu);
    kpa_cells_end(writer);
    kpa_cells_destroy(writer);
    ok = capture_finish(state);
    if (ok) {
        ok = state->length > prefix_length + suffix_length &&
             memcmp(state->bytes, prefix, prefix_length) == 0;
        if (ok) {
            emitted = state->length - prefix_length - suffix_length;
            ok = memcmp(state->bytes + prefix_length + emitted, suffix,
                        suffix_length) == 0 &&
                 emitted > 0u &&
                 emitted < text_length &&       /* it really was truncated */
                 emitted % 3u == 0u &&          /* on a character boundary */
                 kpa_cells_valid_utf8(
                     (const char *)state->bytes + prefix_length, emitted) &&
                 kpa_cells_width(
                     (const char *)state->bytes + prefix_length, emitted) ==
                     (int)(emitted / 3u) * 2;
            if (!ok)
                (void)fprintf(stderr,
                              "%s:%d: truncation emitted %zu of %zu bytes\n",
                              __FILE__, __LINE__, emitted, text_length);
        }
    }
    free(state);
    free(text);
    return ok;
}

static bool test_writer_frame_guards(void)
{
    static const char expected[] =
        "\033[?2026h"
        "\033[1;1H\033[38;2;1;2;3m\033[0m\033[K"
        "\033[32767;32767H\033[38;2;0;0;255m" "x" "\033[0m\033[K"
        "\033[9;1H\033[0m\033[12X"
        "\033[?2026l";
    const size_t expected_length = sizeof expected - 1u;
    capture *state = calloc(1u, sizeof *state);
    kpa_cells_writer *writer;
    bool ok;

    CHECK(state != NULL);
    if (!capture_open(state)) {
        free(state);
        return false;
    }
    writer = kpa_cells_create(state->write_fd);
    if (writer == NULL) {
        capture_abandon(state);
        free(state);
        return false;
    }
    kpa_cells_end(writer);          /* end before begin emits nothing */
    kpa_cells_begin(writer);
    kpa_cells_begin(writer);        /* a second begin emits nothing */
    kpa_cells_row(writer, 0, -4, 40, NULL, 0u, 0x010203u);
    kpa_cells_row(writer, 99999, 99999, 40, "x", 1u, 0xFFu);
    kpa_cells_clear_row(writer, 9, 0);    /* nothing to clear */
    kpa_cells_clear_row(writer, 9, -7);   /* nothing to clear */
    kpa_cells_clear_row(writer, 9, 12);
    kpa_cells_end(writer);
    kpa_cells_end(writer);          /* a second end emits nothing */
    kpa_cells_destroy(writer);
    ok = capture_finish(state);
    if (ok) {
        ok = state->length == expected_length &&
             memcmp(state->bytes, expected, expected_length) == 0;
        if (!ok)
            (void)fprintf(stderr, "%s:%d: guard frame mismatch (%zu bytes)\n",
                          __FILE__, __LINE__, state->length);
    }
    free(state);
    return ok;
}

static bool test_writer_lifetime(void)
{
    kpa_cells_writer *writer;

    /* Every one of these has to be a no-op rather than a fault. */
    kpa_cells_destroy(NULL);
    kpa_cells_begin(NULL);
    kpa_cells_row(NULL, 1, 1, 10, "x", 1u, 0u);
    kpa_cells_clear_row(NULL, 1, 10);
    kpa_cells_end(NULL);

    /* A writer with no usable descriptor buffers, drops, and frees. */
    writer = kpa_cells_create(-1);
    CHECK(writer != NULL);
    kpa_cells_begin(writer);
    kpa_cells_row(writer, 1, 1, 10, "text", 4u, 0x123456u);
    kpa_cells_end(writer);
    kpa_cells_destroy(writer);

    /* Created and dropped without ever drawing: still one free path. */
    writer = kpa_cells_create(-1);
    CHECK(writer != NULL);
    kpa_cells_destroy(writer);
    return true;
}

/* --------------------------------------------------------------- main */

typedef bool (*test_function)(void);

typedef struct test_entry {
    const char *name;
    test_function run;
} test_entry;

int main(void)
{
    static const test_entry tests[] = {
        {"utf-8 acceptance matrix", test_utf8_matrix},
        {"utf-8 exhaustive parity against a re-encoding oracle",
         test_utf8_exhaustive_parity},
        {"width", test_width},
        {"width and fit over the whole repertoire", test_repertoire_sweep},
        {"fit", test_fit},
        {"fit is the longest prefix", test_fit_is_maximal},
        {"writer golden frame", test_writer_golden},
        {"writer erases every row", test_writer_every_row_is_erased},
        {"writer refuses escapes from the text",
         test_writer_refuses_text_escapes},
        {"writer truncates on a character boundary",
         test_writer_truncates_on_a_boundary},
        {"writer frame guards and coordinate clamping",
         test_writer_frame_guards},
        {"writer lifetime", test_writer_lifetime}
    };
    const size_t count = sizeof tests / sizeof tests[0];
    size_t passed = 0u;

    for (size_t index = 0u; index < count; ++index) {
        if (tests[index].run()) {
            ++passed;
            (void)printf("ok   %s\n", tests[index].name);
        } else {
            (void)printf("FAIL %s\n", tests[index].name);
        }
    }
    (void)printf("kpa-cells: %zu/%zu groups passed\n", passed, count);
    return passed == count ? EXIT_SUCCESS : EXIT_FAILURE;
}
