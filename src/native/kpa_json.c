/*
 * Bounded JSON reader for project documents that came off disk.  kpa_json.h
 * holds the contract; what follows are the three decisions the rest of this
 * file turns on.
 *
 * The parser is iterative.  Nesting costs one entry in a fixed stack of
 * KPA_JSON_MAX_DEPTH frames and never a C stack frame, so no shape of input
 * can walk the machine stack into its guard page, and the depth limit is
 * tested before a frame is written rather than after.
 *
 * Decimal to double conversion is done here instead of through strtod, which
 * takes its decimal separator from LC_NUMERIC: under a locale that spells it
 * ',' every "25.755" in a lyric file would parse as 25 and every timestamp in
 * the document would silently move.  The conversion below is locale-free and
 * rounds exactly as IEEE-754 requires (nearest, ties to even) for every input
 * including subnormals.
 *
 * UTF-8 is validated over the whole input before parsing starts.  That way
 * the scanners only ever step over well-formed sequences, and a byte-wise
 * search for '"' or '\\' cannot land inside a multi-byte character.
 */

#include "kilix_playalong/kpa_json.h"

#include <math.h>
#include <string.h>

/* -------------------------------------------------------------- utf-8 */

/*
 * Well-formed UTF-8 as Unicode defines it, which is narrower than "decodes to
 * a code point": overlong encodings, the surrogate range, anything above
 * U+10FFFF and truncated tails are all rejected here, because each of them is
 * a way to smuggle a second reading of the same bytes past a consumer.
 */
static bool utf8_ok(const unsigned char *bytes, size_t length)
{
    size_t index = 0u;

    while (index < length) {
        const unsigned char lead = bytes[index];
        size_t continuations;
        unsigned char low;
        unsigned char high;
        size_t step;

        if (lead < 0x80u) {
            index++;
            continue;
        }
        if (lead < 0xc2u) return false;     /* stray tail byte, or C0/C1 */
        if (lead < 0xe0u) {
            continuations = 1u;
            low = 0x80u;
            high = 0xbfu;
        } else if (lead < 0xf0u) {
            continuations = 2u;
            low = (lead == 0xe0u) ? 0xa0u : 0x80u;   /* E0 80..9F: overlong */
            high = (lead == 0xedu) ? 0x9fu : 0xbfu;  /* ED A0..BF: surrogate */
        } else if (lead < 0xf5u) {
            continuations = 3u;
            low = (lead == 0xf0u) ? 0x90u : 0x80u;   /* F0 80..8F: overlong */
            high = (lead == 0xf4u) ? 0x8fu : 0xbfu;  /* past U+10FFFF */
        } else {
            return false;
        }
        if (length - index <= continuations) return false;
        if (bytes[index + 1u] < low || bytes[index + 1u] > high) return false;
        for (step = 2u; step <= continuations; step++)
            if ((bytes[index + step] & 0xc0u) != 0x80u) return false;
        index += continuations + 1u;
    }
    return true;
}

/* ------------------------------------------------------ decimal number */

/*
 * An exact decimal, big enough that no double's neighbourhood is ambiguous:
 * the smallest subnormal needs 751 significant digits to write out in full.
 * Digits past the end are dropped and recorded in `truncated`, which only
 * ever makes the stored value smaller than the real one -- the rounding rule
 * below reads that flag to break the exact-halfway case the right way.
 *
 *   value = 0.digits[0] digits[1] ... * 10^point
 */
#define KPA_DECIMAL_DIGITS 800
#define KPA_DECIMAL_MAX_SHIFT 27

typedef struct kpa_decimal {
    uint8_t digits[KPA_DECIMAL_DIGITS];
    int32_t count;
    int32_t point;
    bool truncated;
} kpa_decimal;

static void decimal_trim(kpa_decimal *value)
{
    while (value->count > 0 && value->digits[value->count - 1] == 0u)
        value->count--;
    if (value->count == 0) value->point = 0;
}

/*
 * digits <- digits * 2^shift.  Written most significant last into a scratch
 * buffer because the product carries up to ten new digits off the front and
 * there is nowhere in the array to put them.
 */
static void decimal_left_shift(kpa_decimal *value, unsigned int shift)
{
    uint8_t out[KPA_DECIMAL_DIGITS + 24];
    size_t write = sizeof out;
    uint64_t carry = 0u;
    int32_t index;
    int32_t total;
    int32_t kept;

    for (index = value->count; index-- > 0; ) {
        carry += (uint64_t)value->digits[index] << shift;
        out[--write] = (uint8_t)(carry % 10u);
        carry /= 10u;
    }
    while (carry > 0u) {
        out[--write] = (uint8_t)(carry % 10u);
        carry /= 10u;
    }
    total = (int32_t)(sizeof out - write);
    value->point += total - value->count;
    kept = (total < KPA_DECIMAL_DIGITS) ? total : KPA_DECIMAL_DIGITS;
    for (index = kept; index < total; index++)
        if (out[write + (size_t)index] != 0u) value->truncated = true;
    memcpy(value->digits, out + write, (size_t)kept);
    value->count = kept;
    decimal_trim(value);
}

