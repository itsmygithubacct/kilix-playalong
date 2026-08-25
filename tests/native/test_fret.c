/*
 * Conformance suite for the fretboard model.
 *
 * Every number docs/FRETBOARD.md defines is read out of
 * tests/fixtures/fretboard_vectors.json and compared against what kpa_fret.c
 * computes.  Not one expected value is written into this file: a test that
 * restates the implementation's arithmetic proves only that the arithmetic is
 * consistent with itself, and the thing actually at risk here is two surfaces
 * that share no code drifting apart.  The fixture is the third party they are
 * both measured against, and the browser suite reads the same file.
 *
 * The fixture is required, not optional.  A conformance suite that quietly
 * skips when it cannot find its vectors is worth less than no suite at all,
 * so a missing or unparseable file fails the run and says which file.
 * KPA_FRET_VECTORS overrides the path for a runner with a different working
 * directory; the default is relative to the repository root, which is where
 * `make -f Makefile.native native-test` runs the binaries from.
 *
 * Beyond the vectors, the cases the fixture cannot carry are here: an empty
 * tab, one note, six at once, a fret past the end of the neck, a string the
 * instrument does not have, and events stored out of time order.  The
 * out-of-order test is written as a property rather than a table - the same
 * events shuffled must give the same answer - because the answer it is
 * checking is exactly "storage order changed nothing".
 *
 * No song, lyric, stem or media appears here.  The fixture holds pitch and
 * fret numbers read from one transcription and nothing that could reproduce
 * it; every tab in this file is synthesised at run time.
 */
#include "kilix_playalong/kpa_fret.h"

#include "kilix_playalong/kpa_json.h"
#include "kilix_playalong/kpa_project.h"

