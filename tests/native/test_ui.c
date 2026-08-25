/*
 * Tests for the native surface.
 *
 * Three things are worth saying about how this is written.
 *
 * Nothing here opens a terminal.  kpa_ui_compose is a pure function of the
 * view model, so the picture it draws can be asserted directly: the same
 * model twice must give byte-identical canvases, and a canvas that drew a
 * second model and then the first again must come back to the first exactly.
 *
 * The tab-lane assertions do not measure the lane's geometry, which would
 * make them a restatement of the implementation.  They compose the same model
 * twice with one note moved out of the visible window, and take the pixels
 * that changed as the note's rectangle.  The string numbers are read back the
 * same way: an 8x16 glyph drawn at scale 1 lands as exact copies of one
 * colour, so scanning for the bit pattern sr_font_glyph_in() reports is an
 * independent way to ask what digit the gutter is showing.
 *
 * No song, lyric, stem, tab or media of any kind appears here.  Every fixture
 * is nonsense synthesised at run time.
 */
#include "kilix_playalong/kpa_ui.h"

#include "kilix_playalong/kpa_project.h"

#include "kitty_keyboard.h"
#include "soft_raster.h"

#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>

/* Mirrors the return codes of kpa_ui_internal_apply_key in kpa_ui.c. */
#define UI_KEY_HANDLED 0
#define UI_KEY_QUIT 1
#define UI_KEY_OPEN 2
#define UI_KEY_CLOSE 3

/*
 * The key table, exported by kpa_ui.c for this file rather than declared in
 * the frozen kpa_ui.h.  It is a pure function of the model; kpa_ui_run turns
 * the difference it makes into audio calls.  Driving it here is the only way
 * to assert that hiding lyrics does not mute a singer.
 */
int kpa_ui_internal_apply_key(kpa_ui_model *model, uint32_t key,
                              uint32_t modifiers);

/*
 * The lyrics band's provenance caption, exported by kpa_ui.c on the same
 * terms.  What a player is told about where a highlight came from is a
 * property of this function; reading it off a terminal would be a test of the
 * terminal.
 */
bool kpa_ui_internal_lyrics_caption(const kpa_lyrics *lyrics, char *out,
                                    size_t size, uint32_t *rgb);

/*
 * The two pieces of the fretboard the surface owns, exported on the same
 * terms again.  The geometry itself and the chord table live in kpa_fret.h
 * and are pinned by the shared fixture; these are where a fret lands on THIS
 * screen - handedness included - and what THIS screen says when the shared
 * namer will not name a chord.
 */
bool kpa_ui_internal_neck_x(uint32_t max_fret, float x, float width,
                            bool left_handed, uint32_t fret,
                            float *out_wire, float *out_finger);
void kpa_ui_internal_chord_label(const int32_t *pitches, uint32_t count,
                                 char *out, size_t size, bool *out_named);