/* digits <- digits / 2^shift, long division in place. */
static void decimal_right_shift(kpa_decimal *value, unsigned int shift)
{
    const uint64_t mask = (UINT64_C(1) << shift) - 1u;
    int32_t read = 0;
    int32_t write = 0;
    uint64_t accumulator = 0u;

    /* Take in leading digits until there is something to divide. */
    while ((accumulator >> shift) == 0u) {
        if (read >= value->count) {
            if (accumulator == 0u) {
                value->count = 0;
                return;
            }
            while ((accumulator >> shift) == 0u) {
                accumulator *= 10u;
                read++;
            }
            break;
        }
        accumulator = accumulator * 10u + (uint64_t)value->digits[read];
        read++;
    }
    value->point -= read - 1;

    /* The write cursor trails the read cursor, so this stays in place. */
    for (; read < value->count; read++) {
        const uint64_t digit = accumulator >> shift;
        accumulator &= mask;
        value->digits[write++] = (uint8_t)digit;
        accumulator = accumulator * 10u + (uint64_t)value->digits[read];
    }
    while (accumulator > 0u) {
        const uint64_t digit = accumulator >> shift;
        accumulator &= mask;
        if (write < KPA_DECIMAL_DIGITS)
            value->digits[write++] = (uint8_t)digit;
        else if (digit > 0u)
            value->truncated = true;
        accumulator *= 10u;
    }
    value->count = write;
    decimal_trim(value);
}

static void decimal_shift(kpa_decimal *value, int32_t shift)
{
    if (value->count == 0) return;
    while (shift > KPA_DECIMAL_MAX_SHIFT) {
        decimal_left_shift(value, (unsigned int)KPA_DECIMAL_MAX_SHIFT);
        shift -= KPA_DECIMAL_MAX_SHIFT;
        if (value->count == 0) return;
    }
    while (shift < -KPA_DECIMAL_MAX_SHIFT) {
        decimal_right_shift(value, (unsigned int)KPA_DECIMAL_MAX_SHIFT);
        shift += KPA_DECIMAL_MAX_SHIFT;
        if (value->count == 0) return;
    }
    if (shift > 0) decimal_left_shift(value, (unsigned int)shift);
    else if (shift < 0) decimal_right_shift(value, (unsigned int)(-shift));
}

/* Round to nearest, ties to even, cutting the digit string at `at`. */
static bool decimal_round_up(const kpa_decimal *value, int32_t at)
{
    if (at < 0 || at >= value->count) return false;
    if (value->digits[at] == 5u && at + 1 == value->count) {
        /* Truncation means the real value sits above this exact half. */
        if (value->truncated) return true;
        return at > 0 && (value->digits[at - 1] % 2u) != 0u;
    }
    return value->digits[at] >= 5u;
}

static uint64_t decimal_rounded_integer(const kpa_decimal *value)
{
    uint64_t number = 0u;
    int32_t index;

    if (value->point > 20) return UINT64_MAX;
    for (index = 0; index < value->point && index < value->count; index++)
        number = number * 10u + (uint64_t)value->digits[index];
    for (; index < value->point; index++)
        number *= 10u;
    if (decimal_round_up(value, value->point)) number++;
    return number;
}

/* floor(i * log2(10)): how far to shift to move the point i places. */
static const int32_t decimal_pow_shift[9] = {1, 3, 6, 9, 13, 16, 19, 23, 26};

static int32_t decimal_step(int32_t places)
{
    const int32_t table_length =
        (int32_t)(sizeof decimal_pow_shift / sizeof decimal_pow_shift[0]);

    if (places >= table_length) return KPA_DECIMAL_MAX_SHIFT;
    return decimal_pow_shift[places];
}

/*
 * Correctly rounded decimal -> binary64.  The decimal is scaled by powers of
 * two into [0.5, 1) while a binary exponent counts the scaling, which leaves
 * exactly the question a double asks: the top 53 bits of the significand and
 * one rounding decision, both of which the digit string answers exactly.
 * `value` is consumed.
 */