#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CHECK(condition)                                                   \
    do {                                                                   \
        if (!(condition)) {                                                \
            (void)fprintf(stderr, "%s:%d: check failed: %s\n",             \
                          __FILE__, __LINE__, #condition);                 \
            return false;                                                  \
        }                                                                  \
    } while (false)

#define CHECK_CASE(condition, index)                                       \
    do {                                                                   \
        if (!(condition)) {                                                \
            (void)fprintf(stderr, "%s:%d: case %u: %s\n", __FILE__,        \
                          __LINE__, (unsigned)(index), #condition);        \
            return false;                                                  \
        }                                                                  \
    } while (false)

/* ------------------------------------------------------- fixture loading */

/*
 * 2270 values in the fixture as written and no escape sequence in it, so the
 * arena is a little over three times what the document needs and the scratch
 * tail, which only escape decoding uses, is never touched.  Both buffers are
 * heap: at 72 bytes a node, 8192 of them are 576 KiB, which is not a thing to
 * put on a stack.
 */
#define VECTOR_NODE_CAPACITY 8192u
#define VECTOR_SCRATCH_CAPACITY 4096u
#define VECTOR_MAX_BYTES (4u * 1024u * 1024u)

static kpa_json_document g_document;
static char *g_text;
static kpa_json_node *g_nodes;
static char *g_scratch;
static const kpa_json_node *g_root;
static double g_tolerance;

static const char *vectors_path(void)
{
    const char *override = getenv("KPA_FRET_VECTORS");

    if (override != NULL && override[0] != '\0') return override;
    return "tests/fixtures/fretboard_vectors.json";
}

static bool vectors_load(void)
{
    const char *path = vectors_path();
    FILE *stream = fopen(path, "rb");
    long size;
    size_t length;
    kpa_json_result result;
    const kpa_json_node *geometry;

    if (stream == NULL) {
        (void)fprintf(stderr, "test_fret: cannot open %s\n", path);
        return false;
    }
    if (fseek(stream, 0, SEEK_END) != 0) {
        (void)fclose(stream);
        return false;
    }
    size = ftell(stream);
    if (size <= 0 || (unsigned long)size > VECTOR_MAX_BYTES) {
        (void)fprintf(stderr, "test_fret: %s has an implausible size %ld\n",
                      path, size);
        (void)fclose(stream);
        return false;
    }
    rewind(stream);
    length = (size_t)size;
    g_text = malloc(length);
    g_nodes = malloc(VECTOR_NODE_CAPACITY * sizeof *g_nodes);
    g_scratch = malloc(VECTOR_SCRATCH_CAPACITY);
    if (g_text == NULL || g_nodes == NULL || g_scratch == NULL) {
        (void)fclose(stream);
        return false;
    }
    if (fread(g_text, 1u, length, stream) != length) {
        (void)fprintf(stderr, "test_fret: short read on %s\n", path);
        (void)fclose(stream);
        return false;
    }
    (void)fclose(stream);

    kpa_json_document_init(&g_document, g_nodes, VECTOR_NODE_CAPACITY,
                           g_scratch, VECTOR_SCRATCH_CAPACITY);
    result = kpa_json_parse(&g_document, g_text, length);
    if (result != KPA_JSON_OK) {
        (void)fprintf(stderr, "test_fret: %s: %s\n", path,
                      kpa_json_result_name(result));
        return false;
    }
    g_root = kpa_json_root(&g_document);
    if (g_root == NULL || g_root->type != KPA_JSON_OBJECT) return false;

    geometry = kpa_json_member(&g_document, g_root, "geometry");
    if (!kpa_json_number(&g_document, geometry, "tolerance", &g_tolerance)) {
        (void)fprintf(stderr, "test_fret: no geometry.tolerance\n");
        return false;
    }
    (void)printf("test_fret: %s, %u nodes, tolerance %g\n", path,
                 (unsigned)g_document.node_count, g_tolerance);
    return true;
}

static void vectors_free(void)
{
    free(g_text);
    free(g_nodes);
    free(g_scratch);
    g_text = NULL;
    g_nodes = NULL;
    g_scratch = NULL;
    g_root = NULL;
}

/* ----------------------------------------------------- fixture accessors */

static const kpa_json_node *member(const kpa_json_node *node, const char *key)
{
    return kpa_json_member(&g_document, node, key);
}

static const kpa_json_node *element(const kpa_json_node *node, uint32_t index)
{
    return kpa_json_element(&g_document, node, index);
}

static const kpa_json_node *section(const char *key, const char *sub)
{
    const kpa_json_node *node = member(g_root, key);

    if (sub == NULL) return node;
    return member(node, sub);
}

static bool node_double(const kpa_json_node *node, double *out)
{
    if (node == NULL || node->type != KPA_JSON_NUMBER) return false;
    *out = node->number;
    return true;
}

static bool node_int(const kpa_json_node *node, int32_t *out)
{
    double value;

    if (!node_double(node, &value)) return false;
    if (value != floor(value) || value < -2147483648.0 ||
        value > 2147483647.0) {
        return false;
    }
    *out = (int32_t)value;
    return true;
}

static bool member_int(const kpa_json_node *node, const char *key,
                       int32_t *out)
{
    return node_int(member(node, key), out);
}

static bool member_double(const kpa_json_node *node, const char *key,
                          double *out)
{
    return node_double(member(node, key), out);
}

/* An array of arrays whose elements are [index, value] pairs. */
static bool pair_of(const kpa_json_node *array, uint32_t index, int32_t *key,
                    double *value)
{
    const kpa_json_node *pair = element(array, index);

    if (pair == NULL || pair->type != KPA_JSON_ARRAY ||
        pair->child_count != 2u) {
        return false;
    }
    return node_int(element(pair, 0u), key) &&
           node_double(element(pair, 1u), value);
}

/*
 * Every numeric comparison in this file goes through here, so a failure says
 * what the fixture holds, what the module produced and by how much they
 * differ.  "A vector is wrong" and "the code is wrong" look identical from a
 * bare assertion and quite different from these three numbers.
 */
static bool near_within(double got, double want, double tolerance,
                        const char *what, int32_t index)
{
    if (fabs(got - want) <= tolerance) return true;
    (void)fprintf(stderr,
                  "%s[%d]: fixture %.12g, module %.12g, off by %.3g "
                  "(tolerance %.3g)\n",
                  what, index, want, got, fabs(got - want), tolerance);
    return false;
}

static bool near(double got, double want, const char *what, int32_t index)
{
    return near_within(got, want, g_tolerance, what, index);
}

/* ------------------------------------------------------------- geometry */

static bool test_fret_positions(void)
{
    const kpa_json_node *array = section("geometry", "fret_positions");
    uint32_t index;

    CHECK(array != NULL && array->type == KPA_JSON_ARRAY);
    CHECK(array->child_count > 0u);
    for (index = 0u; index < array->child_count; ++index) {
        int32_t fret;
        double want;

        CHECK_CASE(pair_of(array, index, &fret, &want), index);
        CHECK_CASE(near(kpa_fret_position(fret), want, "d", fret), index);
    }
    (void)printf("     %u fret positions\n", (unsigned)array->child_count);
    return true;
}

static bool test_cell_centres(void)
{
    const kpa_json_node *array = section("geometry", "cell_centres");
    uint32_t index;

    CHECK(array != NULL && array->child_count > 0u);
    for (index = 0u; index < array->child_count; ++index) {
        int32_t fret;
        double want;

        CHECK_CASE(pair_of(array, index, &fret, &want), index);
        CHECK_CASE(near(kpa_fret_cell_centre(fret), want, "cell", fret),
                   index);
        /* The centre of a fret's space is between its two wires and never on
         * one of them: the detail most drawn fretboards get wrong. */
        CHECK_CASE(want > kpa_fret_position(fret - 1), index);
        CHECK_CASE(want < kpa_fret_position(fret), index);
    }
    return true;
}

static bool test_display_normalized(void)
{
    const kpa_json_node *array = section("geometry", "display_normalized");
    uint32_t block;

    CHECK(array != NULL && array->child_count > 0u);
    for (block = 0u; block < array->child_count; ++block) {
        const kpa_json_node *entry = element(array, block);
        const kpa_json_node *values;
        int32_t highest;
        uint32_t index;

        CHECK_CASE(entry != NULL, block);
        CHECK_CASE(member_int(entry, "highest_displayed_fret", &highest),
                   block);
        values = member(entry, "u");
        CHECK_CASE(values != NULL && values->type == KPA_JSON_ARRAY, block);
        for (index = 0u; index < values->child_count; ++index) {
            int32_t fret;
            double want;

            CHECK_CASE(pair_of(values, index, &fret, &want), block);
            CHECK_CASE(near(kpa_fret_display_position(fret, highest), want,
                            "u", fret), block);
        }
        /* The last fret drawn lands exactly at the right edge; that is the
         * whole reason the normalisation exists. */
        CHECK_CASE(near(kpa_fret_display_position(highest, highest), 1.0,
                        "u(N,N)", highest), block);
    }
    return true;
}

static bool test_identities(void)
{
    const kpa_json_node *array = section("geometry", "identities");
    uint32_t index;

    CHECK(array != NULL && array->child_count > 0u);
    for (index = 0u; index < array->child_count; ++index) {
        const kpa_json_node *entry = element(array, index);
        const kpa_json_node *expr = member(entry, "expr");
        double want;
        double tolerance;
        double got;

        CHECK_CASE(entry != NULL && expr != NULL, index);
        CHECK_CASE(member_double(entry, "expect", &want), index);
        CHECK_CASE(member_double(entry, "tolerance", &tolerance), index);
        if (kpa_json_string_equals(expr, "d(12)")) {
            got = kpa_fret_position(12);
        } else if (kpa_json_string_equals(expr, "d(24)")) {
            got = kpa_fret_position(24);
        } else if (kpa_json_string_equals(expr, "1/d(1)")) {
            got = 1.0 / kpa_fret_position(1);
        } else {
            (void)fprintf(stderr, "identity %u: unknown expression %.*s\n",
                          (unsigned)index, (int)expr->length, expr->text);
            return false;
        }
        CHECK_CASE(near_within(got, want, tolerance, "identity",
                               (int32_t)index), index);
    }
    return true;
}

static bool test_geometry_refuses_impossible_input(void)
{
    /* Every real position is in [0, 1), so a negative return cannot be
     * mistaken for one. */
    CHECK(kpa_fret_position(-1) < 0.0);
    CHECK(kpa_fret_position(0) == 0.0);
    /* Fret 0 has no space behind it: the nut is not a fret. */
    CHECK(kpa_fret_cell_centre(0) < 0.0);
    CHECK(kpa_fret_cell_centre(-3) < 0.0);
    CHECK(kpa_fret_cell_centre(1) > 0.0);
    CHECK(kpa_fret_display_position(1, 0) < 0.0);
    CHECK(kpa_fret_display_position(-1, 12) < 0.0);
    return true;
}

/* --------------------------------------------------------------- inlays */

static bool test_inlays(void)
{
    const kpa_json_node *inlays = section("inlays", NULL);
    const kpa_json_node *cases = member(inlays, "cases");
    const kpa_json_node *single = member(inlays, "single");
    const kpa_json_node *doubles = member(inlays, "double");
    bool marked[KPA_FRET_MAX_FRET + 1];
    uint32_t index;
    int32_t fret;

    CHECK(cases != NULL && cases->child_count > 0u);
    CHECK(single != NULL && doubles != NULL);
    memset(marked, 0, sizeof marked);

    for (index = 0u; index < cases->child_count; ++index) {
        const kpa_json_node *entry = element(cases, index);
        const kpa_json_node *kind = member(entry, "kind");
        int32_t want_count;
        double centre;
        kpa_fret_inlay got;

        CHECK_CASE(entry != NULL && kind != NULL, index);
        CHECK_CASE(member_int(entry, "fret", &fret), index);
        CHECK_CASE(member_int(entry, "count", &want_count), index);
        CHECK_CASE(member_double(entry, "centre", &centre), index);
        got = kpa_fret_inlay_at(fret);
        if (kpa_json_string_equals(kind, "double")) {
            CHECK_CASE(got == KPA_FRET_INLAY_DOUBLE, index);
        } else if (kpa_json_string_equals(kind, "single")) {
            CHECK_CASE(got == KPA_FRET_INLAY_SINGLE, index);
        } else {
            CHECK_CASE(false, index);
        }
        /* The enumerator's value is the number of dots. */
        CHECK_CASE((int32_t)got == want_count, index);
        /* An inlay sits where a finger sits, not on the wire it is named
         * for. */
        CHECK_CASE(near(kpa_fret_cell_centre(fret), centre, "inlay", fret),
                   index);
        CHECK_CASE(fret >= 0 && fret <= KPA_FRET_MAX_FRET, index);
        marked[fret] = true;
    }

    /* Everything the fixture does not list is bare neck.  Without this the
     * suite would pass an implementation that marked every fret. */
    for (fret = 0; fret <= KPA_FRET_MAX_FRET; ++fret) {
        if (marked[fret]) continue;
        CHECK_CASE(kpa_fret_inlay_at(fret) == KPA_FRET_INLAY_NONE, fret);
    }
    for (index = 0u; index < single->child_count; ++index) {
        CHECK_CASE(node_int(element(single, index), &fret), index);
        CHECK_CASE(kpa_fret_inlay_at(fret) == KPA_FRET_INLAY_SINGLE, index);
    }
    for (index = 0u; index < doubles->child_count; ++index) {
        CHECK_CASE(node_int(element(doubles, index), &fret), index);
        CHECK_CASE(kpa_fret_inlay_at(fret) == KPA_FRET_INLAY_DOUBLE, index);
    }
    return true;
}

/* -------------------------------------------------------------- strings */

static bool test_strings(void)
{
    const kpa_json_node *cases = section("strings", "cases");
    const kpa_json_node *song = section("reference_song", NULL);
    const kpa_json_node *tuning = member(song, "tuning_midi");
    const kpa_json_node *labels = member(song, "tuning_labels");
    uint32_t index;
    double previous = 0.0;

    CHECK(cases != NULL && cases->child_count == KPA_STRING_COUNT);
    CHECK(tuning != NULL && labels != NULL);
    CHECK(tuning->child_count == KPA_STRING_COUNT);

    for (index = 0u; index < cases->child_count; ++index) {
        const kpa_json_node *entry = element(cases, index);
        const kpa_json_node *label = member(entry, "label");
        const kpa_json_node *song_label = element(labels, index);
        int32_t api_index;
        int32_t open_midi;
        int32_t player_number;
        int32_t song_midi;
        int32_t row;
        double ratio;
        int32_t base;

        CHECK_CASE(entry != NULL && label != NULL, index);
        CHECK_CASE(member_int(entry, "api_index", &api_index), index);
        CHECK_CASE(member_int(entry, "open_midi", &open_midi), index);
        CHECK_CASE(member_int(entry, "player_number", &player_number), index);
        CHECK_CASE(member_double(entry, "width_ratio", &ratio), index);
        /* The cases are in api order: 0 is the low E. */
        CHECK_CASE(api_index == (int32_t)index, index);

        /* The string table and the song's tuning must be one order, not two.
         * Two pinned orders that disagree is the exact bug this contract was
         * written after. */
        CHECK_CASE(node_int(element(tuning, index), &song_midi), index);
        CHECK_CASE(song_midi == open_midi, index);
        CHECK_CASE(song_label != NULL && song_label->type == KPA_JSON_STRING,
                   index);
        CHECK_CASE(label->length == song_label->length &&
                   memcmp(label->text, song_label->text, label->length) == 0,
                   index);

        /* There is no player-number function here on purpose: kpa_ui.c owns
         * the one this surface uses.  What the fretboard must guarantee is
         * that its top-down row order IS that numbering, which is this. */
        row = kpa_fret_row(api_index, (int32_t)KPA_STRING_COUNT,
                           KPA_FRET_HIGH_E_TOP);
        CHECK_CASE(row >= 0 && row + 1 == player_number, index);

        CHECK_CASE(near(kpa_fret_string_width_ratio(api_index), ratio,
                        "width_ratio", api_index), index);
        /* Thickest at the low E, thinnest at the high e, strictly. */
        if (index > 0u) CHECK_CASE(ratio < previous, index);
        previous = ratio;

        for (base = 1; base <= 8; ++base) {
            double scaled = round((double)base * ratio);
            int32_t want = scaled < 1.0 ? 1 : (int32_t)scaled;

            CHECK_CASE(kpa_fret_string_width(api_index, base) == want, index);
            CHECK_CASE(kpa_fret_string_width(api_index, base) >= 1, index);
        }
    }
    /* Six gauges are pinned; a seventh string would need the contract to say
     * what it weighs, so this refuses rather than guessing. */
    CHECK(kpa_fret_string_width_ratio(-1) < 0.0);
    CHECK(kpa_fret_string_width_ratio((int32_t)KPA_STRING_COUNT) < 0.0);
    CHECK(kpa_fret_string_width((int32_t)KPA_STRING_COUNT, 4) == 0);
    CHECK(kpa_fret_string_width(0, 0) == 0);
    return true;
}

/* -------------------------------------------------- orientation and point */

static bool orientation_of(const kpa_json_node *node,
                           kpa_fret_orientation *out)
{
    if (node == NULL || node->type != KPA_JSON_STRING) return false;
    if (kpa_json_string_equals(node, "high-e-top")) {
        *out = KPA_FRET_HIGH_E_TOP;
        return true;
    }
    if (kpa_json_string_equals(node, "low-e-top")) {
        *out = KPA_FRET_LOW_E_TOP;
        return true;
    }
    return false;
}

static bool test_orientation_and_points(void)
{
    const kpa_json_node *block = section("orientation", NULL);
    const kpa_json_node *cases = member(block, "cases");
    const kpa_json_node *values = member(block, "values");
    const kpa_json_node *dflt = member(block, "default");
    kpa_fret_orientation defaulted;
    uint32_t index;
    uint32_t high_e_top_cases = 0u;
    uint32_t low_e_top_cases = 0u;

    CHECK(cases != NULL && cases->child_count > 0u);
    CHECK(values != NULL && values->child_count == 2u);
    /* A zeroed model must hold the default the contract names. */
    CHECK(orientation_of(dflt, &defaulted));
    CHECK(defaulted == KPA_FRET_HIGH_E_TOP);
    CHECK((kpa_fret_orientation)0 == KPA_FRET_HIGH_E_TOP);

    for (index = 0u; index < cases->child_count; ++index) {
        const kpa_json_node *entry = element(cases, index);
        const kpa_json_node *point = member(entry, "point");
        kpa_fret_orientation orientation;
        int32_t api_index;
        int32_t fret;
        int32_t count;
        int32_t highest;
        int32_t want_row;
        double want_x;
        double want_y;
        kpa_fret_point got;
        kpa_fret_rect rect;
        kpa_fret_point mapped;

        CHECK_CASE(entry != NULL && point != NULL, index);
        CHECK_CASE(orientation_of(member(entry, "orientation"), &orientation),
                   index);
        CHECK_CASE(member_int(entry, "api_index", &api_index), index);
        CHECK_CASE(member_int(entry, "fret", &fret), index);
        CHECK_CASE(member_int(entry, "string_count", &count), index);
        CHECK_CASE(member_int(entry, "highest_displayed_fret", &highest),
                   index);
        CHECK_CASE(member_int(entry, "row", &want_row), index);
        CHECK_CASE(member_double(point, "x", &want_x), index);
        CHECK_CASE(member_double(point, "y", &want_y), index);

        if (orientation == KPA_FRET_HIGH_E_TOP) {
            ++high_e_top_cases;
        } else {
            ++low_e_top_cases;
        }
        CHECK_CASE(kpa_fret_row(api_index, count, orientation) == want_row,
                   index);
        CHECK_CASE(kpa_fret_point_of(api_index, fret, count, highest,
                                     orientation, &got), index);
        CHECK_CASE(near(got.x, want_x, "point.x", (int32_t)index), index);
        CHECK_CASE(near(got.y, want_y, "point.y", (int32_t)index), index);

        /* An open string is marked at the nut, not in the first fret's
         * space, and nothing else ever lands there. */
        if (fret == 0) {
            CHECK_CASE(got.x == 0.0, index);
        } else {
            CHECK_CASE(got.x > 0.0, index);
        }

        /* The rectangle form is the call a renderer makes; it must be the
         * normalised point and nothing else.  The tolerance scales with the
         * box because a difference of 1e-9 in a normalised coordinate is
         * 1e-9 * width once it has been multiplied up. */
        rect.x = -17.5;
        rect.y = 40.25;
        rect.width = 320.0;
        rect.height = 96.0;
        CHECK_CASE(kpa_fret_point_in_rect(api_index, fret, count, highest,
                                          orientation, &rect, &mapped),
                   index);
        CHECK_CASE(near_within(mapped.x, rect.x + want_x * rect.width,
                               g_tolerance * (fabs(rect.width) + 1.0),
                               "rect.x", (int32_t)index), index);
        CHECK_CASE(near_within(mapped.y, rect.y + want_y * rect.height,
                               g_tolerance * (fabs(rect.height) + 1.0),
                               "rect.y", (int32_t)index), index);
    }
    /* Both conventions are pinned; a suite that only exercised the default
     * would let the preference rot. */
    CHECK(high_e_top_cases > 0u && low_e_top_cases > 0u);
    (void)printf("     %u orientation cases (%u high-e-top, %u low-e-top)\n",
                 (unsigned)cases->child_count, (unsigned)high_e_top_cases,
                 (unsigned)low_e_top_cases);
    return true;
}

static bool test_orientations_are_mirrors(void)
{
    int32_t count;

    /* Flipping the preference must reverse the rows and nothing else - not
     * shift them, not drop one. */
    for (count = 1; count <= KPA_FRET_MAX_STRINGS; ++count) {
        int32_t api_index;

        for (api_index = 0; api_index < count; ++api_index) {
            int32_t high = kpa_fret_row(api_index, count,
                                        KPA_FRET_HIGH_E_TOP);
            int32_t low = kpa_fret_row(api_index, count, KPA_FRET_LOW_E_TOP);

            CHECK_CASE(high >= 0 && low >= 0, api_index);
            CHECK_CASE(high + low == count - 1, api_index);
        }
    }
    return true;
}

static bool test_point_refuses_impossible_input(void)
{
    kpa_fret_point point;
    kpa_fret_rect rect = {0.0, 0.0, 100.0, 100.0};
    int32_t string_count = (int32_t)KPA_STRING_COUNT;

    /* A string index that is not on the instrument has no row and therefore
     * no point.  Negative is the one an unsigned artifact field can never
     * hold and a signed caller can. */
    CHECK(kpa_fret_row(-1, string_count, KPA_FRET_HIGH_E_TOP) == -1);
    CHECK(kpa_fret_row(string_count, string_count, KPA_FRET_HIGH_E_TOP) == -1);
    CHECK(kpa_fret_row(0, 0, KPA_FRET_HIGH_E_TOP) == -1);
    CHECK(kpa_fret_row(0, KPA_FRET_MAX_STRINGS + 1, KPA_FRET_HIGH_E_TOP) ==
          -1);
    CHECK(!kpa_fret_point_of(-1, 0, string_count, 12, KPA_FRET_HIGH_E_TOP,
                             &point));
    CHECK(!kpa_fret_point_of(string_count, 0, string_count, 12,
                             KPA_FRET_HIGH_E_TOP, &point));
    CHECK(!kpa_fret_point_of(0, -1, string_count, 12, KPA_FRET_HIGH_E_TOP,
                             &point));
    CHECK(!kpa_fret_point_of(0, 0, string_count, 0, KPA_FRET_HIGH_E_TOP,
                             &point));
    CHECK(!kpa_fret_point_of(0, 0, string_count, 12, KPA_FRET_HIGH_E_TOP,
                             NULL));
    CHECK(!kpa_fret_point_in_rect(0, 0, string_count, 12,
                                  KPA_FRET_HIGH_E_TOP, NULL, &point));
    CHECK(!kpa_fret_point_in_rect(-1, 0, string_count, 12,
                                  KPA_FRET_HIGH_E_TOP, &rect, &point));
    rect.width = (double)NAN;
    CHECK(!kpa_fret_point_in_rect(0, 0, string_count, 12,
                                  KPA_FRET_HIGH_E_TOP, &rect, &point));
    return true;
}

static bool test_fret_beyond_the_drawn_neck(void)
{
    const kpa_json_node *song = section("reference_song", NULL);
    int32_t max_fret;
    kpa_fret_point at_end;
    kpa_fret_point beyond;

    CHECK(member_int(song, "max_fret", &max_fret));
    CHECK(max_fret > 0);

    /* A fret past the end of the drawn neck is not refused: it is placed
     * beyond the right edge, where a renderer can see it is off the board and
     * clip it.  Refusing would leave a caller nothing to draw an arrow from. */
    CHECK(kpa_fret_point_of(0, max_fret, (int32_t)KPA_STRING_COUNT, max_fret,
                            KPA_FRET_HIGH_E_TOP, &at_end));
    CHECK(kpa_fret_point_of(0, max_fret + 4, (int32_t)KPA_STRING_COUNT,
                            max_fret, KPA_FRET_HIGH_E_TOP, &beyond));
    CHECK(at_end.x < 1.0);          /* the cell centre is behind the wire */
    CHECK(beyond.x > 1.0);
    CHECK(beyond.y == at_end.y);    /* off the end, not off the string */
    /* d(n) stays defined and monotone past the tabulated neck. */
    CHECK(kpa_fret_position(max_fret + 4) > kpa_fret_position(max_fret));
    CHECK(kpa_fret_position(KPA_FRET_MAX_FRET + 1) < 1.0);
    return true;
}

/* --------------------------------------------------------------- chords */

static bool chord_expect_matches(const kpa_json_node *expect,
                                 const kpa_fret_chord *chord, uint32_t index)
{
    if (expect == NULL) {
        (void)fprintf(stderr, "chord case %u: no expect\n", (unsigned)index);
        return false;
    }
    if (expect->type == KPA_JSON_NULL) {
        /* A null expectation is as binding as a name: inventing one here is
         * the defect the whole refusal branch exists to prevent. */
        if (!chord->named && chord->name[0] == '\0') return true;
        (void)fprintf(stderr,
                      "chord case %u: fixture expects nothing, module said "
                      "\"%s\"\n", (unsigned)index, chord->name);
        return false;
    }
    if (expect->type != KPA_JSON_STRING) {
        (void)fprintf(stderr, "chord case %u: expect is not a name\n",
                      (unsigned)index);
        return false;
    }
    if (chord->named && kpa_json_string_equals(expect, chord->name)) {
        return true;
    }
    (void)fprintf(stderr,
                  "chord case %u: fixture \"%.*s\", module \"%s\"\n",
                  (unsigned)index, (int)expect->length, expect->text,
                  chord->named ? chord->name : "");
    return false;
}

static bool test_chord_spelling(void)
{
    const kpa_json_node *spelling = section("chords", "spelling");
    uint32_t index;

    CHECK(spelling != NULL && spelling->child_count == 12u);
    for (index = 0u; index < spelling->child_count; ++index) {
        const kpa_json_node *name = element(spelling, index);
        const char *got = kpa_fret_pitch_class_name((int32_t)index);

        CHECK_CASE(name != NULL && got != NULL, index);
        CHECK_CASE(kpa_json_string_equals(name, got), index);
    }
    CHECK(kpa_fret_pitch_class_name(-1) == NULL);
    CHECK(kpa_fret_pitch_class_name(12) == NULL);
    return true;
}

static bool test_chords(void)
{
    const kpa_json_node *cases = section("chords", "cases");
    uint32_t index;
    uint32_t named = 0u;
    uint32_t refused = 0u;

    CHECK(cases != NULL && cases->child_count > 0u);
    for (index = 0u; index < cases->child_count; ++index) {
        const kpa_json_node *entry = element(cases, index);
        const kpa_json_node *pitches = member(entry, "pitches");
        int32_t values[KPA_FRET_MAX_PITCHES];
        kpa_fret_chord chord;
        uint32_t which;
        bool got;

        CHECK_CASE(entry != NULL && pitches != NULL, index);
        CHECK_CASE(pitches->child_count <= KPA_FRET_MAX_PITCHES, index);
        for (which = 0u; which < pitches->child_count; ++which) {
            CHECK_CASE(node_int(element(pitches, which), &values[which]),
                       index);
        }
        got = kpa_fret_chord_identify(values, (size_t)pitches->child_count,
                                      &chord);
        CHECK_CASE(got == chord.named, index);
        CHECK_CASE(chord_expect_matches(member(entry, "expect"), &chord,
                                        index), index);
        if (chord.named) {
            /* A name is a root and a quality, and the quality's suffix must
             * be the tail of the name it was spelled from. */
            const char *suffix =
                kpa_fret_chord_quality_suffix(chord.quality);
            const char *root = kpa_fret_pitch_class_name(chord.root_pc);

            CHECK_CASE(suffix != NULL && root != NULL, index);
            CHECK_CASE(strncmp(chord.name, root, strlen(root)) == 0, index);
            CHECK_CASE(strncmp(chord.name + strlen(root), suffix,
                               strlen(suffix)) == 0, index);
            /* A slash is present exactly when the bass is not the root. */
            CHECK_CASE((strchr(chord.name, '/') != NULL) ==
                       (chord.root_pc != chord.bass_pc), index);
            ++named;
        } else {
            CHECK_CASE(chord.root_pc == -1, index);
            ++refused;
        }
    }
    CHECK(named > 0u && refused > 0u);
    (void)printf("     %u chord cases: %u named, %u refused\n",
                 (unsigned)cases->child_count, (unsigned)named,
                 (unsigned)refused);
    return true;
}

static bool test_chord_input_order_is_irrelevant(void)
{
    const kpa_json_node *cases = section("chords", "cases");
    uint32_t index;

    CHECK(cases != NULL);
    for (index = 0u; index < cases->child_count; ++index) {
        const kpa_json_node *entry = element(cases, index);
        const kpa_json_node *pitches = member(entry, "pitches");
        int32_t forward[KPA_FRET_MAX_PITCHES];
        int32_t backward[KPA_FRET_MAX_PITCHES];
        kpa_fret_chord one;
        kpa_fret_chord other;
        uint32_t count;
        uint32_t which;

        CHECK_CASE(pitches != NULL, index);
        count = pitches->child_count;
        for (which = 0u; which < count; ++which) {
            CHECK_CASE(node_int(element(pitches, which), &forward[which]),
                       index);
            backward[count - 1u - which] = forward[which];
        }
        /* The bass is the lowest pitch, not the first element: reversing the
         * input cannot change a name or a slash. */
        (void)kpa_fret_chord_identify(forward, (size_t)count, &one);
        (void)kpa_fret_chord_identify(backward, (size_t)count, &other);
        CHECK_CASE(one.named == other.named, index);
        CHECK_CASE(one.root_pc == other.root_pc, index);
        CHECK_CASE(one.bass_pc == other.bass_pc, index);
        CHECK_CASE(strcmp(one.name, other.name) == 0, index);
    }
    return true;
}

static bool test_chord_refuses_impossible_input(void)
{
    int32_t seven[7] = {40, 45, 50, 55, 59, 64, 67};
    int32_t one[1] = {60};
    int32_t bad[2] = {60, 128};
    int32_t negative[2] = {-1, 60};
    kpa_fret_chord chord;

    CHECK(!kpa_fret_chord_identify(NULL, 0u, &chord));
    CHECK(!chord.named && chord.name[0] == '\0');
    CHECK(!kpa_fret_chord_identify(one, 1u, &chord));
    /* Six strings, six pitches; a seventh is not a guitar chord. */
    CHECK(!kpa_fret_chord_identify(seven, 7u, &chord));
    CHECK(!kpa_fret_chord_identify(bad, 2u, &chord));
    CHECK(!kpa_fret_chord_identify(negative, 2u, &chord));
    CHECK(!kpa_fret_chord_identify(one, 1u, NULL));
    CHECK(kpa_fret_chord_quality_suffix(KPA_FRET_CHORD_NONE) == NULL);
    CHECK(strcmp(kpa_fret_chord_quality_suffix(KPA_FRET_CHORD_POWER), "5") ==
          0);
    CHECK(strcmp(kpa_fret_chord_quality_suffix(KPA_FRET_CHORD_MAJOR), "") ==
          0);
    return true;
}

/* -------------------------------------------------------- synthetic tabs */

#define FAKE_EVENTS 8u
#define FAKE_POSITIONS 48u

typedef struct fake_tab {
    kpa_tab tab;
    kpa_tab_event events[FAKE_EVENTS];
    kpa_tab_position positions[FAKE_POSITIONS];
} fake_tab;

static void fake_tab_init(fake_tab *fake)
{
    memset(fake, 0, sizeof *fake);
    fake->tab.events = fake->events;
    fake->tab.positions = fake->positions;
    fake->tab.string_count = KPA_STRING_COUNT;
    fake->tab.max_fret = 20u;
}

/* Reads the tuning out of the fixture rather than spelling one here: the
 * string order is the contract's, not this file's. */
static bool fake_tab_tune(fake_tab *fake)
{
    const kpa_json_node *tuning = section("reference_song", NULL);
    uint32_t index;

    tuning = member(tuning, "tuning_midi");
    if (tuning == NULL || tuning->child_count != KPA_STRING_COUNT) {
        return false;
    }
    for (index = 0u; index < KPA_STRING_COUNT; ++index) {
        int32_t midi;

        if (!node_int(element(tuning, index), &midi)) return false;
        fake->tab.tuning_midi[index] = midi;
    }
    return true;
}

static void fake_add_event(fake_tab *fake, double start, double end)
{
    kpa_tab_event *event = &fake->events[fake->tab.event_count];

    event->start = start;
    event->end = end;
    event->first_position = fake->tab.position_count;
    event->position_count = 0u;
    ++fake->tab.event_count;
}

static void fake_add_position(fake_tab *fake, int32_t string_index,
                              int32_t fret, int32_t pitch)
{
    kpa_tab_position *position = &fake->positions[fake->tab.position_count];

    position->string_index = (uint8_t)string_index;
    position->fret = (uint8_t)fret;
    position->pitch = (uint8_t)pitch;
    ++fake->tab.position_count;
    ++fake->events[fake->tab.event_count - 1u].position_count;
}

/* Standard-tuning pitch for a position, from the tuning the fixture pins.
 * The artifact carries this value; §4 says it is redundant with the position
 * and must never place a note, so it is only ever built, never read back. */
static int32_t fake_pitch(const fake_tab *fake, int32_t string_index,
                          int32_t fret)
{
    return fake->tab.tuning_midi[string_index] + fret;
}

static bool test_chords_from_events(void)
{
    const kpa_json_node *cases = section("chords", "cases");
    uint32_t index;
    uint32_t checked = 0u;

    CHECK(cases != NULL);
    for (index = 0u; index < cases->child_count; ++index) {
        const kpa_json_node *entry = element(cases, index);
        const kpa_json_node *positions = member(entry, "positions");
        const kpa_json_node *pitches = member(entry, "pitches");
        fake_tab fake;
        kpa_fret_chord chord;
        int32_t derived[KPA_FRET_MAX_PITCHES];
        int32_t listed[KPA_FRET_MAX_PITCHES];
        uint32_t which;
        uint32_t outer;

        if (positions == NULL) continue;   /* synthetic case, pitches only */
        CHECK_CASE(pitches != NULL, index);
        CHECK_CASE(positions->child_count == pitches->child_count, index);
        CHECK_CASE(positions->child_count <= KPA_FRET_MAX_PITCHES, index);

        fake_tab_init(&fake);
        CHECK_CASE(fake_tab_tune(&fake), index);
        fake_add_event(&fake, 1.0, 2.0);
        for (which = 0u; which < positions->child_count; ++which) {
            const kpa_json_node *pair = element(positions, which);
            int32_t string_index;
            int32_t fret;

            CHECK_CASE(pair != NULL && pair->child_count == 2u, index);
            CHECK_CASE(node_int(element(pair, 0u), &string_index), index);
            CHECK_CASE(node_int(element(pair, 1u), &fret), index);
            derived[which] = fake_pitch(&fake, string_index, fret);
            CHECK_CASE(node_int(element(pitches, which), &listed[which]),
                       index);
            fake_add_position(&fake, string_index, fret, derived[which]);
        }

        /* §4's claim about the artifact, checked on every transcription case:
         * pitch is exactly tuning[string] + fret, so the redundant field
         * cannot disagree with the position that draws the note.  The two
         * arrays are the same multiset; the fixture lists pitches sorted and
         * positions in string order, which is not the same order. */
        for (outer = 0u; outer < positions->child_count; ++outer) {
            uint32_t in_derived = 0u;
            uint32_t in_listed = 0u;

            for (which = 0u; which < positions->child_count; ++which) {
                if (derived[which] == listed[outer]) ++in_derived;
                if (listed[which] == listed[outer]) ++in_listed;
            }
            CHECK_CASE(in_derived == in_listed && in_derived > 0u, index);
        }

        CHECK_CASE(kpa_fret_chord_of_event(&fake.tab, 0u, &chord) ==
                   chord.named, index);
        CHECK_CASE(chord_expect_matches(member(entry, "expect"), &chord,
                                        index), index);
        ++checked;
    }
    CHECK(checked > 0u);
    (void)printf("     %u chord cases replayed through a tab event\n",
                 (unsigned)checked);
    return true;
}

/* -------------------------------------------------------- hand position */

static bool test_hand_window_constants(void)
{
    const kpa_json_node *block = section("hand_position", NULL);
    const kpa_json_node *cases = member(block, "cases");
    double back;
    double forward;
    int32_t box;
    uint32_t index;

    CHECK(block != NULL && cases != NULL);
    CHECK(member_double(block, "window_back_s", &back));
    CHECK(member_double(block, "window_forward_s", &forward));
    CHECK(member_int(block, "box_frets", &box));
    /* The module's constants and the contract's numbers are the same numbers,
     * and the window leans forward because the marker has to arrive before
     * the player does. */
    CHECK(back == KPA_FRET_WINDOW_BACK_S);
    CHECK(forward == KPA_FRET_WINDOW_FORWARD_S);
    CHECK(box == KPA_FRET_HAND_FRETS);
    CHECK(forward > back);

    for (index = 0u; index < cases->child_count; ++index) {
        const kpa_json_node *entry = element(cases, index);
        const kpa_json_node *window = member(entry, "window");
        double at;
        double left;
        double right;

        if (window == NULL) continue;      /* synthetic case, frets only */
        CHECK_CASE(member_double(entry, "at", &at), index);
        CHECK_CASE(node_double(element(window, 0u), &left), index);
        CHECK_CASE(node_double(element(window, 1u), &right), index);
        CHECK_CASE(near(at - KPA_FRET_WINDOW_BACK_S, left, "window.left",
                        (int32_t)index), index);
        CHECK_CASE(near(at + KPA_FRET_WINDOW_FORWARD_S, right, "window.right",
                        (int32_t)index), index);
    }
    return true;
}

#define HAND_MAX_FRETS 64u

static bool test_hand_positions(void)
{
    const kpa_json_node *block = section("hand_position", NULL);
    const kpa_json_node *cases = member(block, "cases");
    int32_t max_fret;
    uint32_t index;
    uint32_t placed = 0u;
    uint32_t hidden = 0u;

    CHECK(cases != NULL && cases->child_count > 0u);
    CHECK(member_int(block, "max_fret", &max_fret));

    for (index = 0u; index < cases->child_count; ++index) {
        const kpa_json_node *entry = element(cases, index);
        const kpa_json_node *frets = member(entry, "frets");
        const kpa_json_node *expect = member(entry, "expect");
        int32_t values[HAND_MAX_FRETS];
        kpa_fret_hand hand;
        uint32_t which;
        bool got;

        CHECK_CASE(entry != NULL && frets != NULL && expect != NULL, index);
        CHECK_CASE(frets->child_count <= HAND_MAX_FRETS, index);
        for (which = 0u; which < frets->child_count; ++which) {
            CHECK_CASE(node_int(element(frets, which), &values[which]),
                       index);
        }
        got = kpa_fret_hand_span(values, (size_t)frets->child_count,
                                 max_fret, &hand);
        if (expect->type == KPA_JSON_NULL) {
            /* null means hide the marker, not move it. */
            CHECK_CASE(!got, index);
            ++hidden;
            continue;
        }
        CHECK_CASE(expect->type == KPA_JSON_ARRAY &&
                   expect->child_count == 2u, index);
        {
            int32_t want_low;
            int32_t want_high;

            CHECK_CASE(node_int(element(expect, 0u), &want_low), index);
            CHECK_CASE(node_int(element(expect, 1u), &want_high), index);
            if (!got || hand.low != want_low || hand.high != want_high) {
                (void)fprintf(stderr,
                              "hand case %u: fixture [%d, %d], module ",
                              (unsigned)index, want_low, want_high);
                if (got) {
                    (void)fprintf(stderr, "[%d, %d]\n", hand.low, hand.high);
                } else {
                    (void)fprintf(stderr, "nothing\n");
                }
                return false;
            }
            /* The box is a fixed five frets wide unless the neck itself is
             * shorter, and it never covers the nut. */
            CHECK_CASE(hand.low >= 1 && hand.high <= max_fret, index);
            CHECK_CASE(hand.high - hand.low + 1 == KPA_FRET_HAND_FRETS ||
                       max_fret < KPA_FRET_HAND_FRETS, index);
            ++placed;
        }
    }
    CHECK(placed > 0u && hidden > 0u);
    (void)printf("     %u hand cases: %u placed, %u hidden\n",
                 (unsigned)cases->child_count, (unsigned)placed,
                 (unsigned)hidden);
    return true;
}

static bool test_hand_span_refuses_impossible_input(void)
{
    int32_t frets[2] = {3, 5};
    kpa_fret_hand hand;

    CHECK(!kpa_fret_hand_span(frets, 2u, 0, &hand));
    CHECK(!kpa_fret_hand_span(frets, 2u, 5, NULL));
    CHECK(!kpa_fret_hand_span(NULL, 2u, 5, &hand));
    CHECK(kpa_fret_hand_span(NULL, 0u, 5, &hand) == false);
    CHECK(!kpa_fret_hand_at(NULL, 1.0, &hand));
    return true;
}

static bool test_hand_at_matches_hand_span(void)
{
    fake_tab fake;
    kpa_fret_hand from_tab;
    kpa_fret_hand from_list;
    /* Frets of the events that overlap [t - 0.5, t + 1.5) at t = 10.0 below;
     * the event at 12.0 is outside it and its fret must not count. */
    int32_t in_window[4] = {0, 7, 8, 9};
    int32_t with_outsider[6] = {0, 7, 8, 9, 19, 20};

    fake_tab_init(&fake);
    CHECK(fake_tab_tune(&fake));

    fake_add_event(&fake, 9.8, 10.2);
    fake_add_position(&fake, 0, 0, fake_pitch(&fake, 0, 0));
    fake_add_position(&fake, 1, 7, fake_pitch(&fake, 1, 7));
    fake_add_event(&fake, 11.0, 11.4);
    fake_add_position(&fake, 2, 8, fake_pitch(&fake, 2, 8));
    fake_add_position(&fake, 3, 9, fake_pitch(&fake, 3, 9));
    fake_add_event(&fake, 12.0, 12.4);
    fake_add_position(&fake, 4, 19, fake_pitch(&fake, 4, 19));
    fake_add_position(&fake, 5, 20, fake_pitch(&fake, 5, 20));

    CHECK(kpa_fret_hand_at(&fake.tab, 10.0, &from_tab));
    CHECK(kpa_fret_hand_span(in_window, 4u, (int32_t)fake.tab.max_fret,
                             &from_list));
    CHECK(from_tab.low == from_list.low && from_tab.high == from_list.high);

    /* The event at 12.0 starts exactly at the window's forward edge, and the
     * predicate is start < right, so it is out.  Counting it would move the
     * box. */
    CHECK(kpa_fret_hand_span(with_outsider, 6u, (int32_t)fake.tab.max_fret,
                             &from_list));
    CHECK(from_tab.low != from_list.low || from_tab.high != from_list.high);

    /* A time with nothing anywhere near it hides the marker. */
    CHECK(!kpa_fret_hand_at(&fake.tab, 400.0, &from_tab));
    return true;
}

/* ----------------------------------------------------------- note state */

static bool test_empty_tab(void)
{
    fake_tab fake;
    kpa_fret_note notes[4];
    kpa_fret_note_report report;
    kpa_fret_hand hand;
    kpa_fret_chord chord;

    fake_tab_init(&fake);
    /* No events at all: every query answers, none of them invents a note. */
    CHECK(kpa_fret_notes_at(&fake.tab, 0.0, 1.0, notes, 4u, &report));
    CHECK(report.count == 0u && report.total == 0u);
    CHECK(report.sounding == 0u && report.approaching == 0u);
    CHECK(!report.truncated);
    CHECK(!kpa_fret_hand_at(&fake.tab, 0.0, &hand));
    CHECK(!kpa_fret_chord_of_event(&fake.tab, 0u, &chord));

    /* An empty tab whose arrays are absent as well, which is what a project
     * with no tab document looks like. */
    fake.tab.events = NULL;
    fake.tab.positions = NULL;
    CHECK(kpa_fret_notes_at(&fake.tab, 0.0, 1.0, notes, 4u, &report));
    CHECK(report.total == 0u);
    return true;
}

static bool test_single_note(void)
{
    fake_tab fake;
    kpa_fret_note notes[4];
    kpa_fret_note_report report;
    kpa_fret_chord chord;

    fake_tab_init(&fake);
    CHECK(fake_tab_tune(&fake));
    fake_add_event(&fake, 2.0, 3.0);
    fake_add_position(&fake, 2, 5, fake_pitch(&fake, 2, 5));

    /* Before the lead-in: nothing to draw. */
    CHECK(kpa_fret_notes_at(&fake.tab, 0.5, 1.0, notes, 4u, &report));
    CHECK(report.total == 0u);

    /* Inside the lead-in: approaching, with the time until it starts. */
    CHECK(kpa_fret_notes_at(&fake.tab, 1.5, 1.0, notes, 4u, &report));
    CHECK(report.count == 1u && report.approaching == 1u &&
          report.sounding == 0u);
    CHECK(notes[0].state == KPA_FRET_NOTE_APPROACHING);
    CHECK(fabs(notes[0].time_to_start - 0.5) < 1e-12);
    CHECK(notes[0].progress == 0.0);
    CHECK(notes[0].string_index == 2 && notes[0].fret == 5);
    CHECK(!notes[0].out_of_range);

    /* Sounding, a quarter of the way through its life. */
    CHECK(kpa_fret_notes_at(&fake.tab, 2.25, 1.0, notes, 4u, &report));
    CHECK(report.count == 1u && report.sounding == 1u);
    CHECK(notes[0].state == KPA_FRET_NOTE_SOUNDING);
    CHECK(fabs(notes[0].progress - 0.25) < 1e-12);
    CHECK(notes[0].time_to_start <= 0.0);

    /* Half open at both ends: a note starts at its start and is gone at its
     * end, so a time exactly on the end belongs to whatever comes next. */
    CHECK(kpa_fret_notes_at(&fake.tab, 2.0, 0.0, notes, 4u, &report));
    CHECK(report.sounding == 1u && notes[0].progress == 0.0);
    CHECK(kpa_fret_notes_at(&fake.tab, 3.0, 0.0, notes, 4u, &report));
    CHECK(report.total == 0u);

    /* One note is not a chord. */
    CHECK(!kpa_fret_chord_of_event(&fake.tab, 0u, &chord));
    CHECK(!chord.named);
    return true;
}

static bool test_six_simultaneous_notes(void)
{
    fake_tab fake;
    kpa_fret_note notes[8];
    kpa_fret_note_report report;
    kpa_fret_chord chord;
    kpa_fret_hand hand;
    uint32_t index;
    bool seen[KPA_STRING_COUNT];

    fake_tab_init(&fake);
    CHECK(fake_tab_tune(&fake));
    /* A full six-string open E shape: 0 2 2 1 0 0. */
    fake_add_event(&fake, 5.0, 6.0);
    fake_add_position(&fake, 0, 0, fake_pitch(&fake, 0, 0));
    fake_add_position(&fake, 1, 2, fake_pitch(&fake, 1, 2));
    fake_add_position(&fake, 2, 2, fake_pitch(&fake, 2, 2));
    fake_add_position(&fake, 3, 1, fake_pitch(&fake, 3, 1));
    fake_add_position(&fake, 4, 0, fake_pitch(&fake, 4, 0));
    fake_add_position(&fake, 5, 0, fake_pitch(&fake, 5, 0));

    CHECK(kpa_fret_notes_at(&fake.tab, 5.5, 1.0, notes, 8u, &report));
    CHECK(report.count == KPA_STRING_COUNT);
    CHECK(report.sounding == KPA_STRING_COUNT);
    CHECK(report.out_of_range == 0u && !report.truncated);

    memset(seen, 0, sizeof seen);
    for (index = 0u; index < report.count; ++index) {
        kpa_fret_point point;

        CHECK_CASE(notes[index].string_index >= 0 &&
                   notes[index].string_index < (int32_t)KPA_STRING_COUNT,
                   index);
        /* Six notes at once means six different strings, which is what makes
         * them playable at all. */
        CHECK_CASE(!seen[notes[index].string_index], index);
        seen[notes[index].string_index] = true;
        CHECK_CASE(notes[index].event_index == 0u, index);
        CHECK_CASE(fabs(notes[index].progress - 0.5) < 1e-12, index);
        CHECK_CASE(kpa_fret_point_of(notes[index].string_index,
                                     notes[index].fret,
                                     (int32_t)fake.tab.string_count, 12,
                                     KPA_FRET_HIGH_E_TOP, &point), index);
        CHECK_CASE(point.y > 0.0 && point.y < 1.0, index);
    }

    /* Three distinct pitch classes over six strings: the doubling collapses
     * and the shape is still one chord. */
    CHECK(kpa_fret_chord_of_event(&fake.tab, 0u, &chord));
    CHECK(chord.named && chord.quality == KPA_FRET_CHORD_MAJOR);
    CHECK(strcmp(chord.name, "E") == 0);

    /* Two of the six frets are open, so the hand sits on the other three. */
    CHECK(kpa_fret_hand_at(&fake.tab, 5.2, &hand));
    CHECK(hand.low == 1 && hand.high == 5);
    return true;
}

static bool test_out_of_range_positions(void)
{
    fake_tab fake;
    kpa_fret_note notes[8];
    kpa_fret_note_report report;
    kpa_fret_point point;
    uint32_t index;
    uint32_t flagged = 0u;

    fake_tab_init(&fake);
    CHECK(fake_tab_tune(&fake));
    fake_add_event(&fake, 1.0, 2.0);
    fake_add_position(&fake, 0, 3, fake_pitch(&fake, 0, 3));
    /* A string the instrument does not have and a fret past the end of the
     * neck.  Both are artifact defects; the query reports them rather than
     * dropping them, and geometry is what refuses to place the string. */
    fake_add_position(&fake, (int32_t)KPA_STRING_COUNT + 3, 4, 60);
    fake_add_position(&fake, 1, (int32_t)fake.tab.max_fret + 2, 70);

    CHECK(kpa_fret_notes_at(&fake.tab, 1.5, 0.0, notes, 8u, &report));
    CHECK(report.count == 3u);
    CHECK(report.out_of_range == 2u);
    for (index = 0u; index < report.count; ++index) {
        if (!notes[index].out_of_range) {
            CHECK_CASE(kpa_fret_point_of(notes[index].string_index,
                                         notes[index].fret,
                                         (int32_t)fake.tab.string_count, 20,
                                         KPA_FRET_HIGH_E_TOP, &point), index);
            continue;
        }
        ++flagged;
        if (notes[index].string_index >= (int32_t)fake.tab.string_count) {
            /* No row, so no point: a string that is not there cannot be
             * drawn anywhere on a picture of the strings that are. */
            CHECK_CASE(!kpa_fret_point_of(notes[index].string_index,
                                          notes[index].fret,
                                          (int32_t)fake.tab.string_count, 20,
                                          KPA_FRET_HIGH_E_TOP, &point),
                       index);
        } else {
            /* A fret past the end is still on a string, and lands past the
             * right edge where the renderer can clip it. */
            CHECK_CASE(kpa_fret_point_of(notes[index].string_index,
                                         notes[index].fret,
                                         (int32_t)fake.tab.string_count,
                                         (int32_t)fake.tab.max_fret,
                                         KPA_FRET_HIGH_E_TOP, &point), index);
            CHECK_CASE(point.x > 1.0, index);
        }
    }
    CHECK(flagged == 2u);
    return true;
}

static bool notes_agree(const kpa_fret_note *one, const kpa_fret_note *other)
{
    return one->string_index == other->string_index &&
           one->fret == other->fret && one->pitch == other->pitch &&
           one->state == other->state && one->start == other->start &&
           one->end == other->end && one->progress == other->progress &&
           one->time_to_start == other->time_to_start;
}

static bool test_events_out_of_order(void)
{
    fake_tab sorted;
    fake_tab shuffled;
    double times[6] = {1.0, 1.6, 2.4, 3.1, 4.0, 4.4};
    uint32_t index;

    /* The same three events, stored in time order and stored backwards.  The
     * module scans linearly rather than binary searching from a sorted start
     * time, so both must answer identically at every probe. */
    fake_tab_init(&sorted);
    fake_tab_init(&shuffled);
    CHECK(fake_tab_tune(&sorted));
    CHECK(fake_tab_tune(&shuffled));

    fake_add_event(&sorted, 1.0, 1.5);
    fake_add_position(&sorted, 0, 3, fake_pitch(&sorted, 0, 3));
    fake_add_event(&sorted, 2.0, 2.5);
    fake_add_position(&sorted, 1, 7, fake_pitch(&sorted, 1, 7));
    fake_add_position(&sorted, 2, 9, fake_pitch(&sorted, 2, 9));
    fake_add_event(&sorted, 3.0, 3.5);
    fake_add_position(&sorted, 3, 12, fake_pitch(&sorted, 3, 12));

    fake_add_event(&shuffled, 3.0, 3.5);
    fake_add_position(&shuffled, 3, 12, fake_pitch(&shuffled, 3, 12));
    fake_add_event(&shuffled, 1.0, 1.5);
    fake_add_position(&shuffled, 0, 3, fake_pitch(&shuffled, 0, 3));
    fake_add_event(&shuffled, 2.0, 2.5);
    fake_add_position(&shuffled, 1, 7, fake_pitch(&shuffled, 1, 7));
    fake_add_position(&shuffled, 2, 9, fake_pitch(&shuffled, 2, 9));

    for (index = 0u; index < 6u; ++index) {
        kpa_fret_note ordered_notes[8];
        kpa_fret_note shuffled_notes[8];
        kpa_fret_note_report ordered_report;
        kpa_fret_note_report shuffled_report;
        kpa_fret_hand ordered_hand;
        kpa_fret_hand shuffled_hand;
        bool ordered_has;
        bool shuffled_has;
        uint32_t outer;

        CHECK_CASE(kpa_fret_notes_at(&sorted.tab, times[index], 0.75,
                                     ordered_notes, 8u, &ordered_report),
                   index);
        CHECK_CASE(kpa_fret_notes_at(&shuffled.tab, times[index], 0.75,
                                     shuffled_notes, 8u, &shuffled_report),
                   index);
        CHECK_CASE(ordered_report.count == shuffled_report.count, index);
        CHECK_CASE(ordered_report.sounding == shuffled_report.sounding,
                   index);
        CHECK_CASE(ordered_report.approaching == shuffled_report.approaching,
                   index);

        /* Same notes, not necessarily in the same slots: results come back in
         * artifact order, and the artifact order is the thing that differs. */
        for (outer = 0u; outer < ordered_report.count; ++outer) {
            uint32_t inner;
            uint32_t matches = 0u;

            for (inner = 0u; inner < shuffled_report.count; ++inner) {
                if (notes_agree(&ordered_notes[outer],
                                &shuffled_notes[inner])) {
                    ++matches;
                }
            }
            CHECK_CASE(matches == 1u, index);
        }

        ordered_has = kpa_fret_hand_at(&sorted.tab, times[index],
                                       &ordered_hand);
        shuffled_has = kpa_fret_hand_at(&shuffled.tab, times[index],
                                        &shuffled_hand);
        CHECK_CASE(ordered_has == shuffled_has, index);
        if (ordered_has) {
            CHECK_CASE(ordered_hand.low == shuffled_hand.low, index);
            CHECK_CASE(ordered_hand.high == shuffled_hand.high, index);
        }
    }
    return true;
}

static bool test_notes_truncate_and_refuse(void)
{
    fake_tab fake;
    kpa_fret_note notes[2];
    kpa_fret_note_report report;

    fake_tab_init(&fake);
    CHECK(fake_tab_tune(&fake));
    fake_add_event(&fake, 1.0, 2.0);
    fake_add_position(&fake, 0, 0, fake_pitch(&fake, 0, 0));
    fake_add_position(&fake, 1, 2, fake_pitch(&fake, 1, 2));
    fake_add_position(&fake, 2, 2, fake_pitch(&fake, 2, 2));
    fake_add_position(&fake, 3, 1, fake_pitch(&fake, 3, 1));

    /* More notes than room: the caller is told how many there were rather
     * than being left to think it saw them all. */
    CHECK(kpa_fret_notes_at(&fake.tab, 1.5, 0.0, notes, 2u, &report));
    CHECK(report.count == 2u && report.total == 4u && report.truncated);
    CHECK(report.sounding == 2u);

    /* Zero capacity is a legitimate way to ask "how many". */
    CHECK(kpa_fret_notes_at(&fake.tab, 1.5, 0.0, NULL, 0u, &report));
    CHECK(report.count == 0u && report.total == 4u && report.truncated);

    CHECK(!kpa_fret_notes_at(NULL, 1.5, 0.0, notes, 2u, &report));
    CHECK(report.total == 0u);
    CHECK(!kpa_fret_notes_at(&fake.tab, 1.5, -0.5, notes, 2u, &report));
    CHECK(!kpa_fret_notes_at(&fake.tab, (double)NAN, 0.5, notes, 2u,
                             &report));
    CHECK(!kpa_fret_notes_at(&fake.tab, 1.5, 0.0, NULL, 2u, &report));
    CHECK(!kpa_fret_notes_at(&fake.tab, 1.5, 0.0, notes, 2u, NULL));

    /* An event whose positions run off the end of the position array is a
     * malformed tab, and reading it would be reading past the end. */
    fake.events[0].position_count = fake.tab.position_count + 1u;
    CHECK(!kpa_fret_notes_at(&fake.tab, 1.5, 0.0, notes, 2u, &report));
    CHECK(report.count == 0u && report.total == 0u);
    return true;
}

/* ---------------------------------------------------------------- main */

typedef bool (*test_function)(void);

typedef struct test_entry {
    const char *name;
    test_function run;
} test_entry;

int main(void)
{
    static const test_entry tests[] = {
        {"fret positions are geometric", test_fret_positions},
        {"cell centres sit between the wires", test_cell_centres},
        {"display normalisation fills the box", test_display_normalized},
        {"the exact identities hold", test_identities},
        {"geometry refuses what has no position",
         test_geometry_refuses_impossible_input},
        {"inlays are where the contract puts them", test_inlays},
        {"string order, labels and thickness", test_strings},
        {"orientation rows and points, both conventions",
         test_orientation_and_points},
        {"the two orientations are exact mirrors",
         test_orientations_are_mirrors},
        {"a point needs a string that exists",
         test_point_refuses_impossible_input},
        {"a fret past the drawn neck lands past the edge",
         test_fret_beyond_the_drawn_neck},
        {"pitch classes are spelled with sharps", test_chord_spelling},
        {"every chord vector, name or nothing", test_chords},
        {"chord naming ignores input order",
         test_chord_input_order_is_irrelevant},
        {"chord naming refuses what is not a chord",
         test_chord_refuses_impossible_input},
        {"chord vectors replayed through tab events",
         test_chords_from_events},
        {"the hand window is the contract's window",
         test_hand_window_constants},
        {"every hand-position vector", test_hand_positions},
        {"hand position refuses what it cannot place",
         test_hand_span_refuses_impossible_input},
        {"the tab window and the fret list agree",
         test_hand_at_matches_hand_span},
        {"an empty tab answers without inventing a note", test_empty_tab},
        {"one note: approaching, sounding, gone", test_single_note},
        {"six strings at once", test_six_simultaneous_notes},
        {"a string that is not there and a fret past the end",
         test_out_of_range_positions},
        {"events out of order change no answer", test_events_out_of_order},
        {"more notes than room, and arguments that are not notes",
         test_notes_truncate_and_refuse}
    };
    const size_t count = sizeof tests / sizeof tests[0];
    size_t passed = 0u;
    size_t index;

    if (!vectors_load()) {
        (void)fprintf(stderr,
                      "kpa-fret: the vectors are the contract; refusing to "
                      "report a pass without them\n");
        vectors_free();
        return EXIT_FAILURE;
    }
    for (index = 0u; index < count; ++index) {
        if (tests[index].run()) {
            ++passed;
            (void)printf("ok   %s\n", tests[index].name);
        } else {
            (void)printf("FAIL %s\n", tests[index].name);
        }
    }
    vectors_free();
    (void)printf("kpa-fret: %zu/%zu groups passed\n", passed, count);
    return passed == count ? EXIT_SUCCESS : EXIT_FAILURE;
}