#define CHECK(condition)                                                   \
    do {                                                                   \
        if (!(condition)) {                                                \
            (void)fprintf(stderr, "%s:%d: check failed: %s\n",             \
                          __FILE__, __LINE__, #condition);                 \
            return false;                                                  \
        }                                                                  \
    } while (false)

#define CHECK_AT(condition, w, h)                                          \
    do {                                                                   \
        if (!(condition)) {                                                \
            (void)fprintf(stderr, "%s:%d: %dx%d: %s\n", __FILE__,          \
                          __LINE__, (w), (h), #condition);                 \
            return false;                                                  \
        }                                                                  \
    } while (false)

/* ------------------------------------------------------------ fixtures */

static kpa_ui_model *model_new(void)
{
    kpa_ui_model *model = calloc(1u, sizeof *model);

    if (model == NULL) return NULL;
    model->view = KPA_VIEW_PRACTICE;
    model->title = "Synthetic Fixture";
    model->artist = "Nobody At All";
    model->duration = 213.5;
    model->position = 61.25;
    model->playing = true;
    model->rate = 1.0;
    model->rate_available = true;
    model->lyrics_visible = true;
    model->tab_visible = true;
    model->active_cue = -1;
    model->active_word = -1;
    return model;
}

/* Five stems whose kinds match what the pipeline writes. */
static void model_tracks(kpa_ui_model *model)
{
    static const char *const kinds[5] = {
        "vocals", "rhythm", "bass", "guitar", "other"
    };
    static const char *const labels[5] = {
        "Vocals", "Drums", "Bass", "Guitar", "Other"
    };
    uint32_t index;

    for (index = 0u; index < 5u; ++index) {
        (void)snprintf(model->tracks[index].label,
                       sizeof model->tracks[index].label, "%s",
                       labels[index]);
        (void)snprintf(model->tracks[index].kind,
                       sizeof model->tracks[index].kind, "%s", kinds[index]);
        model->tracks[index].gain = 1.0f;
    }
    model->track_count = 5u;
    model->selected_track = 3u;
}

typedef struct tab_builder {
    kpa_tab tab;
    uint32_t event_capacity;
    uint32_t position_capacity;
} tab_builder;

static bool tab_start(tab_builder *builder, uint32_t strings, uint32_t room)
{
    static const char *const names[KPA_STRING_COUNT] = {
        "E", "A", "D", "G", "B", "e"
    };
    uint32_t index;

    (void)memset(builder, 0, sizeof *builder);
    builder->tab.events = calloc(room, sizeof *builder->tab.events);
    builder->tab.positions = calloc(room, sizeof *builder->tab.positions);
    if (builder->tab.events == NULL || builder->tab.positions == NULL) {
        kpa_tab_free(&builder->tab);
        return false;
    }
    builder->event_capacity = room;
    builder->position_capacity = room;
    builder->tab.string_count = strings;
    builder->tab.max_fret = 12u;
    /* Standard tuning, which is what the artifacts carry: the two highest
     * strings are not a fourth above the ones below them, and a fixture that
     * pretended otherwise would name every chord wrongly. */
    for (index = 0u; index < KPA_STRING_COUNT; ++index) {
        static const int32_t standard[KPA_STRING_COUNT] = {
            40, 45, 50, 55, 59, 64
        };

        builder->tab.tuning_midi[index] = standard[index];
        (void)snprintf(builder->tab.tuning_labels[index],
                       sizeof builder->tab.tuning_labels[index], "%s",
                       names[index]);
    }
    return true;
}

/*
 * One event of any number of simultaneous notes - a real chord shape, which
 * is what 528 of the audited song's 937 events are.  The pitch is the one
 * the fingering implies, tuning included, because a chord name means nothing
 * if the pitches under it are invented.  Events are appended in ascending
 * start order, which is what kpa_tab_first_after's binary search assumes.
 */
static void tab_chord(tab_builder *builder, double start, double end,
                      const uint8_t *strings, const uint8_t *frets,
                      uint32_t count)
{
    kpa_tab *tab = &builder->tab;
    kpa_tab_event *event;
    uint32_t index;

    if (tab->event_count >= builder->event_capacity) return;
    if (tab->position_count + count > builder->position_capacity) return;
    event = &tab->events[tab->event_count];
    event->start = start;
    event->end = end;
    event->first_position = tab->position_count;
    event->position_count = count;
    for (index = 0u; index < count; ++index) {
        kpa_tab_position *position = &tab->positions[tab->position_count];
        const uint8_t string_index = strings[index];
        int32_t pitch = (int32_t)frets[index];

        if (string_index < KPA_STRING_COUNT) {
            pitch += tab->tuning_midi[string_index];
        }
        position->string_index = string_index;
        position->fret = frets[index];
        position->pitch = (uint8_t)(pitch > 0 && pitch < 128 ? pitch : 0);
        tab->position_count++;
    }
    tab->event_count++;
}

/* One event, one fretted note. */
static void tab_note(tab_builder *builder, double start, double end,
                     uint8_t string_index, uint8_t fret)
{
    tab_chord(builder, start, end, &string_index, &fret, 1u);
}

static void tab_release(tab_builder *builder)
{
    kpa_tab_free(&builder->tab);
    builder->event_capacity = 0u;
    builder->position_capacity = 0u;
}

typedef struct lyric_builder {
    kpa_lyrics lyrics;
} lyric_builder;

/*
 * Four cues of three synthetic words each.  The text is deliberately
 * meaningless: no lyric of any real song may exist in this repository.
 */
static bool lyrics_start(lyric_builder *builder)
{
    static const char *const words[3] = {"alpha", "beta", "gamma"};
    kpa_lyrics *lyrics = &builder->lyrics;
    char *cursor;
    uint32_t cue;

    (void)memset(builder, 0, sizeof *builder);
    lyrics->cues = calloc(4u, sizeof *lyrics->cues);
    lyrics->words = calloc(12u, sizeof *lyrics->words);
    lyrics->text_bytes = calloc(512u, 1u);
    if (lyrics->cues == NULL || lyrics->words == NULL ||
        lyrics->text_bytes == NULL) {
        kpa_lyrics_free(lyrics);
        return false;
    }
    lyrics->text_size = 512u;
    cursor = lyrics->text_bytes;
    for (cue = 0u; cue < 4u; ++cue) {
        const double start = 60.0 + (double)cue * 2.0;
        uint32_t word;

        lyrics->cues[cue].start = start;
        lyrics->cues[cue].end = start + 1.8;
        lyrics->cues[cue].first_word = lyrics->word_count;
        lyrics->cues[cue].word_count = 3u;
        lyrics->cues[cue].text = cursor;
        cursor += (size_t)snprintf(cursor, 64u, "alpha beta gamma") + 1u;
        lyrics->cues[cue].length = 16u;
        for (word = 0u; word < 3u; ++word) {
            kpa_word *item = &lyrics->words[lyrics->word_count];

            item->start = start + (double)word * 0.6;
            item->end = item->start + 0.55;
            item->text = cursor;
            item->length = (uint32_t)strlen(words[word]);
            cursor += (size_t)snprintf(cursor, 32u, "%s", words[word]) + 1u;
            lyrics->word_count++;
        }
        lyrics->cue_count++;
    }
    (void)snprintf(lyrics->language, sizeof lyrics->language, "en");
    (void)snprintf(lyrics->source, sizeof lyrics->source, "synthetic");
    return true;
}

static void lyrics_release(lyric_builder *builder)
{
    kpa_lyrics_free(&builder->lyrics);
}

static void summaries_fill(kpa_project_summary *out, uint32_t count)
{
    uint32_t index;

    for (index = 0u; index < count; ++index) {
        (void)snprintf(out[index].id, sizeof out[index].id,
                       "fixture-%04u", (unsigned)index);
        (void)snprintf(out[index].title, sizeof out[index].title,
                       "Fixture Number %u", (unsigned)index);
        (void)snprintf(out[index].artist, sizeof out[index].artist,
                       "Test Artist %u", (unsigned)index);
        out[index].duration = 90.0 + (double)index * 7.5;
        out[index].track_count = 4u + index % 3u;
        out[index].ready = (index % 3u) != 0u;
        out[index].has_lyrics = true;
        out[index].has_tab = (index % 2u) == 0u;
    }
}

/* --------------------------------------------------------- canvas tools */

static size_t canvas_bytes(const sr_canvas *canvas)
{
    return (size_t)canvas->w * (size_t)canvas->h * sizeof *canvas->px;
}

static bool canvas_equal(const sr_canvas *left, const sr_canvas *right)
{
    return left->w == right->w && left->h == right->h &&
           memcmp(left->px, right->px, canvas_bytes(left)) == 0;
}

/* True when every pixel of the row is untouched transparent black. */
static bool row_is_clear(const sr_canvas *canvas, int row, int cell_height)
{
    int y;

    for (y = row * cell_height; y < (row + 1) * cell_height; ++y) {
        int x;

        if (y >= canvas->h) break;
        for (x = 0; x < canvas->w; ++x) {
            if (canvas->px[(size_t)y * (size_t)canvas->w + (size_t)x] != 0u) {
                return false;
            }
        }
    }
    return true;
}

static bool canvas_has_ink(const sr_canvas *canvas)
{
    size_t index;
    const size_t count = (size_t)canvas->w * (size_t)canvas->h;

    for (index = 0u; index < count; ++index) {
        if (canvas->px[index] != 0u) return true;
    }
    return false;
}

typedef struct box {
    int x0;
    int y0;
    int x1;   /* exclusive */
    int y1;   /* exclusive */
    bool found;
} box;

/* Bounding box of the pixels in which two canvases of one size differ. */
static box canvas_difference(const sr_canvas *left, const sr_canvas *right)
{
    box result;
    int y;

    (void)memset(&result, 0, sizeof result);
    for (y = 0; y < left->h; ++y) {
        int x;

        for (x = 0; x < left->w; ++x) {
            const size_t at = (size_t)y * (size_t)left->w + (size_t)x;

            if (left->px[at] == right->px[at]) continue;
            if (!result.found) {
                result.x0 = x;
                result.y0 = y;
                result.x1 = x + 1;
                result.y1 = y + 1;
                result.found = true;
                continue;
            }
            if (x < result.x0) result.x0 = x;
            if (y < result.y0) result.y0 = y;
            if (x + 1 > result.x1) result.x1 = x + 1;
            if (y + 1 > result.y1) result.y1 = y + 1;
        }
    }
    return result;
}

/*
 * An 8x16 glyph drawn at scale 1 with alpha 1 lands as exact copies of one
 * colour, so a cell matches a character when the pixels equal to the ink are
 * exactly the character's set bits.  That makes what a label says readable
 * from the canvas without knowing anything about the palette or the layout.
 */
static bool glyph_at(const sr_canvas *canvas, const uint8_t *rows, int x,
                     int y)
{
    uint32_t ink = 0u;
    bool have_ink = false;
    int dy;

    for (dy = 0; dy < 16; ++dy) {
        int dx;

        for (dx = 0; dx < 8; ++dx) {
            const bool set = ((rows[dy] >> (7 - dx)) & 1u) != 0u;
            const uint32_t pixel =
                canvas->px[(size_t)(y + dy) * (size_t)canvas->w +
                           (size_t)(x + dx)];

            if (set && !have_ink) {
                ink = pixel;
                have_ink = true;
            }
            if (set ? pixel != ink : pixel == ink) return false;
        }
    }
    return have_ink;
}

static bool find_glyph(const sr_canvas *canvas, unsigned char character,
                       int y0, int y1, int *out_x)
{
    const uint8_t *rows = sr_font_glyph_in(SR_FONT_FIXED_8X16, character);
    int y;

    if (rows == NULL) return false;
    if (y0 < 0) y0 = 0;
    if (y1 > canvas->h) y1 = canvas->h;
    for (y = y0; y + 16 <= y1; ++y) {
        int x;

        for (x = 0; x + 8 <= canvas->w; ++x) {
            if (glyph_at(canvas, rows, x, y)) {
                if (out_x != NULL) *out_x = x;
                return true;
            }
        }
    }
    return false;
}

/* ----------------------------------------------------------- purity ---- */

static bool test_compose_is_pure(void)
{
    kpa_ui_model *practice = model_new();
    kpa_ui_model *library = NULL;
    kpa_project_summary summaries[6];
    tab_builder tab;
    lyric_builder lyrics;
    sr_canvas first;
    sr_canvas second;
    sr_canvas reused;
    bool ok = false;

    CHECK(practice != NULL);
    library = model_new();
    if (library == NULL || !tab_start(&tab, KPA_STRING_COUNT, 64u) ||
        !lyrics_start(&lyrics)) {
        free(practice);
        free(library);
        return false;
    }
    model_tracks(practice);
    tab_note(&tab, 60.0, 60.5, 0u, 3u);
    tab_note(&tab, 61.0, 61.6, 3u, 7u);
    tab_note(&tab, 62.0, 62.4, 5u, 0u);
    practice->tab = &tab.tab;
    practice->lyrics = &lyrics.lyrics;
    practice->active_cue = 1;
    practice->active_word = 4;
    practice->loop_active = true;
    practice->loop_start = 40.0;
    practice->loop_end = 95.0;
    practice->underrun = true;
    (void)snprintf(practice->notice, sizeof practice->notice,
                   "a notice that has to draw somewhere");

    summaries_fill(summaries, 6u);
    library->view = KPA_VIEW_LIBRARY;
    library->summaries = summaries;
    library->summary_count = 6u;
    library->selected_project = 4u;

    if (!sr_canvas_init(&first, 900, 620)) goto done;
    if (!sr_canvas_init(&second, 900, 620)) {
        sr_canvas_free(&first);
        goto done;
    }
    if (!sr_canvas_init(&reused, 900, 620)) {
        sr_canvas_free(&first);
        sr_canvas_free(&second);
        goto done;
    }

    kpa_ui_compose(&first, practice);
    kpa_ui_compose(&second, practice);
    ok = canvas_equal(&first, &second);
    if (ok) ok = canvas_has_ink(&first);
    if (ok) {
        /* A then B then A: every pixel of B has to be gone, including the
         * ones inside the bands the overlay owns. */
        kpa_ui_compose(&reused, practice);
        kpa_ui_compose(&reused, library);
        ok = !canvas_equal(&reused, &first);
        if (ok) {
            kpa_ui_compose(&reused, practice);
            ok = canvas_equal(&reused, &first);
        }
    }
    if (ok) {
        /* The clip composition leaves behind must not change the next call. */
        sr_canvas_set_clip(&second, 10, 10, 20, 20);
        kpa_ui_compose(&second, practice);
        ok = canvas_equal(&first, &second);
    }
    sr_canvas_free(&first);
    sr_canvas_free(&second);
    sr_canvas_free(&reused);
done:
    tab_release(&tab);
    lyrics_release(&lyrics);
    free(practice);
    free(library);
    if (!ok) (void)fprintf(stderr, "compose was not reproducible\n");
    return ok;
}

/* ---------------------------------------------- layout and compose agree */

static bool layout_agrees(kpa_ui_model *model, int width, int height,
                          int cell_height)
{
    kpa_ui_cell_layout layout;
    sr_canvas canvas;
    const int rows = height / cell_height;
    int row;
    bool ok = true;

    if (rows <= 0) return true;
    if (!sr_canvas_init(&canvas, width, height)) return false;
    kpa_ui_compose(&canvas, model);
    kpa_ui_cell_layout_get(model, width / 8, rows, cell_height, &layout);

    CHECK_AT(layout.rows == rows, width, height);
    if (layout.title_row >= 0) {
        ok = ok && layout.title_row < rows &&
             row_is_clear(&canvas, layout.title_row, cell_height);
    }
    if (layout.status_row >= 0) {
        ok = ok && layout.status_row < rows &&
             row_is_clear(&canvas, layout.status_row, cell_height);
    }
    for (row = layout.lyric_row;
         layout.lyric_row >= 0 && row < layout.lyric_row +
                                       layout.lyric_row_count; ++row) {
        ok = ok && row < rows && row_is_clear(&canvas, row, cell_height);
        ok = ok && row != layout.title_row && row != layout.status_row;
    }
    if (!ok) {
        (void)fprintf(stderr,
                      "layout %dx%d cell %d: title %d lyric %d+%d status %d "
                      "overlaps drawn pixels\n", width, height, cell_height,
                      layout.title_row, layout.lyric_row,
                      layout.lyric_row_count, layout.status_row);
    }
    sr_canvas_free(&canvas);
    return ok;
}

static bool test_layout_matches_compose(void)
{
    static const int cells[] = {8, 12, 16, 18, 20, 24, 32, 40};
    static const int sizes[][2] = {
        {320, 200}, {640, 400}, {800, 480}, {900, 620}, {1024, 640},
        {1280, 720}, {1400, 900}, {401, 373}, {97, 61}
    };
    kpa_ui_model *model = model_new();
    tab_builder tab;
    lyric_builder lyrics;
    size_t size;
    bool ok = true;

    CHECK(model != NULL);
    if (!tab_start(&tab, KPA_STRING_COUNT, 32u) || !lyrics_start(&lyrics)) {
        free(model);
        return false;
    }
    tab_note(&tab, 61.0, 61.6, 2u, 5u);
    model_tracks(model);
    model->tab = &tab.tab;
    model->lyrics = &lyrics.lyrics;
    model->active_cue = 0;

    for (size = 0u; ok && size < sizeof sizes / sizeof sizes[0]; ++size) {
        size_t cell;

        for (cell = 0u; ok && cell < sizeof cells / sizeof cells[0]; ++cell) {
            int view;

            for (view = 0; ok && view < 3; ++view) {
                model->view = (kpa_view)view;
                model->lyrics_visible = (view % 2) == 0;
                ok = layout_agrees(model, sizes[size][0], sizes[size][1],
                                   cells[cell]);
                if (!ok) break;
                model->lyrics_visible = true;
                ok = layout_agrees(model, sizes[size][0], sizes[size][1],
                                   cells[cell]);
            }
        }
    }
    tab_release(&tab);
    lyrics_release(&lyrics);
    free(model);
    return ok;
}

static bool test_cell_only_draws_no_pixels(void)
{
    kpa_ui_model *model = model_new();
    kpa_ui_cell_layout layout;
    sr_canvas canvas;
    size_t index;
    bool ok = true;

    CHECK(model != NULL);
    model_tracks(model);
    model->cell_only = true;
    if (!sr_canvas_init(&canvas, 640, 400)) {
        free(model);
        return false;
    }
    /* Deliberately dirty, so "no pixels" has to mean the call cleared them
     * rather than that nothing had ever been drawn. */
    for (index = 0u; index < (size_t)(640 * 400); ++index) {
        canvas.px[index] = 0xFF00FF00u;
    }
    kpa_ui_compose(&canvas, model);
    ok = !canvas_has_ink(&canvas);
    if (!ok) (void)fprintf(stderr, "cell_only touched the pixel layer\n");

    /* Every row belongs to the overlay when there is no pixel layer. */
    kpa_ui_cell_layout_get(model, 80, 24, 18, &layout);
    ok = ok && layout.title_row == 0 && layout.status_row == 23 &&
         layout.lyric_row == 1 && layout.lyric_row_count == 22;
    sr_canvas_free(&canvas);
    free(model);
    return ok;
}

/* --------------------------------------------------------- the tab lane */

/*
 * The rectangle one note occupies, found by composing the same model with
 * that note moved far outside the visible window.  Everything else on the
 * canvas - the caption, the string lines, the gutter, the playhead - is
 * identical between the two, so what differs is the note.
 */
static bool note_box(kpa_ui_model *model, tab_builder *tab, uint8_t string,
                     double when, int width, int height, box *out)
{
    sr_canvas near_canvas;
    sr_canvas far_canvas;

    tab->tab.event_count = 0u;
    tab->tab.position_count = 0u;
    tab_note(tab, when, when + 0.4, string, 0u);
    if (!sr_canvas_init(&near_canvas, width, height)) return false;
    kpa_ui_compose(&near_canvas, model);

    tab->tab.event_count = 0u;
    tab->tab.position_count = 0u;
    tab_note(tab, when + 500.0, when + 500.4, string, 0u);
    if (!sr_canvas_init(&far_canvas, width, height)) {
        sr_canvas_free(&near_canvas);
        return false;
    }
    kpa_ui_compose(&far_canvas, model);
    *out = canvas_difference(&near_canvas, &far_canvas);
    sr_canvas_free(&near_canvas);
    sr_canvas_free(&far_canvas);
    return true;
}

static bool test_tab_lane_is_high_to_low(void)
{
    kpa_ui_model *model = model_new();
    tab_builder tab;
    box low;
    box high;
    bool ok;

    CHECK(model != NULL);
    if (!tab_start(&tab, KPA_STRING_COUNT, 8u)) {
        free(model);
        return false;
    }
    model_tracks(model);
    model->tab = &tab.tab;
    model->lyrics_visible = false;
    /* note_box takes the pixels that changed when a note moved.  With a neck
     * drawn, the dot it lights and the mark on the approach ramp change too,
     * and the box would span both widgets instead of measuring the lane. */
    model->fretboard = KPA_FB_OFF;

    ok = note_box(model, &tab, 0u, model->position, 1400, 900, &low) &&
         note_box(model, &tab, 5u, model->position, 1400, 900, &high);
    if (ok) ok = low.found && high.found;
    if (ok && low.y0 <= high.y1) {
        (void)fprintf(stderr,
                      "the low E landed at y %d..%d and the high e at %d..%d:"
                      " the lane is not drawn high to low\n",
                      low.y0, low.y1, high.y0, high.y1);
        ok = false;
    }
    tab_release(&tab);
    free(model);
    return ok;
}

static bool test_string_numbers_are_the_players(void)
{
    kpa_ui_model *model = model_new();
    tab_builder tab;
    box low;
    box high;
    sr_canvas canvas;
    bool ok;

    CHECK(model != NULL);
    if (!tab_start(&tab, KPA_STRING_COUNT, 8u)) {
        free(model);
        return false;
    }
    model_tracks(model);
    model->tab = &tab.tab;
    model->lyrics_visible = false;
    model->fretboard = KPA_FB_OFF;   /* the lane's numbers, not the neck's */

    ok = note_box(model, &tab, 0u, model->position, 1400, 900, &low) &&
         note_box(model, &tab, 5u, model->position, 1400, 900, &high) &&
         low.found && high.found;
    if (!ok) {
        tab_release(&tab);
        free(model);
        return false;
    }
    tab.tab.event_count = 0u;
    tab.tab.position_count = 0u;
    tab_note(&tab, model->position, model->position + 0.4, 0u, 0u);
    if (!sr_canvas_init(&canvas, 1400, 900)) {
        tab_release(&tab);
        free(model);
        return false;
    }
    kpa_ui_compose(&canvas, model);
    /*
     * The bottom row carries the low E, and a player calls it string 6.  The
     * API calls it index 0; a surface that printed the index would show a 1
     * here, which is the bug the browser shipped.
     */
    if (!find_glyph(&canvas, '6', low.y0 - 8, low.y1 + 8, NULL)) {
        (void)fprintf(stderr, "the low E row is not labelled string 6\n");
        ok = false;
    }
    if (find_glyph(&canvas, '1', low.y0 - 8, low.y1 + 8, NULL)) {
        (void)fprintf(stderr, "the low E row is labelled with its API index\n");
        ok = false;
    }
    if (!find_glyph(&canvas, '1', high.y0 - 8, high.y1 + 8, NULL)) {
        (void)fprintf(stderr, "the high e row is not labelled string 1\n");
        ok = false;
    }
    if (find_glyph(&canvas, '6', high.y0 - 8, high.y1 + 8, NULL)) {
        (void)fprintf(stderr, "the high e row is labelled with its index\n");
        ok = false;
    }
    sr_canvas_free(&canvas);
    tab_release(&tab);
    free(model);
    return ok;
}

static bool test_gutter_is_pinned(void)
{
    /* One note at a fixed moment in the song and the playhead moving past
     * it, which is the situation the browser's gutter slid away in. */
    static const double when = 61.25;
    static const double positions[] = {58.0, 60.0, 61.25, 62.5, 64.0};
    kpa_ui_model *model = model_new();
    tab_builder tab;
    box low;
    sr_canvas canvas;
    int gutter_x = -1;
    int first_note_x = -1;
    bool moved = false;
    size_t index;
    bool ok = true;

    CHECK(model != NULL);
    if (!tab_start(&tab, KPA_STRING_COUNT, 8u)) {
        free(model);
        return false;
    }
    model_tracks(model);
    model->tab = &tab.tab;
    model->lyrics_visible = false;
    /* The neck labels its own strings too, and its `6` is inside the y range
     * this scans.  The question here is about the lane's gutter. */
    model->fretboard = KPA_FB_OFF;

    for (index = 0u; ok && index < sizeof positions / sizeof positions[0];
         ++index) {
        int found_x = -1;

        model->position = positions[index];
        if (!note_box(model, &tab, 0u, when, 1400, 900, &low) || !low.found) {
            ok = false;
            break;
        }
        tab.tab.event_count = 0u;
        tab.tab.position_count = 0u;
        tab_note(&tab, when, when + 0.4, 0u, 0u);
        if (!sr_canvas_init(&canvas, 1400, 900)) {
            ok = false;
            break;
        }
        kpa_ui_compose(&canvas, model);
        ok = find_glyph(&canvas, '6', low.y0 - 8, low.y1 + 8, &found_x);
        sr_canvas_free(&canvas);
        if (!ok) break;
        if (gutter_x < 0) {
            gutter_x = found_x;
            first_note_x = low.x0;
        } else if (found_x != gutter_x) {
            (void)fprintf(stderr,
                          "the gutter moved from x %d to x %d at %.1fs: it is"
                          " translating with the lane\n", gutter_x, found_x,
                          positions[index]);
            ok = false;
        }
        if (low.x0 != first_note_x) moved = true;
    }
    /* And the lane really is rolling, or the test above proves nothing. */
    if (ok && !moved) {
        (void)fprintf(stderr, "the lane never moved; the pin proves nothing\n");
        ok = false;
    }
    tab_release(&tab);
    free(model);
    return ok;
}

/* ------------------------------------------------------------- geometry */

static bool test_every_view_at_every_size(void)
{
    static const int sizes[][2] = {
        {1, 1}, {2, 3}, {3, 1}, {5, 7}, {8, 16}, {17, 9}, {31, 47},
        {64, 32}, {80, 24}, {129, 3}, {3, 129}, {160, 120}, {320, 200},
        {401, 137}, {640, 400}, {1280, 720}
    };
    kpa_ui_model *model = model_new();
    kpa_project_summary summaries[6];
    tab_builder tab;
    lyric_builder lyrics;
    size_t index;
    bool ok = true;

    CHECK(model != NULL);
    if (!tab_start(&tab, KPA_STRING_COUNT, 32u) || !lyrics_start(&lyrics)) {
        free(model);
        return false;
    }
    tab_note(&tab, 60.5, 61.0, 0u, 3u);
    tab_note(&tab, 61.0, 61.5, 5u, 12u);
    summaries_fill(summaries, 6u);
    model_tracks(model);
    model->tab = &tab.tab;
    model->lyrics = &lyrics.lyrics;
    model->active_cue = 1;
    model->active_word = 4;
    model->summaries = summaries;
    model->summary_count = 6u;
    model->selected_project = 5u;
    model->loop_active = true;
    model->loop_start = 30.0;
    model->loop_end = 120.0;
    (void)snprintf(model->notice, sizeof model->notice, "notice");

    for (index = 0u; index < sizeof sizes / sizeof sizes[0]; ++index) {
        const int width = sizes[index][0];
        const int height = sizes[index][1];
        int variant;

        for (variant = 0; variant < 12; ++variant) {
            kpa_ui_cell_layout layout;
            sr_canvas canvas;

            model->view = (kpa_view)(variant % 3);
            model->lyrics_visible = (variant & 4) == 0;
            model->tab_visible = (variant & 8) == 0;
            model->cell_only = variant >= 6;
            if (!sr_canvas_init(&canvas, width, height)) {
                ok = false;
                break;
            }
            kpa_ui_compose(&canvas, model);
            sr_canvas_free(&canvas);
            /* Degenerate grids have to answer, not walk off an array. */
            kpa_ui_cell_layout_get(model, width / 8, height / 18, 18,
                                   &layout);
            CHECK_AT(layout.lyric_row_count >= 0, width, height);
        }
        if (!ok) break;
    }
    /* The guards, which a fuzzer would find first. */
    kpa_ui_compose(NULL, model);
    {
        sr_canvas canvas;
        kpa_ui_cell_layout layout;

        if (sr_canvas_init(&canvas, 64, 64)) {
            kpa_ui_compose(&canvas, NULL);
            ok = ok && !canvas_has_ink(&canvas);
            sr_canvas_free(&canvas);
        }
        kpa_ui_cell_layout_get(NULL, 80, 24, 18, &layout);
        kpa_ui_cell_layout_get(model, 0, 0, 0, &layout);
        ok = ok && layout.title_row == -1 && layout.status_row == -1 &&
             layout.lyric_row_count == 0;
        kpa_ui_cell_layout_get(model, 80, 24, -4, &layout);
        ok = ok && layout.title_row == -1;
        kpa_ui_cell_layout_get(model, 80, 24, 18, NULL);
    }
    tab_release(&tab);
    lyrics_release(&lyrics);
    free(model);
    return ok;
}

/* ------------------------------------------------------------ the keys */

static bool test_transport_keys(void)
{
    kpa_ui_model *model = model_new();
    bool ok = true;

    CHECK(model != NULL);
    model_tracks(model);
    model->playing = false;

    CHECK(kpa_ui_internal_apply_key(model, ' ', 0u) == UI_KEY_HANDLED);
    ok = ok && model->playing;
    (void)kpa_ui_internal_apply_key(model, ' ', 0u);
    ok = ok && !model->playing;

    model->position = 61.25;
    (void)kpa_ui_internal_apply_key(model, KITTYKB_KEY_LEFT, 0u);
    ok = ok && model->position == 56.25;
    (void)kpa_ui_internal_apply_key(model, KITTYKB_KEY_RIGHT, 0u);
    ok = ok && model->position == 61.25;
    /* Shift changes the step; it does not turn seeking into something else. */
    (void)kpa_ui_internal_apply_key(model, KITTYKB_KEY_RIGHT,
                                    KITTYKB_MOD_SHIFT);
    ok = ok && model->position == 91.25;
    (void)kpa_ui_internal_apply_key(model, KITTYKB_KEY_LEFT,
                                    KITTYKB_MOD_SHIFT);
    ok = ok && model->position == 61.25;

    /* Bounded at both ends: never before the song, never past it. */
    model->position = 2.0;
    (void)kpa_ui_internal_apply_key(model, KITTYKB_KEY_LEFT, 0u);
    ok = ok && model->position == 0.0;
    model->position = model->duration - 1.0;
    (void)kpa_ui_internal_apply_key(model, KITTYKB_KEY_RIGHT,
                                    KITTYKB_MOD_SHIFT);
    ok = ok && model->position == model->duration;

    /* Nothing about seeking touches what is muted or what is shown. */
    ok = ok && model->lyrics_visible && model->tab_visible &&
         !model->tracks[0].muted;
    free(model);
    if (!ok) (void)fprintf(stderr, "transport keys\n");
    return ok;
}

static bool test_loop_keys(void)
{
    kpa_ui_model *model = model_new();
    bool ok = true;

    CHECK(model != NULL);
    model_tracks(model);
    model->position = 61.25;
    (void)kpa_ui_internal_apply_key(model, '[', 0u);
    ok = ok && model->loop_start == 61.25 && model->loop_active;
    model->position = 95.0;
    (void)kpa_ui_internal_apply_key(model, ']', 0u);
    ok = ok && model->loop_end == 95.0 && model->loop_active;

    (void)kpa_ui_internal_apply_key(model, KITTYKB_KEY_BACKSPACE, 0u);
    ok = ok && !model->loop_active && model->loop_start == 0.0 &&
         model->loop_end == 0.0;

    /* An end before its start is refused and said out loud, not stored. */
    model->position = 100.0;
    (void)kpa_ui_internal_apply_key(model, '[', 0u);
    model->position = 20.0;
    (void)kpa_ui_internal_apply_key(model, ']', 0u);
    ok = ok && !model->loop_active && model->notice[0] != '\0';
    free(model);
    if (!ok) (void)fprintf(stderr, "loop keys\n");
    return ok;
}

static bool test_stem_keys(void)
{
    kpa_ui_model *model = model_new();
    bool ok = true;

    CHECK(model != NULL);
    model_tracks(model);          /* five stems */
    (void)kpa_ui_internal_apply_key(model, '1', 0u);
    ok = ok && model->selected_track == 0u;
    (void)kpa_ui_internal_apply_key(model, '5', 0u);
    ok = ok && model->selected_track == 4u;
    /* Six is a key, not a promise that a sixth stem exists. */
    (void)kpa_ui_internal_apply_key(model, '6', 0u);
    ok = ok && model->selected_track == 4u && model->notice[0] != '\0';

    (void)kpa_ui_internal_apply_key(model, KITTYKB_KEY_TAB, 0u);
    ok = ok && model->selected_track == 0u;
    (void)kpa_ui_internal_apply_key(model, KITTYKB_KEY_TAB,
                                    KITTYKB_MOD_SHIFT);
    ok = ok && model->selected_track == 4u;

    (void)kpa_ui_internal_apply_key(model, 'm', 0u);
    ok = ok && model->tracks[4].muted;
    (void)kpa_ui_internal_apply_key(model, 'm', 0u);
    ok = ok && !model->tracks[4].muted;
    (void)kpa_ui_internal_apply_key(model, 's', 0u);
    ok = ok && model->tracks[4].soloed;

    /* Gain is clamped at both ends rather than wrapping or running away. */
    (void)kpa_ui_internal_apply_key(model, '+', 0u);
    ok = ok && model->tracks[4].gain > 1.04f && model->tracks[4].gain < 1.06f;
    (void)kpa_ui_internal_apply_key(model, '-', 0u);
    (void)kpa_ui_internal_apply_key(model, '-', 0u);
    ok = ok && model->tracks[4].gain > 0.94f && model->tracks[4].gain < 0.96f;
    {
        int press;

        for (press = 0; press < 60; ++press) {
            (void)kpa_ui_internal_apply_key(model, '=', 0u);
        }
        ok = ok && model->tracks[4].gain == 2.0f;
        for (press = 0; press < 80; ++press) {
            (void)kpa_ui_internal_apply_key(model, '_', 0u);
        }
        ok = ok && model->tracks[4].gain == 0.0f;
    }
    free(model);
    if (!ok) (void)fprintf(stderr, "stem keys\n");
    return ok;
}

/*
 * The property the whole surface is built around: what is heard and what is
 * shown are two things.  A key that changes one must leave the other exactly
 * as it was.
 */
static bool test_audio_and_display_are_independent(void)
{
    kpa_ui_model *model = model_new();
    bool ok = true;

    CHECK(model != NULL);
    model_tracks(model);          /* vocals at 0, guitar at 3 */

    /* l hides the words and mutes nobody. */
    (void)kpa_ui_internal_apply_key(model, 'l', 0u);
    ok = ok && !model->lyrics_visible;
    ok = ok && !model->tracks[0].muted && !model->tracks[3].muted;
    if (!ok) (void)fprintf(stderr, "l changed the audio\n");

    /* v mutes the singer and hides nothing. */
    (void)kpa_ui_internal_apply_key(model, 'v', 0u);
    if (!model->tracks[0].muted) {
        (void)fprintf(stderr, "v did not mute the vocal stem\n");
        ok = false;
    }
    if (model->lyrics_visible) {
        (void)fprintf(stderr, "v changed the lyric layer\n");
        ok = false;
    }
    (void)kpa_ui_internal_apply_key(model, 'l', 0u);
    ok = ok && model->lyrics_visible && model->tracks[0].muted;
    if (!ok) (void)fprintf(stderr, "showing lyrics unmuted the vocals\n");

    /* t hides the tab lane and mutes no guitar. */
    (void)kpa_ui_internal_apply_key(model, 't', 0u);
    ok = ok && !model->tab_visible && !model->tracks[3].muted;
    if (!ok) (void)fprintf(stderr, "t changed the audio\n");

    /* ...and muting the guitar hides no tab. */
    (void)kpa_ui_internal_apply_key(model, '4', 0u);
    ok = ok && model->selected_track == 3u;
    (void)kpa_ui_internal_apply_key(model, 'm', 0u);
    ok = ok && model->tracks[3].muted && !model->tab_visible;
    (void)kpa_ui_internal_apply_key(model, 't', 0u);
    ok = ok && model->tab_visible && model->tracks[3].muted;
    if (!ok) {
        (void)fprintf(stderr, "the tab layer and the guitar are tangled\n");
    }

    /* A project with no singer says so instead of muting something else. */
    {
        uint32_t index;

        for (index = 0u; index < model->track_count; ++index) {
            (void)snprintf(model->tracks[index].kind,
                           sizeof model->tracks[index].kind, "other");
            model->tracks[index].muted = false;
        }
        (void)kpa_ui_internal_apply_key(model, 'v', 0u);
        ok = ok && model->notice[0] != '\0';
        for (index = 0u; index < model->track_count; ++index) {
            ok = ok && !model->tracks[index].muted;
        }
    }
    free(model);
    return ok;
}

static bool test_rate_keys(void)
{
    kpa_ui_model *model = model_new();
    bool ok = true;
    int press;

    CHECK(model != NULL);
    model_tracks(model);
    (void)kpa_ui_internal_apply_key(model, '.', 0u);
    ok = ok && model->rate > 1.049 && model->rate < 1.051;
    (void)kpa_ui_internal_apply_key(model, ',', 0u);
    ok = ok && model->rate > 0.999 && model->rate < 1.001;
    for (press = 0; press < 40; ++press) {
        (void)kpa_ui_internal_apply_key(model, '>', 0u);
    }
    ok = ok && model->rate == 1.5;
    for (press = 0; press < 60; ++press) {
        (void)kpa_ui_internal_apply_key(model, '<', 0u);
    }
    ok = ok && model->rate == 0.5;

    /* Without a qualified engine the rate does not move, and the surface
     * says why rather than transposing the song by a semitone in silence. */
    model->rate = 1.0;
    model->rate_available = false;
    (void)kpa_ui_internal_apply_key(model, '.', 0u);
    ok = ok && model->rate == 1.0 && model->notice[0] != '\0';
    (void)kpa_ui_internal_apply_key(model, ',', 0u);
    ok = ok && model->rate == 1.0;
    free(model);
    if (!ok) (void)fprintf(stderr, "rate keys\n");
    return ok;
}

static bool test_views_and_escape(void)
{
    kpa_ui_model *model = model_new();
    kpa_project_summary summaries[3];
    bool ok = true;

    CHECK(model != NULL);
    model_tracks(model);
    summaries_fill(summaries, 3u);
    model->summaries = summaries;
    model->summary_count = 3u;

    CHECK(kpa_ui_internal_apply_key(model, '?', 0u) == UI_KEY_HANDLED);
    ok = ok && model->view == KPA_VIEW_HELP;
    (void)kpa_ui_internal_apply_key(model, '?', 0u);
    ok = ok && model->view == KPA_VIEW_PRACTICE;
    /* Shifted slash is the same question mark on a legacy terminal. */
    (void)kpa_ui_internal_apply_key(model, '/', KITTYKB_MOD_SHIFT);
    ok = ok && model->view == KPA_VIEW_HELP;

    /* Escape always leaves something, and from the outermost view it leaves
     * the player: it never becomes a key that does nothing. */
    (void)kpa_ui_internal_apply_key(model, KITTYKB_KEY_ESCAPE, 0u);
    ok = ok && model->view == KPA_VIEW_PRACTICE;
    ok = ok && kpa_ui_internal_apply_key(model, KITTYKB_KEY_ESCAPE, 0u) ==
               UI_KEY_CLOSE;
    ok = ok && model->view == KPA_VIEW_LIBRARY;
    ok = ok && kpa_ui_internal_apply_key(model, KITTYKB_KEY_ESCAPE, 0u) ==
               UI_KEY_QUIT;

    /* Library navigation, and Enter as the only thing that opens. */
    model->view = KPA_VIEW_LIBRARY;
    model->selected_project = 0u;
    (void)kpa_ui_internal_apply_key(model, KITTYKB_KEY_TAB, 0u);
    ok = ok && model->selected_project == 1u;
    (void)kpa_ui_internal_apply_key(model, KITTYKB_KEY_TAB,
                                    KITTYKB_MOD_SHIFT);
    ok = ok && model->selected_project == 0u;
    (void)kpa_ui_internal_apply_key(model, KITTYKB_KEY_UP, 0u);
    ok = ok && model->selected_project == 2u;
    ok = ok && kpa_ui_internal_apply_key(model, KITTYKB_KEY_ENTER, 0u) ==
               UI_KEY_OPEN;

    /* q quits from anywhere, in either case. */
    ok = ok && kpa_ui_internal_apply_key(model, 'q', 0u) == UI_KEY_QUIT;
    model->view = KPA_VIEW_PRACTICE;
    ok = ok && kpa_ui_internal_apply_key(model, 'Q', KITTYKB_MOD_SHIFT) ==
               UI_KEY_QUIT;
    /* Uppercase from a terminal that has no keyboard protocol is the same
     * key, so M has to mute as m does. */
    model->selected_track = 2u;
    (void)kpa_ui_internal_apply_key(model, 'M', KITTYKB_MOD_SHIFT);
    ok = ok && model->tracks[2].muted;

    ok = ok && kpa_ui_internal_apply_key(NULL, 'q', 0u) == UI_KEY_HANDLED;
    free(model);
    if (!ok) (void)fprintf(stderr, "views and escape\n");
    return ok;
}

/* ------------------------------------------------------- the fretboard */

/*
 * Where the drawn neck puts a fret.  Asserted here rather than read back out
 * of a canvas: the geometry itself is kpa_fret.c's and is pinned by its own
 * fixture, and what this file is responsible for is that the practice screen
 * uses it - proportionally, and mirrored when the player is left-handed.
 */
static bool test_neck_is_a_neck(void)
{
    const float width = 1000.0f;
    float wire[25];
    float finger[25];
    uint32_t fret;
    bool ok = true;

    for (fret = 0u; fret <= 20u; ++fret) {
        CHECK(kpa_ui_internal_neck_x(20u, 100.0f, width, false, fret,
                                     &wire[fret], &finger[fret]));
    }
    /* Fret 0 is the nut, and the far fret fills the box. */
    CHECK(wire[0] > 99.9f && wire[0] < 100.1f);
    CHECK(wire[20] > 1099.9f && wire[20] < 1100.1f);
    for (fret = 1u; fret <= 20u; ++fret) {
        /* Ascending, and the finger goes behind the wire that names it. */
        CHECK(wire[fret] > wire[fret - 1u]);
        CHECK(finger[fret] > wire[fret - 1u] && finger[fret] < wire[fret]);
    }
    /*
     * THE ASSERTION THAT FORBIDS A FAKE FRETBOARD: every fret cell is
     * narrower than the one before it.  Evenly spaced frets are the single
     * thing that makes a drawn neck look wrong, and nothing else in this
     * file would notice them.
     */
    for (fret = 2u; fret <= 20u; ++fret) {
        const float cell = wire[fret] - wire[fret - 1u];
        const float before = wire[fret - 1u] - wire[fret - 2u];

        if (!(cell < before)) {
            (void)fprintf(stderr,
                          "fret %u is %.2f px wide and fret %u is %.2f: the "
                          "neck is not getting tighter\n", (unsigned)fret,
                          (double)cell, (unsigned)(fret - 1u),
                          (double)before);
            ok = false;
        }
    }
    /* The audited song's neck: frets 0..8 hold 92.8% of its notes, and the
     * physically correct spacing gives them more than half the width. */
    CHECK(wire[8] - wire[0] > width * 0.5f);
    /* Left-handed is the same neck seen from the other side. */
    for (fret = 0u; fret <= 20u; ++fret) {
        float mirrored = 0.0f;

        CHECK(kpa_ui_internal_neck_x(20u, 100.0f, width, true, fret,
                                     &mirrored, NULL));
        CHECK(mirrored > 100.0f + width - wire[fret] + 99.0f);
        CHECK(mirrored < 100.0f + width - wire[fret] + 101.0f);
    }
    /* Refused rather than guessed at. */
    CHECK(!kpa_ui_internal_neck_x(0u, 0.0f, 100.0f, false, 0u, &wire[0],
                                  NULL));
    CHECK(!kpa_ui_internal_neck_x(20u, 0.0f, -1.0f, false, 0u, &wire[0],
                                  NULL));
    CHECK(!kpa_ui_internal_neck_x(20u, 0.0f, 100.0f, false, 21u, &wire[0],
                                  NULL));
    return ok;
}

/*
 * One note per string, latest onset wins.
 *
 * A transcription's sustains overlap the next note on the same string: on
 * the audited song 46% of 20 Hz samples have two to four notes claiming one
 * string at once.  The neck draws the newest of them and nothing else, so
 * the picture of a string whose older note is still written down has to be
 * the same picture as one where that note had already ended.
 *
 * The decoy on the high e is there to hold the hand position still: without
 * it the two tabs put the five-fret box in different places and the canvases
 * would differ for a reason that has nothing to do with the rule.
 */
static bool test_one_note_per_string(void)
{
    const double when = 61.25;
    kpa_ui_model *model = model_new();
    tab_builder both;
    tab_builder newest;
    tab_builder oldest;
    sr_canvas with_older;
    sr_canvas without_older;
    sr_canvas older_alone;
    bool ok = false;

    CHECK(model != NULL);
    if (!tab_start(&both, KPA_STRING_COUNT, 8u) ||
        !tab_start(&newest, KPA_STRING_COUNT, 8u) ||
        !tab_start(&oldest, KPA_STRING_COUNT, 8u)) {
        free(model);
        return false;
    }
    model_tracks(model);
    model->position = when;
    model->lyrics_visible = false;
    model->tab_visible = false;
    model->fretboard = KPA_FB_NECK;

    tab_note(&both, when - 0.5, when + 0.5, 0u, 2u);
    tab_note(&both, when - 0.5, when + 0.5, 5u, 2u);
    tab_note(&both, when - 0.1, when + 0.5, 0u, 7u);

    tab_note(&newest, when - 0.5, when + 0.5, 5u, 2u);
    tab_note(&newest, when - 0.1, when + 0.5, 0u, 7u);

    tab_note(&oldest, when - 0.5, when + 0.5, 0u, 2u);
    tab_note(&oldest, when - 0.5, when + 0.5, 5u, 2u);

    if (!sr_canvas_init(&with_older, 1280, 720)) goto done;
    if (!sr_canvas_init(&without_older, 1280, 720)) {
        sr_canvas_free(&with_older);
        goto done;
    }
    if (!sr_canvas_init(&older_alone, 1280, 720)) {
        sr_canvas_free(&with_older);
        sr_canvas_free(&without_older);
        goto done;
    }
    model->tab = &both.tab;
    kpa_ui_compose(&with_older, model);
    model->tab = &newest.tab;
    kpa_ui_compose(&without_older, model);
    model->tab = &oldest.tab;
    kpa_ui_compose(&older_alone, model);

    ok = canvas_equal(&with_older, &without_older);
    if (!ok) {
        const box difference = canvas_difference(&with_older,
                                                 &without_older);

        (void)fprintf(stderr,
                      "a note the same string had already moved on from is "
                      "still drawn, at %d..%d x %d..%d\n", difference.x0,
                      difference.x1, difference.y0, difference.y1);
    }
    /* ...and the picture really does depend on which note is newest, or the
     * comparison above would hold for a neck that drew nothing. */
    if (ok && canvas_equal(&with_older, &older_alone)) {
        (void)fprintf(stderr, "the neck drew the same thing either way\n");
        ok = false;
    }
    sr_canvas_free(&with_older);
    sr_canvas_free(&without_older);
    sr_canvas_free(&older_alone);
done:
    tab_release(&both);
    tab_release(&newest);
    tab_release(&oldest);
    free(model);
    return ok;
}

/* The picture of one note at three ages, and the tail a very short note is
 * still drawn with. */
static bool test_note_envelope(void)
{
    const double start = 61.25;
    kpa_ui_model *model = model_new();
    tab_builder tab;
    sr_canvas attack;
    sr_canvas sustain;
    sr_canvas decay;
    sr_canvas after;
    bool ok = false;

    CHECK(model != NULL);
    if (!tab_start(&tab, KPA_STRING_COUNT, 8u)) {
        free(model);
        return false;
    }
    model_tracks(model);
    model->lyrics_visible = false;
    model->tab_visible = false;
    model->fretboard = KPA_FB_NECK;
    model->tab = &tab.tab;
    /* A two-second note, long enough to have three distinct ages. */
    tab_note(&tab, start, start + 2.0, 2u, 5u);

    if (!sr_canvas_init(&attack, 1024, 640)) goto done;
    if (!sr_canvas_init(&sustain, 1024, 640)) {
        sr_canvas_free(&attack);
        goto done;
    }
    if (!sr_canvas_init(&decay, 1024, 640)) {
        sr_canvas_free(&attack);
        sr_canvas_free(&sustain);
        goto done;
    }
    if (!sr_canvas_init(&after, 1024, 640)) {
        sr_canvas_free(&attack);
        sr_canvas_free(&sustain);
        sr_canvas_free(&decay);
        goto done;
    }
    model->position = start + 0.01;
    kpa_ui_compose(&attack, model);
    model->position = start + 1.0;
    kpa_ui_compose(&sustain, model);
    model->position = start + 1.99;
    kpa_ui_compose(&decay, model);
    ok = !canvas_equal(&attack, &sustain) && !canvas_equal(&sustain, &decay) &&
         !canvas_equal(&attack, &decay);
    if (!ok) {
        (void)fprintf(stderr, "a note is drawn the same at every age\n");
    }
    if (ok) {
        /*
         * The shortest event in the audited song is 0.094 s, which is about
         * two frames at the loop's 40 ms poll.  It is drawn for
         * KPA_FB_MIN_LIFE from its onset instead, so it is still there a
         * fifth of a second later - and gone by a third of one.
         */
        tab.tab.event_count = 0u;
        tab.tab.position_count = 0u;
        tab_note(&tab, start, start + 0.094, 2u, 5u);
        model->position = start + 0.20;
        kpa_ui_compose(&attack, model);
        model->position = start + 0.35;
        kpa_ui_compose(&after, model);
        ok = !canvas_equal(&attack, &after);
        if (!ok) {
            (void)fprintf(stderr,
                          "a 0.094s note looks the same at 0.20s and 0.35s: "
                          "the tail is not being drawn\n");
        }
    }
    sr_canvas_free(&attack);
    sr_canvas_free(&sustain);
    sr_canvas_free(&decay);
    sr_canvas_free(&after);
done:
    tab_release(&tab);
    free(model);
    return ok;
}

/*
 * What the callout says a set of pitches is.  The chord table itself is
 * kpa_fret.c's and is asserted against the shared fixture there; what is
 * asserted here is the surface's own decision - that a set the shared namer
 * refuses is shown as the notes it is made of and is never dressed up as a
 * chord.
 */
static bool test_chord_labels(void)
{
    static const int32_t e5[3] = {40, 47, 52};
    static const int32_t e_major[3] = {40, 44, 47};
    static const int32_t a_minor[3] = {45, 48, 52};
    static const int32_t a_major[3] = {45, 49, 52};
    static const int32_t inversion[3] = {52, 57, 60};
    static const int32_t third[2] = {40, 44};
    static const int32_t nameless[3] = {40, 42, 49};
    char label[24];
    bool named = false;

    kpa_ui_internal_chord_label(e5, 3u, label, sizeof label, &named);
    CHECK(named && strcmp(label, "E5") == 0);
    kpa_ui_internal_chord_label(e_major, 3u, label, sizeof label, &named);
    CHECK(named && strcmp(label, "E") == 0);
    kpa_ui_internal_chord_label(a_minor, 3u, label, sizeof label, &named);
    CHECK(named && strcmp(label, "Am") == 0);
    kpa_ui_internal_chord_label(a_major, 3u, label, sizeof label, &named);
    CHECK(named && strcmp(label, "A") == 0);
    /* The same chord over a different lowest note says so. */
    kpa_ui_internal_chord_label(inversion, 3u, label, sizeof label, &named);
    CHECK(named && strcmp(label, "Am/E") == 0);

    /* A major third alone belongs to five different chords, so it is not
     * given any of their names. */
    kpa_ui_internal_chord_label(third, 2u, label, sizeof label, &named);
    CHECK(!named);
    CHECK(strcmp(label, "E G#") == 0);
    /* Three pitch classes that match no shape: the notes, and nothing more. */
    kpa_ui_internal_chord_label(nameless, 3u, label, sizeof label, &named);
    CHECK(!named);
    CHECK(strcmp(label, "C# E F#") == 0);

    /* Nothing sounding says nothing at all. */
    (void)snprintf(label, sizeof label, "stale");
    kpa_ui_internal_chord_label(NULL, 0u, label, sizeof label, &named);
    CHECK(!named && label[0] == '\0');
    kpa_ui_internal_chord_label(e5, 0u, label, sizeof label, &named);
    CHECK(!named && label[0] == '\0');
    kpa_ui_internal_chord_label(e5, 3u, NULL, 0u, &named);
    return true;
}

/*
 * The lowest string of a note, as a y range, found by composing the same
 * model with that note moved to another string.  The two canvases differ
 * wherever either string was drawn, so the bottom of the difference is the
 * lower of the two rows - which is all this needs to know.
 */
static bool row_span(kpa_ui_model *model, tab_builder *tab, uint8_t string,
                     uint8_t against, int width, int height, box *out)
{
    sr_canvas first;
    sr_canvas second;
    const double when = model->position;

    tab->tab.event_count = 0u;
    tab->tab.position_count = 0u;
    tab_note(tab, when - 0.2, when + 0.6, string, 0u);
    if (!sr_canvas_init(&first, width, height)) return false;
    kpa_ui_compose(&first, model);

    tab->tab.event_count = 0u;
    tab->tab.position_count = 0u;
    tab_note(tab, when - 0.2, when + 0.6, against, 0u);
    if (!sr_canvas_init(&second, width, height)) {
        sr_canvas_free(&first);
        return false;
    }
    kpa_ui_compose(&second, model);
    *out = canvas_difference(&first, &second);
    sr_canvas_free(&first);
    sr_canvas_free(&second);
    return true;
}

/*
 * One key, both pictures.
 *
 * Tablature puts the high e on top; a player looking down at the instrument
 * sees the low E nearest them.  Both are real, so it is a preference - and
 * the point of the preference is that it moves the neck AND the tab lane,
 * because two pictures of one guitar disagreeing about string order on one
 * screen is worse than either convention.
 */
static bool test_string_order_moves_both(void)
{
    kpa_ui_model *model = model_new();
    tab_builder tab;
    box low;
    box high;
    bool ok = true;
    int surface;

    CHECK(model != NULL);
    if (!tab_start(&tab, KPA_STRING_COUNT, 8u)) {
        free(model);
        return false;
    }
    model_tracks(model);
    model->tab = &tab.tab;
    model->lyrics_visible = false;

    for (surface = 0; ok && surface < 2; ++surface) {
        const char *what = surface == 0 ? "the neck" : "the tab lane";
        int flip;

        /* The neck alone, then the lane alone: one key has to move both. */
        model->fretboard = surface == 0 ? KPA_FB_NECK : KPA_FB_OFF;
        model->tab_visible = surface != 0;
        model->low_string_on_top = false;

        for (flip = 0; ok && flip < 2; ++flip) {
            if (!row_span(model, &tab, 0u, 2u, 1280, 900, &low) ||
                !row_span(model, &tab, 5u, 2u, 1280, 900, &high) ||
                !low.found || !high.found) {
                (void)fprintf(stderr, "%s drew nothing\n", what);
                ok = false;
                break;
            }
            if (model->low_string_on_top == (low.y1 > high.y1)) {
                (void)fprintf(stderr,
                              "%s: low E reaches y %d and high e y %d with "
                              "low_string_on_top %d\n", what, low.y1, high.y1,
                              model->low_string_on_top ? 1 : 0);
                ok = false;
                break;
            }
            /* And the key is what does it. */
            (void)kpa_ui_internal_apply_key(model, 'o', 0u);
            if (model->low_string_on_top != (flip == 0)) {
                (void)fprintf(stderr, "o did not swap the string order\n");
                ok = false;
            }
        }
    }
    tab_release(&tab);
    free(model);
    return ok;
}

/* Does this rectangle of the canvas spell `text`?  Spaces are skipped: the
 * font's space glyph has no set bits, so there is nothing to match. */
static bool text_at(const sr_canvas *canvas, const char *text, int x, int y)
{
    int index;

    for (index = 0; text[index] != '\0'; ++index) {
        const uint8_t *rows;

        if (text[index] == ' ') continue;
        rows = sr_font_glyph_in(SR_FONT_FIXED_8X16,
                                (unsigned char)text[index]);
        if (rows == NULL) return false;
        if (x + 8 * index + 8 > canvas->w) return false;
        if (!glyph_at(canvas, rows, x + 8 * index, y)) return false;
    }
    return true;
}

static bool find_text(const sr_canvas *canvas, const char *text, int y0,
                      int y1)
{
    int y;

    if (y0 < 0) y0 = 0;
    if (y1 > canvas->h) y1 = canvas->h;
    for (y = y0; y + 16 <= y1; ++y) {
        int x;

        for (x = 0; x + 8 <= canvas->w; ++x) {
            if (text_at(canvas, text, x, y)) return true;
        }
    }
    return false;
}

/*
 * Where the hand is, read off the callout.  The rule itself - a five-fret
 * box placed by the median of what is played over a window that reaches
 * further forward than back - is kpa_fret.c's; what is asserted here is that
 * the screen says it, that one far-off sustain does not drag the box up the
 * neck after it, and that a passage of open strings is called open rather
 * than called first position.
 */
static bool test_hand_callout(void)
{
    const double when = 61.25;
    kpa_ui_model *model = model_new();
    tab_builder tab;
    sr_canvas canvas;
    bool ok = true;

    CHECK(model != NULL);
    if (!tab_start(&tab, KPA_STRING_COUNT, 16u)) {
        free(model);
        return false;
    }
    model_tracks(model);
    model->tab = &tab.tab;
    model->position = when;
    model->lyrics_visible = false;
    model->tab_visible = false;
    if (!sr_canvas_init(&canvas, 1280, 720)) {
        tab_release(&tab);
        free(model);
        return false;
    }
    /* A twelfth-fret sustain ringing under a run down at the second. */
    tab_note(&tab, when - 0.4, when + 3.0, 0u, 12u);
    tab_note(&tab, when - 0.2, when + 0.4, 3u, 2u);
    tab_note(&tab, when - 0.1, when + 0.4, 4u, 3u);
    tab_note(&tab, when + 0.1, when + 0.5, 5u, 2u);
    kpa_ui_compose(&canvas, model);
    if (!find_text(&canvas, "pos", 0, canvas.h)) {
        (void)fprintf(stderr, "the callout does not say where the hand is\n");
        ok = false;
    }
    if (find_text(&canvas, "pos 10", 0, canvas.h)) {
        (void)fprintf(stderr,
                      "one sustain at the twelfth fret dragged the hand up "
                      "the neck\n");
        ok = false;
    }

    /* Nothing but open strings is not first position; it is no position. */
    tab.tab.event_count = 0u;
    tab.tab.position_count = 0u;
    tab_note(&tab, when - 0.2, when + 0.6, 0u, 0u);
    tab_note(&tab, when - 0.2, when + 0.6, 5u, 0u);
    kpa_ui_compose(&canvas, model);
    if (!find_text(&canvas, "open", 0, canvas.h)) {
        (void)fprintf(stderr, "a window of open strings is not called open\n");
        ok = false;
    }
    if (find_text(&canvas, "pos", 0, canvas.h)) {
        (void)fprintf(stderr, "open strings were given a hand position\n");
        ok = false;
    }
    sr_canvas_free(&canvas);
    tab_release(&tab);
    free(model);
    return ok;
}

/*
 * A capo does not move a note; it makes some notes unplayable.  The surface
 * says which, and never redraws them at a fret the transcription did not
 * have.
 */
static bool test_capo(void)
{
    const double when = 61.25;
    kpa_ui_model *model = model_new();
    tab_builder tab;
    sr_canvas without;
    sr_canvas with;
    bool ok = true;

    CHECK(model != NULL);
    if (!tab_start(&tab, KPA_STRING_COUNT, 8u)) {
        free(model);
        return false;
    }
    model_tracks(model);
    model->tab = &tab.tab;
    model->position = when;
    model->lyrics_visible = false;
    model->tab_visible = false;
    tab_note(&tab, when - 0.2, when + 0.6, 0u, 1u);
    tab_note(&tab, when - 0.2, when + 0.6, 4u, 5u);

    if (!sr_canvas_init(&without, 1024, 640)) {
        tab_release(&tab);
        free(model);
        return false;
    }
    if (!sr_canvas_init(&with, 1024, 640)) {
        sr_canvas_free(&without);
        tab_release(&tab);
        free(model);
        return false;
    }
    kpa_ui_compose(&without, model);
    CHECK(model->capo == 0u);
    (void)kpa_ui_internal_apply_key(model, 'k', 0u);
    (void)kpa_ui_internal_apply_key(model, 'k', 0u);
    (void)kpa_ui_internal_apply_key(model, 'k', 0u);
    if (model->capo != 3u) {
        (void)fprintf(stderr, "k did not raise the capo to 3\n");
        ok = false;
    }
    if (model->notice[0] == '\0') {
        (void)fprintf(stderr,
                      "a capo over a note this part plays was not mentioned\n");
        ok = false;
    }
    kpa_ui_compose(&with, model);
    if (canvas_equal(&without, &with)) {
        (void)fprintf(stderr, "the capo changed nothing on the neck\n");
        ok = false;
    }
    /* K is the same key the other way, and the capo wraps rather than
     * running away. */
    (void)kpa_ui_internal_apply_key(model, 'k', KITTYKB_MOD_SHIFT);
    ok = ok && model->capo == 2u;
    (void)kpa_ui_internal_apply_key(model, 'k', KITTYKB_MOD_SHIFT);
    (void)kpa_ui_internal_apply_key(model, 'k', KITTYKB_MOD_SHIFT);
    ok = ok && model->capo == 0u;
    (void)kpa_ui_internal_apply_key(model, 'k', KITTYKB_MOD_SHIFT);
    ok = ok && model->capo == 7u;
    sr_canvas_free(&without);
    sr_canvas_free(&with);
    tab_release(&tab);
    free(model);
    return ok;
}

/*
 * The keys the fretboard added.  Two properties across all of them: a
 * shifted letter is the same family as its unshifted one, and none of them
 * touches what is audible.
 */
static bool test_fretboard_keys(void)
{
    kpa_ui_model *model = model_new();
    lyric_builder lyrics;
    bool ok = true;

    CHECK(model != NULL);
    if (!lyrics_start(&lyrics)) {
        free(model);
        return false;
    }
    model_tracks(model);

    /* f walks the fretboard down and round; F is the same family - the
     * other axis of the same thing - and never something unrelated. */
    CHECK(model->fretboard == KPA_FB_NECK_AND_RAMP);
    (void)kpa_ui_internal_apply_key(model, 'f', 0u);
    ok = ok && model->fretboard == KPA_FB_NECK;
    (void)kpa_ui_internal_apply_key(model, 'f', 0u);
    ok = ok && model->fretboard == KPA_FB_OFF;
    (void)kpa_ui_internal_apply_key(model, 'f', 0u);
    ok = ok && model->fretboard == KPA_FB_NECK_AND_RAMP;
    /* Zero reads as the two-second default, so the first F is 3.0. */
    (void)kpa_ui_internal_apply_key(model, 'F', KITTYKB_MOD_SHIFT);
    ok = ok && model->ramp_seconds > 2.99f && model->ramp_seconds < 3.01f;
    (void)kpa_ui_internal_apply_key(model, 'F', KITTYKB_MOD_SHIFT);
    ok = ok && model->ramp_seconds > 3.99f && model->ramp_seconds < 4.01f;
    (void)kpa_ui_internal_apply_key(model, 'F', KITTYKB_MOD_SHIFT);
    ok = ok && model->ramp_seconds > 0.99f && model->ramp_seconds < 1.01f;
    ok = ok && model->fretboard == KPA_FB_NECK_AND_RAMP;
    if (!ok) (void)fprintf(stderr, "f and F\n");

    (void)kpa_ui_internal_apply_key(model, 'n', 0u);
    ok = ok && model->note_label == KPA_LABEL_NOTE;
    (void)kpa_ui_internal_apply_key(model, 'n', 0u);
    ok = ok && model->note_label == KPA_LABEL_BOTH;
    (void)kpa_ui_internal_apply_key(model, 'n', 0u);
    ok = ok && model->note_label == KPA_LABEL_FRET;

    (void)kpa_ui_internal_apply_key(model, 'w', 0u);
    ok = ok && model->overview == KPA_OVERVIEW_DENSITY;
    (void)kpa_ui_internal_apply_key(model, 'w', 0u);
    ok = ok && model->overview == KPA_OVERVIEW_PLAIN;

    /* The lead-in is armed and sized by the same letter. */
    (void)kpa_ui_internal_apply_key(model, 'c', 0u);
    ok = ok && model->lead_in == 2u;
    (void)kpa_ui_internal_apply_key(model, 'C', KITTYKB_MOD_SHIFT);
    ok = ok && model->lead_in == 4u;
    model->position = 60.0;
    model->playing = false;
    (void)kpa_ui_internal_apply_key(model, ' ', 0u);
    ok = ok && model->playing && model->position == 56.0;
    if (!ok) (void)fprintf(stderr, "the lead-in did not rewind\n");
    /* Stopping does not rewind again, and a loop is the floor. */
    (void)kpa_ui_internal_apply_key(model, ' ', 0u);
    ok = ok && !model->playing && model->position == 56.0;
    model->loop_active = true;
    model->loop_start = 55.0;
    model->loop_end = 70.0;
    (void)kpa_ui_internal_apply_key(model, ' ', 0u);
    ok = ok && model->position == 55.0;
    (void)kpa_ui_internal_apply_key(model, 'c', 0u);
    ok = ok && model->lead_in == 0u;

    /* a and A jump to the loop's ends while there is a loop. */
    model->position = 61.0;
    (void)kpa_ui_internal_apply_key(model, 'a', 0u);
    ok = ok && model->position == 55.0;
    (void)kpa_ui_internal_apply_key(model, 'a', KITTYKB_MOD_SHIFT);
    ok = ok && model->position == 70.0;
    /* { } widen the loop, each at its own end. */
    (void)kpa_ui_internal_apply_key(model, '{', 0u);
    ok = ok && model->loop_start > 54.89 && model->loop_start < 54.91;
    (void)kpa_ui_internal_apply_key(model, '}', 0u);
    ok = ok && model->loop_end > 70.09 && model->loop_end < 70.11;
    if (!ok) (void)fprintf(stderr, "loop jumps and widening\n");

    /* With no loop, the same key walks the lyric cues. */
    (void)kpa_ui_internal_apply_key(model, KITTYKB_KEY_BACKSPACE, 0u);
    model->lyrics = &lyrics.lyrics;
    model->position = 63.5;
    (void)kpa_ui_internal_apply_key(model, 'a', 0u);
    ok = ok && model->position == 62.0;
    (void)kpa_ui_internal_apply_key(model, 'a', KITTYKB_MOD_SHIFT);
    ok = ok && model->position == 64.0;
    if (!ok) (void)fprintf(stderr, "cue jumps\n");

    /* The speed ramp sets the rate it starts at; R puts it back. */
    (void)kpa_ui_internal_apply_key(model, 'r', 0u);
    ok = ok && model->speed_ramp == 70u && model->rate > 0.699 &&
         model->rate < 0.701;
    (void)kpa_ui_internal_apply_key(model, 'r', 0u);
    ok = ok && model->speed_ramp == 80u;
    (void)kpa_ui_internal_apply_key(model, 'R', KITTYKB_MOD_SHIFT);
    ok = ok && model->speed_ramp == 0u && model->rate == 1.0;
    /* Without an engine it says so and moves nothing, which is the path the
     * comma and full stop keys already take. */
    model->rate_available = false;
    model->rate = 1.0;
    (void)kpa_ui_internal_apply_key(model, 'r', 0u);
    ok = ok && model->speed_ramp == 0u && model->rate == 1.0 &&
         model->notice[0] != '\0';
    if (!ok) (void)fprintf(stderr, "the speed ramp\n");

    /*
     * With no pixel layer there are not rows for both pictures of the
     * guitar, so the request that just arrived takes the other one's rows
     * and says so.  Only the press that turns a layer ON does that: `f`
     * cycling its own ramp off is not a request for the neck, and a key
     * that quietly switched something else off every time it was pressed
     * would be worse than the crowding.
     */
    model->cell_only = true;
    model->fretboard = KPA_FB_NECK_AND_RAMP;
    model->tab_visible = true;
    (void)kpa_ui_internal_apply_key(model, 'f', 0u);      /* -> neck */
    ok = ok && model->tab_visible && model->notice[0] == '\0';
    (void)kpa_ui_internal_apply_key(model, 'f', 0u);      /* -> off */
    ok = ok && model->tab_visible && model->notice[0] == '\0';
    (void)kpa_ui_internal_apply_key(model, 'f', 0u);      /* -> back on */
    ok = ok && !model->tab_visible && model->notice[0] != '\0';
    (void)kpa_ui_internal_apply_key(model, 't', 0u);      /* the lane back */
    ok = ok && model->tab_visible && model->fretboard == KPA_FB_OFF &&
         model->notice[0] != '\0';
    if (!ok) (void)fprintf(stderr, "the cell-only neck and lane\n");
    /* With a pixel layer they coexist and neither key touches the other. */
    model->cell_only = false;
    model->fretboard = KPA_FB_NECK_AND_RAMP;
    (void)kpa_ui_internal_apply_key(model, 't', 0u);
    (void)kpa_ui_internal_apply_key(model, 't', 0u);
    ok = ok && model->tab_visible &&
         model->fretboard == KPA_FB_NECK_AND_RAMP;

    /* And none of it was ever about the audio. */
    ok = ok && !model->tracks[0].muted && !model->tracks[3].muted &&
         model->tracks[3].gain == 1.0f;
    lyrics_release(&lyrics);
    free(model);
    return ok;
}

/* --------------------------------------------------------------- still */

static bool test_render_ppm(void)
{
    const char *path = getenv("KPA_UI_PPM");
    kpa_ui_model *model = model_new();
    tab_builder tab;
    lyric_builder lyrics;
    FILE *file;
    long size = 0L;
    bool ok;

    CHECK(model != NULL);
    if (!tab_start(&tab, KPA_STRING_COUNT, 64u) || !lyrics_start(&lyrics)) {
        free(model);
        return false;
    }
    if (path == NULL) {
        /* Under build/, which .gitignore already covers, so running the
         * suite from the repository root does not leave an untracked image
         * behind.  KPA_UI_PPM overrides it for a release-gate run. */
        (void)mkdir("build", 0700);
        path = "build/kpa-ui-surface.ppm";
    }
    /*
     * A picture of an empty player proves only that the program starts, so
     * this one is a minute into a song with a loop set and a stem muted -
     * and with a chord under the hand rather than a scattering of single
     * notes, because a fretboard with nothing shaped on it is another kind
     * of empty player.  The shape is the open A minor: 0 2 2 1 0 from the A
     * string up, which the callout has to name.
     */
    {
        static const uint8_t chord_strings[5] = {1u, 2u, 3u, 4u, 5u};
        static const uint8_t chord_frets[5] = {0u, 2u, 2u, 1u, 0u};
        static const uint8_t next_strings[3] = {2u, 3u, 4u};
        static const uint8_t next_frets[3] = {10u, 9u, 8u};

        tab_note(&tab, 60.8, 61.1, 0u, 3u);
        tab_chord(&tab, 61.0, 61.9, chord_strings, chord_frets, 5u);
        tab_note(&tab, 61.6, 62.1, 5u, 3u);
        /* Far enough up the neck to be a hand move, so the callout's third
         * field has something to say. */
        tab_chord(&tab, 62.4, 63.0, next_strings, next_frets, 3u);
    }
    model_tracks(model);
    model->tab = &tab.tab;
    model->lyrics = &lyrics.lyrics;
    model->active_cue = 0;
    model->active_word = 1;
    model->tracks[0].muted = true;
    model->tracks[3].gain = 1.4f;
    model->loop_active = true;
    model->loop_start = 55.0;
    model->loop_end = 78.0;
    ok = kpa_ui_render_ppm(model, 1400, 900, path) == 0;
    if (ok) {
        file = fopen(path, "rb");
        ok = file != NULL;
        if (ok) {
            ok = fseek(file, 0L, SEEK_END) == 0;
            size = ftell(file);
            (void)fclose(file);
        }
    }
    if (ok) {
        (void)printf("     %s, %ld bytes, 1400x900 at %.2fs\n", path, size,
                     model->position);
    }
    /* The guards: a bad size or a path that cannot be written is a failure
     * that is reported, not a crash. */
    ok = ok && kpa_ui_render_ppm(model, 0, 900, path) != 0;
    ok = ok && kpa_ui_render_ppm(model, 1400, -1, path) != 0;
    ok = ok && kpa_ui_render_ppm(NULL, 100, 100, path) != 0;
    ok = ok && kpa_ui_render_ppm(model, 100, 100, NULL) != 0;
    ok = ok && kpa_ui_render_ppm(model, 100, 100,
                                 "/nonexistent-directory/x.ppm") != 0;
    tab_release(&tab);
    lyrics_release(&lyrics);
    free(model);
    if (!ok) (void)fprintf(stderr, "render_ppm\n");
    return ok;
}

/* --------------------------------------------------- lyric provenance */

/*
 * The caption for a document of this provenance, and the ink it is drawn in.
 * The cues under it are the fixture's real ones: the caption is a function of
 * the provenance alone, and building a whole document around it is what keeps
 * these assertions about a lyric band rather than about a zeroed struct.
 */
static bool caption_of(kpa_lyrics_timing timing,
                       const kpa_lyrics_alignment *report, char *out,
                       size_t size, uint32_t *rgb, bool *out_said)
{
    lyric_builder builder;

    if (!lyrics_start(&builder)) return false;
    builder.lyrics.timing = timing;
    if (report != NULL) builder.lyrics.alignment = *report;
    *out_said = kpa_ui_internal_lyrics_caption(&builder.lyrics, out, size,
                                               rgb);
    lyrics_release(&builder);
    return true;
}

/*
 * No colour is named here - the palette is kpa_ui.c's - so what is asserted
 * is which cases share an ink: a guess and an alignment its own producer
 * called poor are drawn alike, and neither is drawn like times that came with
 * the source.
 */
static bool test_lyric_caption(void)
{
    kpa_lyrics_alignment report;
    char line[256];
    uint32_t unknown_rgb = 0u;
    uint32_t authored_rgb = 0u;
    uint32_t measured_rgb = 0u;
    uint32_t poor_rgb = 0u;
    uint32_t estimated_rgb = 0u;
    bool said = false;

    (void)memset(&report, 0, sizeof report);

    /* A document from before the field says nothing, and a band that says
     * nothing is the band exactly as it was: no caption, no row spent. */
    CHECK(caption_of(KPA_TIMING_UNKNOWN, NULL, line, sizeof line,
                     &unknown_rgb, &said));
    CHECK(!said && line[0] == '\0');

    CHECK(caption_of(KPA_TIMING_AUTHORED, NULL, line, sizeof line,
                     &authored_rgb, &said));
    CHECK(said && strstr(line, "authored") != NULL);

    /* The case a player must see: nothing here was heard. */
    CHECK(caption_of(KPA_TIMING_ESTIMATED, NULL, line, sizeof line,
                     &estimated_rgb, &said));
    CHECK(said && strstr(line, "estimated") != NULL);
    CHECK(strstr(line, "not measured") != NULL);
    CHECK(estimated_rgb != authored_rgb);

    /* Measured, with the document's own numbers and no others. */
    report.present = true;
    report.matched_fraction = 0.94;
    report.interpolated_words = 3u;
    report.mean_displacement = 0.125;
    report.usable = true;
    CHECK(caption_of(KPA_TIMING_MEASURED, &report, line, sizeof line,
                     &measured_rgb, &said));
    CHECK(said && strstr(line, "measured") != NULL);
    CHECK(strstr(line, "94%") != NULL);
    CHECK(strstr(line, "3 filled in") != NULL);
    CHECK(measured_rgb == authored_rgb);

    /* A hundredth that the arithmetic lands just short of still reads as
     * that hundredth: 0.29 * 100.0 is 28.999999999999996. */
    report.matched_fraction = 0.29;
    CHECK(caption_of(KPA_TIMING_MEASURED, &report, line, sizeof line,
                     &measured_rgb, &said));
    CHECK(said && strstr(line, "29%") != NULL);

    /* Floored, never rounded up: 99.6% of the words is not all of them, and
     * "100% matched, 2 filled in" would contradict itself. */
    report.matched_fraction = 0.996;
    report.interpolated_words = 2u;
    CHECK(caption_of(KPA_TIMING_MEASURED, &report, line, sizeof line,
                     &measured_rgb, &said));
    CHECK(said && strstr(line, "99%") != NULL);
    CHECK(strstr(line, "100%") == NULL);

    /* An alignment its own producer called poor is drawn like a guess. */
    report.usable = false;
    CHECK(caption_of(KPA_TIMING_MEASURED, &report, line, sizeof line,
                     &poor_rgb, &said));
    CHECK(said && strstr(line, "poor") != NULL);
    CHECK(poor_rgb == estimated_rgb);

    /* Measured with no report invents no percentage to fill the gap. */
    (void)memset(&report, 0, sizeof report);
    CHECK(caption_of(KPA_TIMING_MEASURED, &report, line, sizeof line,
                     &measured_rgb, &said));
    CHECK(said && strstr(line, "measured") != NULL);
    CHECK(strchr(line, '%') == NULL);
    CHECK(strstr(line, "filled in") == NULL);

    /* No lyrics at all is no caption, and the line is cleared before the
     * question is even asked. */
    (void)snprintf(line, sizeof line, "stale");
    CHECK(!kpa_ui_internal_lyrics_caption(NULL, line, sizeof line, NULL));
    CHECK(line[0] == '\0');

    /* A buffer far too small for any of these captions.  It is on the heap
     * so the sanitizer, not this file, is the judge of what was written. */
    {
        char *tight = malloc(6u);
        bool fits;

        CHECK(tight != NULL);
        fits = caption_of(KPA_TIMING_ESTIMATED, NULL, tight, 6u, NULL,
                          &said) && said && strlen(tight) == 5u;
        free(tight);
        CHECK(fits);
    }
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
        {"compose is a pure function of the model", test_compose_is_pure},
        {"compose leaves the rows the overlay owns clear",
         test_layout_matches_compose},
        {"cell_only draws no pixels at all", test_cell_only_draws_no_pixels},
        {"the tab lane runs high string to low",
         test_tab_lane_is_high_to_low},
        {"the string numbers shown are the player's",
         test_string_numbers_are_the_players},
        {"the string gutter is pinned as the lane rolls",
         test_gutter_is_pinned},
        {"every view composes at every size", test_every_view_at_every_size},
        {"transport keys", test_transport_keys},
        {"loop keys", test_loop_keys},
        {"stem selection, mute, solo and gain", test_stem_keys},
        {"audio state and display state are independent",
         test_audio_and_display_are_independent},
        {"rate keys, present and absent", test_rate_keys},
        {"views, escape and quit", test_views_and_escape},
        {"the neck's frets crowd together the way a guitar's do",
         test_neck_is_a_neck},
        {"one note per string, latest onset wins",
         test_one_note_per_string},
        {"a note is drawn differently as it decays", test_note_envelope},
        {"what the callout calls a set of pitches", test_chord_labels},
        {"one key swaps the string order on the neck and in the lane",
         test_string_order_moves_both},
        {"the callout says where the hand is", test_hand_callout},
        {"a capo marks what it makes unplayable", test_capo},
        {"the fretboard keys", test_fretboard_keys},
        {"a still of a populated surface", test_render_ppm},
        {"the lyrics band says where its times came from",
         test_lyric_caption}
    };
    const size_t count = sizeof tests / sizeof tests[0];
    size_t passed = 0u;
    size_t index;

    for (index = 0u; index < count; ++index) {
        if (tests[index].run()) {
            ++passed;
            (void)printf("ok   %s\n", tests[index].name);
        } else {
            (void)printf("FAIL %s\n", tests[index].name);
        }
    }
    (void)printf("kpa-ui: %zu/%zu groups passed\n", passed, count);
    return passed == count ? EXIT_SUCCESS : EXIT_FAILURE;
}