static kpa_json_result decimal_to_double(kpa_decimal *value, bool negative,
                                         double *out)
{
    int32_t exponent = 0;
    uint64_t mantissa;
    double magnitude;

    if (value->count == 0) {
        *out = negative ? -0.0 : 0.0;
        return KPA_JSON_OK;
    }
    if (value->point > 310) return KPA_JSON_RANGE;    /* above DBL_MAX */
    if (value->point < -330) return KPA_JSON_RANGE;   /* rounds to zero */

    while (value->point > 0) {
        const int32_t step = decimal_step(value->point);
        decimal_shift(value, -step);
        exponent += step;
    }
    while (value->point < 0 || (value->point == 0 && value->digits[0] < 5u)) {
        const int32_t step = decimal_step(-value->point);
        decimal_shift(value, step);
        exponent -= step;
    }
    /* [0.5, 1) counts one exponent lower than a significand in [1, 2). */
    exponent--;

    if (exponent < -1022) {
        /* Subnormal: the significand loses a bit for every exponent it is
         * short of the smallest normal, so scale it down and round there. */
        decimal_shift(value, exponent + 1022);
        exponent = -1022;
    }
    if (exponent > 1023) return KPA_JSON_RANGE;

    decimal_shift(value, 53);
    mantissa = decimal_rounded_integer(value);
    if (mantissa == (UINT64_C(1) << 53)) {
        mantissa >>= 1;
        exponent++;
        if (exponent > 1023) return KPA_JSON_RANGE;
    }
    /* A nonzero input that rounds to zero is a value a double cannot hold. */
    if (mantissa == 0u) return KPA_JSON_RANGE;

    magnitude = ldexp((double)mantissa, (int)(exponent - 52));
    *out = negative ? -magnitude : magnitude;
    return KPA_JSON_OK;
}

/* ------------------------------------------------------ number scanner */

/* Exact powers of ten; 10^22 is the last one a double holds exactly. */
static const double decimal_pow10[23] = {
    1e0, 1e1, 1e2, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8, 1e9, 1e10, 1e11,
    1e12, 1e13, 1e14, 1e15, 1e16, 1e17, 1e18, 1e19, 1e20, 1e21, 1e22
};

#define KPA_FAST_DIGITS 19

typedef struct kpa_number_scan {
    kpa_decimal value;
    int64_t point;
    bool significant;
    uint64_t fast;
    int32_t fast_count;
    bool fast_exact;
} kpa_number_scan;

static void number_scan_init(kpa_number_scan *scan)
{
    memset(&scan->value, 0, sizeof scan->value);
    scan->point = 0;
    scan->significant = false;
    scan->fast = 0u;
    scan->fast_count = 0;
    scan->fast_exact = true;
}

/*
 * Digits arrive in reading order.  Zeros ahead of the first significant digit
 * carry no information but do move the point, which is the only reason the
 * fraction flag is needed here.
 */
static void number_push(kpa_number_scan *scan, uint8_t digit, bool fraction)
{
    if (!scan->significant) {
        if (digit == 0u) {
            if (fraction) scan->point--;
            return;
        }
        scan->significant = true;
    }
    if (!fraction) scan->point++;
    if (scan->value.count < KPA_DECIMAL_DIGITS)
        scan->value.digits[scan->value.count++] = digit;
    else if (digit != 0u)
        scan->value.truncated = true;
    if (scan->fast_count < KPA_FAST_DIGITS) {
        scan->fast = scan->fast * 10u + digit;
        scan->fast_count++;
    } else {
        scan->fast_exact = false;
    }
}

static bool is_digit(unsigned char byte)
{
    return byte >= '0' && byte <= '9';
}

/*
 * Clinger's fast path: when both the significand and the power of ten are
 * exact doubles, one multiply or divide is one correctly rounded operation
 * and the result is the same double the slow path would produce.
 */
static bool number_fast_path(const kpa_number_scan *scan, bool negative,
                             double *out)
{
    int64_t exponent;
    double magnitude;

    if (!scan->fast_exact || scan->fast > (UINT64_C(1) << 53)) return false;
    exponent = scan->point - (int64_t)scan->fast_count;
    if (exponent > 22 || exponent < -22) return false;
    if (exponent >= 0)
        magnitude = (double)scan->fast * decimal_pow10[exponent];
    else
        magnitude = (double)scan->fast / decimal_pow10[-exponent];
    *out = negative ? -magnitude : magnitude;
    return true;
}

static int64_t clamp_point(int64_t point)
{
    if (point > 100000) return 100000;
    if (point < -100000) return -100000;
    return point;
}

/* --------------------------------------------------------------- parser */

typedef struct kpa_parser {
    const unsigned char *text;
    size_t length;
    size_t position;
    kpa_json_document *document;
} kpa_parser;

/*
 * Grammar per RFC 8259 section 6, enforced by structure rather than by a
 * cleanup pass: no leading '+', no leading zero, at least one digit on each
 * side of a '.' that is present, and at least one exponent digit.  The
 * exponent is accumulated with a ceiling so a million-digit exponent cannot
 * wrap the accumulator into a small number.
 */
static kpa_json_result scan_number(kpa_parser *parser, double *out)
{
    kpa_number_scan scan;
    size_t index = parser->position;
    bool negative = false;
    bool exponent_negative = false;
    int64_t exponent = 0;
    int64_t point;

    number_scan_init(&scan);
    if (index < parser->length && parser->text[index] == '-') {
        negative = true;
        index++;
    }
    if (index >= parser->length) return KPA_JSON_SYNTAX;
    if (parser->text[index] == '0') {
        number_push(&scan, 0u, false);
        index++;
        if (index < parser->length && is_digit(parser->text[index]))
            return KPA_JSON_SYNTAX;
    } else if (is_digit(parser->text[index])) {
        while (index < parser->length && is_digit(parser->text[index])) {
            number_push(&scan, (uint8_t)(parser->text[index] - '0'), false);
            index++;
        }
    } else {
        return KPA_JSON_SYNTAX;
    }
    if (index < parser->length && parser->text[index] == '.') {
        index++;
        if (index >= parser->length || !is_digit(parser->text[index]))
            return KPA_JSON_SYNTAX;
        while (index < parser->length && is_digit(parser->text[index])) {
            number_push(&scan, (uint8_t)(parser->text[index] - '0'), true);
            index++;
        }
    }
    if (index < parser->length &&
        (parser->text[index] == 'e' || parser->text[index] == 'E')) {
        index++;
        if (index < parser->length &&
            (parser->text[index] == '+' || parser->text[index] == '-')) {
            exponent_negative = parser->text[index] == '-';
            index++;
        }
        if (index >= parser->length || !is_digit(parser->text[index]))
            return KPA_JSON_SYNTAX;
        while (index < parser->length && is_digit(parser->text[index])) {
            if (exponent < 1000000)
                exponent = exponent * 10 + (parser->text[index] - '0');
            index++;
        }
    }
    parser->position = index;

    if (!scan.significant) {
        *out = negative ? -0.0 : 0.0;
        return KPA_JSON_OK;
    }
    point = clamp_point(scan.point +
                        (exponent_negative ? -exponent : exponent));
    scan.point = point;
    if (number_fast_path(&scan, negative, out)) return KPA_JSON_OK;
    scan.value.point = (int32_t)point;
    decimal_trim(&scan.value);
    return decimal_to_double(&scan.value, negative, out);
}

/* ------------------------------------------------------- arena and text */

static kpa_json_result node_alloc(kpa_json_document *document,
                                  kpa_json_ref *out)
{
    if (document->node_count >= document->node_capacity)
        return KPA_JSON_NO_SPACE;
    *out = document->node_count;
    document->node_count++;
    memset(&document->nodes[*out], 0, sizeof document->nodes[*out]);
    return KPA_JSON_OK;
}

static kpa_json_result scratch_write(kpa_json_document *document,
                                     const unsigned char *bytes, size_t count)
{
    if (count == 0u) return KPA_JSON_OK;
    if (count > document->scratch_capacity - document->scratch_used)
        return KPA_JSON_NO_SPACE;
    memcpy(document->scratch + document->scratch_used, bytes, count);
    document->scratch_used += count;
    return KPA_JSON_OK;
}

static kpa_json_result scratch_push(kpa_json_document *document,
                                    unsigned char byte)
{
    return scratch_write(document, &byte, 1u);
}

static bool hex_value(unsigned char byte, uint32_t *out)
{
    if (byte >= '0' && byte <= '9') *out = (uint32_t)(byte - '0');
    else if (byte >= 'a' && byte <= 'f') *out = (uint32_t)(byte - 'a') + 10u;
    else if (byte >= 'A' && byte <= 'F') *out = (uint32_t)(byte - 'A') + 10u;
    else return false;
    return true;
}

static bool read_hex4(const kpa_parser *parser, size_t index, uint32_t *out)
{
    uint32_t code = 0u;
    size_t step;

    if (parser->length - index < 4u) return false;
    for (step = 0u; step < 4u; step++) {
        uint32_t nibble;
        if (!hex_value(parser->text[index + step], &nibble)) return false;
        code = (code << 4) | nibble;
    }
    *out = code;
    return true;
}

static kpa_json_result encode_utf8(kpa_json_document *document,
                                   uint32_t code_point)
{
    unsigned char buffer[4];
    size_t count;

    if (code_point < 0x80u) {
        buffer[0] = (unsigned char)code_point;
        count = 1u;
    } else if (code_point < 0x800u) {
        buffer[0] = (unsigned char)(0xc0u | (code_point >> 6));
        buffer[1] = (unsigned char)(0x80u | (code_point & 0x3fu));
        count = 2u;
    } else if (code_point < 0x10000u) {
        buffer[0] = (unsigned char)(0xe0u | (code_point >> 12));
        buffer[1] = (unsigned char)(0x80u | ((code_point >> 6) & 0x3fu));
        buffer[2] = (unsigned char)(0x80u | (code_point & 0x3fu));
        count = 3u;
    } else {
        buffer[0] = (unsigned char)(0xf0u | (code_point >> 18));
        buffer[1] = (unsigned char)(0x80u | ((code_point >> 12) & 0x3fu));
        buffer[2] = (unsigned char)(0x80u | ((code_point >> 6) & 0x3fu));
        buffer[3] = (unsigned char)(0x80u | (code_point & 0x3fu));
        count = 4u;
    }
    return scratch_write(document, buffer, count);
}

/*
 * A \u escape names a UTF-16 code unit, so a code point above the BMP arrives
 * as a surrogate pair.  A half pair is not a code point and has no UTF-8
 * encoding, so it is rejected here for the same reason the raw bytes D800..
 * DFFF are rejected in utf8_ok: it is text that two readers would disagree
 * about.  `index` is the offset of the first hex digit and is advanced past
 * whatever was consumed.
 */
static kpa_json_result scan_escape_unicode(kpa_parser *parser, size_t *index)
{
    uint32_t code;
    uint32_t low;

    if (!read_hex4(parser, *index, &code)) return KPA_JSON_SYNTAX;
    *index += 4u;
    if (code >= 0xdc00u && code <= 0xdfffu) return KPA_JSON_UTF8;
    if (code >= 0xd800u && code <= 0xdbffu) {
        if (parser->length - *index < 2u ||
            parser->text[*index] != '\\' || parser->text[*index + 1u] != 'u')
            return KPA_JSON_UTF8;
        if (!read_hex4(parser, *index + 2u, &low)) return KPA_JSON_SYNTAX;
        if (low < 0xdc00u || low > 0xdfffu) return KPA_JSON_UTF8;
        *index += 6u;
        code = 0x10000u + ((code - 0xd800u) << 10) + (low - 0xdc00u);
    }
    return encode_utf8(parser->document, code);
}

/*
 * On return the string is either borrowed from the caller's input (no escape
 * was present, which is the common case and costs no arena at all) or decoded
 * into the scratch tail.  `parser->position` sits on the opening quote.
 */
static kpa_json_result scan_string(kpa_parser *parser, const char **out_text,
                                   size_t *out_length)
{
    const size_t begin = parser->position + 1u;
    size_t index = begin;
    size_t scratch_begin;
    kpa_json_result result;

    while (index < parser->length) {
        const unsigned char byte = parser->text[index];
        if (byte == '"') {
            *out_text = (const char *)parser->text + begin;
            *out_length = index - begin;
            parser->position = index + 1u;
            return KPA_JSON_OK;
        }
        if (byte == '\\') break;
        if (byte < 0x20u) return KPA_JSON_SYNTAX;
        index++;
    }
    if (index >= parser->length) return KPA_JSON_SYNTAX;

    scratch_begin = parser->document->scratch_used;
    result = scratch_write(parser->document, parser->text + begin,
                           index - begin);
    if (result != KPA_JSON_OK) return result;
    while (index < parser->length) {
        unsigned char byte = parser->text[index];

        if (byte == '"') {
            *out_text = parser->document->scratch + scratch_begin;
            *out_length = parser->document->scratch_used - scratch_begin;
            parser->position = index + 1u;
            return KPA_JSON_OK;
        }
        if (byte < 0x20u) return KPA_JSON_SYNTAX;
        if (byte != '\\') {
            result = scratch_push(parser->document, byte);
            if (result != KPA_JSON_OK) return result;
            index++;
            continue;
        }
        index++;
        if (index >= parser->length) return KPA_JSON_SYNTAX;
        byte = parser->text[index];
        index++;
        switch (byte) {
        case '"':  result = scratch_push(parser->document, '"'); break;
        case '\\': result = scratch_push(parser->document, '\\'); break;
        case '/':  result = scratch_push(parser->document, '/'); break;
        case 'b':  result = scratch_push(parser->document, 0x08u); break;
        case 'f':  result = scratch_push(parser->document, 0x0cu); break;
        case 'n':  result = scratch_push(parser->document, 0x0au); break;
        case 'r':  result = scratch_push(parser->document, 0x0du); break;
        case 't':  result = scratch_push(parser->document, 0x09u); break;
        case 'u':  result = scan_escape_unicode(parser, &index); break;
        default:   return KPA_JSON_SYNTAX;
        }
        if (result != KPA_JSON_OK) return result;
    }
    return KPA_JSON_SYNTAX;
}

static void skip_whitespace(kpa_parser *parser)
{
    while (parser->position < parser->length) {
        const unsigned char byte = parser->text[parser->position];
        if (byte != 0x20u && byte != 0x09u && byte != 0x0au && byte != 0x0du)
            break;
        parser->position++;
    }
}

static bool match_literal(kpa_parser *parser, const char *literal,
                          size_t length)
{
    if (parser->length - parser->position < length) return false;
    if (memcmp(parser->text + parser->position, literal, length) != 0)
        return false;
    parser->position += length;
    return true;
}

static kpa_json_result scan_scalar(kpa_parser *parser, kpa_json_node *node)
{
    const unsigned char byte = parser->text[parser->position];
    kpa_json_result result;

    switch (byte) {
    case '"':
        result = scan_string(parser, &node->text, &node->length);
        if (result != KPA_JSON_OK) return result;
        node->type = KPA_JSON_STRING;
        return KPA_JSON_OK;
    case 't':
        if (!match_literal(parser, "true", 4u)) return KPA_JSON_SYNTAX;
        node->type = KPA_JSON_BOOL;
        node->boolean = true;
        return KPA_JSON_OK;
    case 'f':
        if (!match_literal(parser, "false", 5u)) return KPA_JSON_SYNTAX;
        node->type = KPA_JSON_BOOL;
        node->boolean = false;
        return KPA_JSON_OK;
    case 'n':
        if (!match_literal(parser, "null", 4u)) return KPA_JSON_SYNTAX;
        node->type = KPA_JSON_NULL;
        return KPA_JSON_OK;
    default:
        break;
    }
    if (byte != '-' && !is_digit(byte)) return KPA_JSON_SYNTAX;
    result = scan_number(parser, &node->number);
    if (result != KPA_JSON_OK) return result;
    node->type = KPA_JSON_NUMBER;
    return KPA_JSON_OK;
}

/* ------------------------------------------------------ container stack */

typedef struct kpa_frame {
    kpa_json_ref container;
    kpa_json_ref last_child;
    /* One bit per key hash. A key whose bit is clear cannot be a duplicate,
     * which keeps the common object off the O(n^2) scan below entirely. */
    uint64_t key_filter;
} kpa_frame;

static uint64_t key_filter_bit(const char *key, size_t length)
{
    uint64_t hash = UINT64_C(1469598103934665603);
    size_t index;

    for (index = 0u; index < length; index++) {
        hash ^= (unsigned char)key[index];
        hash *= UINT64_C(1099511628211);
    }
    return UINT64_C(1) << (hash & 63u);
}

static bool object_has_key(const kpa_json_document *document,
                           kpa_json_ref container, const char *key,
                           size_t length)
{
    kpa_json_ref child = document->nodes[container].first_child;

    while (child != 0u) {
        const kpa_json_node *member = &document->nodes[child];
        if (member->key_length == length &&
            (length == 0u || memcmp(member->key, key, length) == 0))
            return true;
        child = member->next_sibling;
    }
    return false;
}

static void attach_child(kpa_json_document *document, kpa_frame *frame,
                         kpa_json_ref child)
{
    if (frame->last_child == 0u)
        document->nodes[frame->container].first_child = child;
    else
        document->nodes[frame->last_child].next_sibling = child;
    frame->last_child = child;
    document->nodes[frame->container].child_count++;
}

typedef enum kpa_parse_state {
    KPA_STATE_VALUE,
    KPA_STATE_KEY,
    KPA_STATE_SEPARATOR
} kpa_parse_state;

/*
 * One value, iteratively.  `stack` holds the open containers, so the machine
 * stack sees a single frame no matter how deep the document goes and the only
 * bound that matters is KPA_JSON_MAX_DEPTH, checked before each push.
 */
static kpa_json_result parse_value(kpa_parser *parser)
{
    kpa_frame stack[KPA_JSON_MAX_DEPTH];
    kpa_json_document *document = parser->document;
    kpa_parse_state state = KPA_STATE_VALUE;
    uint32_t depth = 0u;
    const char *pending_key = NULL;
    size_t pending_key_length = 0u;
    kpa_json_result result;

    for (;;) {
        skip_whitespace(parser);
        switch (state) {
        case KPA_STATE_KEY: {
            kpa_frame *frame;
            uint64_t bit;

            /* Only an open object leads here, so depth is at least one.
             * Checking it keeps that an invariant of the loop rather than an
             * assumption a later edit could break into an underflow. */
            if (depth == 0u) return KPA_JSON_SYNTAX;
            frame = &stack[depth - 1u];
            if (parser->position >= parser->length ||
                parser->text[parser->position] != '"')
                return KPA_JSON_SYNTAX;
            result = scan_string(parser, &pending_key, &pending_key_length);
            if (result != KPA_JSON_OK) return result;
            skip_whitespace(parser);
            if (parser->position >= parser->length ||
                parser->text[parser->position] != ':')
                return KPA_JSON_SYNTAX;
            parser->position++;
            /* Structure first, then the semantic check, so a member that is
             * malformed reads as malformed rather than as a duplicate. */
            bit = key_filter_bit(pending_key, pending_key_length);
            if ((frame->key_filter & bit) != 0u &&
                object_has_key(document, frame->container, pending_key,
                               pending_key_length))
                return KPA_JSON_DUPLICATE_KEY;
            frame->key_filter |= bit;
            state = KPA_STATE_VALUE;
            break;
        }
        case KPA_STATE_VALUE: {
            unsigned char byte;
            kpa_json_ref reference;
            kpa_json_node *node;

            if (parser->position >= parser->length) return KPA_JSON_SYNTAX;
            byte = parser->text[parser->position];
            if ((byte == '{' || byte == '[') && depth == KPA_JSON_MAX_DEPTH)
                return KPA_JSON_DEPTH;
            result = node_alloc(document, &reference);
            if (result != KPA_JSON_OK) return result;
            node = &document->nodes[reference];
            node->key = pending_key;
            node->key_length = pending_key_length;
            pending_key = NULL;
            pending_key_length = 0u;
            if (depth > 0u) attach_child(document, &stack[depth - 1u],
                                         reference);
            if (byte == '{' || byte == '[') {
                node->type = (byte == '{') ? KPA_JSON_OBJECT : KPA_JSON_ARRAY;
                parser->position++;
                stack[depth].container = reference;
                stack[depth].last_child = 0u;
                stack[depth].key_filter = 0u;
                depth++;
                skip_whitespace(parser);
                if (parser->position < parser->length &&
                    parser->text[parser->position] ==
                        ((byte == '{') ? '}' : ']')) {
                    parser->position++;
                    depth--;
                    state = KPA_STATE_SEPARATOR;
                } else {
                    state = (byte == '{') ? KPA_STATE_KEY : KPA_STATE_VALUE;
                }
                break;
            }
            result = scan_scalar(parser, node);
            if (result != KPA_JSON_OK) return result;
            state = KPA_STATE_SEPARATOR;
            break;
        }
        case KPA_STATE_SEPARATOR: {
            bool object;
            unsigned char byte;

            if (depth == 0u) return KPA_JSON_OK;
            object = document->nodes[stack[depth - 1u].container].type ==
                     KPA_JSON_OBJECT;
            if (parser->position >= parser->length) return KPA_JSON_SYNTAX;
            byte = parser->text[parser->position];
            if (byte == ',') {
                parser->position++;
                state = object ? KPA_STATE_KEY : KPA_STATE_VALUE;
            } else if (byte == (object ? '}' : ']')) {
                parser->position++;
                depth--;
            } else {
                return KPA_JSON_SYNTAX;
            }
            break;
        }
        default:
            return KPA_JSON_SYNTAX;
        }
    }
}

/* ----------------------------------------------------------------- api */

void kpa_json_document_init(kpa_json_document *document,
                            kpa_json_node *nodes, uint32_t node_capacity,
                            char *scratch, size_t scratch_capacity)
{
    if (document == NULL) return;
    document->nodes = nodes;
    document->node_count = 0u;
    document->node_capacity = (nodes == NULL) ? 0u : node_capacity;
    document->scratch = scratch;
    document->scratch_used = 0u;
    document->scratch_capacity = (scratch == NULL) ? 0u : scratch_capacity;
}

kpa_json_result kpa_json_parse(kpa_json_document *document, const char *text,
                               size_t length)
{
    kpa_parser parser;
    kpa_json_result result;

    if (document == NULL || document->nodes == NULL ||
        document->node_capacity == 0u ||
        (document->scratch == NULL && document->scratch_capacity != 0u))
        return KPA_JSON_INVALID_ARGUMENT;
    document->node_count = 0u;
    document->scratch_used = 0u;
    if (text == NULL) return KPA_JSON_INVALID_ARGUMENT;
    if (!utf8_ok((const unsigned char *)text, length)) return KPA_JSON_UTF8;

    parser.text = (const unsigned char *)text;
    parser.length = length;
    parser.position = 0u;
    parser.document = document;
    result = parse_value(&parser);
    if (result == KPA_JSON_OK) {
        skip_whitespace(&parser);
        if (parser.position != parser.length) result = KPA_JSON_SYNTAX;
    }
    /* A half-built tree is worse than no tree: a caller that ignores the
     * result must not be able to read one node of a rejected document. */
    if (result != KPA_JSON_OK) {
        document->node_count = 0u;
        document->scratch_used = 0u;
    }
    return result;
}

const char *kpa_json_result_name(kpa_json_result result)
{
    switch (result) {
    case KPA_JSON_OK:               return "ok";
    case KPA_JSON_INVALID_ARGUMENT: return "invalid argument";
    case KPA_JSON_SYNTAX:           return "syntax";
    case KPA_JSON_DEPTH:            return "too deep";
    case KPA_JSON_NO_SPACE:         return "no space";
    case KPA_JSON_UTF8:             return "invalid utf-8";
    case KPA_JSON_DUPLICATE_KEY:    return "duplicate key";
    case KPA_JSON_RANGE:            return "out of range";
    default:                        break;
    }
    return "unknown";
}

const kpa_json_node *kpa_json_root(const kpa_json_document *document)
{
    if (document == NULL || document->nodes == NULL ||
        document->node_count == 0u)
        return NULL;
    return &document->nodes[0];
}

const kpa_json_node *kpa_json_at(const kpa_json_document *document,
                                 kpa_json_ref reference)
{
    if (document == NULL || document->nodes == NULL ||
        reference >= document->node_count)
        return NULL;
    return &document->nodes[reference];
}

const kpa_json_node *kpa_json_member(const kpa_json_document *document,
                                     const kpa_json_node *node,
                                     const char *key)
{
    size_t length;
    kpa_json_ref child;

    if (document == NULL || document->nodes == NULL || node == NULL ||
        key == NULL || node->type != KPA_JSON_OBJECT)
        return NULL;
    length = strlen(key);
    child = node->first_child;
    while (child != 0u && child < document->node_count) {
        const kpa_json_node *member = &document->nodes[child];
        if (member->key_length == length && member->key != NULL &&
            (length == 0u || memcmp(member->key, key, length) == 0))
            return member;
        child = member->next_sibling;
    }
    return NULL;
}

const kpa_json_node *kpa_json_element(const kpa_json_document *document,
                                      const kpa_json_node *node,
                                      uint32_t index)
{
    kpa_json_ref child;

    if (document == NULL || document->nodes == NULL || node == NULL ||
        node->type != KPA_JSON_ARRAY || index >= node->child_count)
        return NULL;
    child = node->first_child;
    while (child != 0u && child < document->node_count) {
        if (index == 0u) return &document->nodes[child];
        index--;
        child = document->nodes[child].next_sibling;
    }
    return NULL;
}

bool kpa_json_string(const kpa_json_document *document,
                     const kpa_json_node *node, const char *key,
                     const char **out, size_t *out_length)
{
    const kpa_json_node *member = kpa_json_member(document, node, key);

    if (member == NULL || member->type != KPA_JSON_STRING || out == NULL ||
        out_length == NULL)
        return false;
    *out = member->text;
    *out_length = member->length;
    return true;
}

bool kpa_json_string_copy(const kpa_json_document *document,
                          const kpa_json_node *node, const char *key,
                          char *out, size_t out_size)
{
    const char *text;
    size_t length;

    if (out == NULL || out_size == 0u) return false;
    if (!kpa_json_string(document, node, key, &text, &length)) return false;
    if (text == NULL || length >= out_size) return false;
    /* An embedded NUL cannot survive as a C string, and a silently shortened
     * path or filename is exactly the bug this refuses to hand the caller. */
    if (length > 0u && memchr(text, 0, length) != NULL) return false;
    if (length > 0u) memcpy(out, text, length);
    out[length] = '\0';
    return true;
}

bool kpa_json_number(const kpa_json_document *document,
                     const kpa_json_node *node, const char *key, double *out)
{
    const kpa_json_node *member = kpa_json_member(document, node, key);

    if (member == NULL || member->type != KPA_JSON_NUMBER || out == NULL)
        return false;
    *out = member->number;
    return true;
}

bool kpa_json_bool(const kpa_json_document *document,
                   const kpa_json_node *node, const char *key, bool *out)
{
    const kpa_json_node *member = kpa_json_member(document, node, key);

    if (member == NULL || member->type != KPA_JSON_BOOL || out == NULL)
        return false;
    *out = member->boolean;
    return true;
}

bool kpa_json_string_equals(const kpa_json_node *node, const char *value)
{
    size_t length;

    if (node == NULL || value == NULL || node->type != KPA_JSON_STRING ||
        node->text == NULL)
        return false;
    length = strlen(value);
    return node->length == length &&
           (length == 0u || memcmp(node->text, value, length) == 0);
}
