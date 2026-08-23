/*
 * kilix-playalong-native - the way into the native surface.
 *
 * This file owns three things and deliberately nothing else: which view to
 * open, what the process exits with, and what a person or a script is told
 * when a view will not open.  Pixels belong to kpa_ui.c, projects to
 * kpa_project.c, sound to kpa_audio.c.
 *
 * The exit codes are the contract - 0 ok, 2 invalid input, 3 project not
 * found, 4 no audio device, 5 no usable terminal - and kpa_ui_run reports
 * only whether it ended cleanly.  So each condition those codes name is
 * checked here, before the surface starts, rather than guessed afterwards
 * from a return value that cannot carry it.  The device check is literally
 * the function --doctor reports, so the doctor cannot disagree with the
 * program about the sound card.  The terminal check is narrower than the
 * doctor's on purpose: the surface falls back to cells when a terminal has
 * no graphics path, so only the absence of a terminal is fatal.
 *
 * Errors on stderr are bare on purpose.  The fleet rule is bounded public
 * errors - no path, no URL, no capability, no lyric text - because a failure
 * message is the part of a program most likely to end up in a bug report, a
 * CI log or a screenshot.  --doctor prints the store's path because someone
 * asked it to; a failure never does.
 *
 * Nothing here writes to a project.  --render reads one and draws a picture
 * of it, the practice view reads one and plays it, and neither creates a
 * store that is not already there: the pipeline owns those bytes.
 */

#define SDL_MAIN_HANDLED

#include "kilix_playalong/kpa_ui.h"

#include "kilix_playalong/kpa_audio.h"
#include "kilix_playalong/kpa_cells.h"
#include "kilix_playalong/kpa_project.h"

#include "kitty_framebuffer.h"

#include <SDL2/SDL.h>
#include <sndfile.h>

#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define PROGRAM "kilix-playalong-native"
/* Tracks pyproject.toml and kilix_playalong/__init__.py.  One program in two
 * languages should not answer --version two ways. */
#define PROGRAM_VERSION "0.1.0"

#define KPA_EXIT_OK 0
#define KPA_EXIT_FAILED 1
#define KPA_EXIT_USAGE 2
#define KPA_EXIT_NO_PROJECT 3
#define KPA_EXIT_NO_AUDIO 4
#define KPA_EXIT_NO_TERMINAL 5

/* Copied from the reader's own listing bound rather than shared with it:
 * KPA_MAX_LIST_ENTRIES is private to kpa_project.c.  A store holding more
 * than this lists the first of them, and --list says on stderr that it
 * stopped there, so the two drifting apart is visible rather than silent. */
#define LIST_CAPACITY 4096u
#define DIRECTORY_CAPACITY 4096u
#define RENDER_WIDTH 1280
#define RENDER_HEIGHT 720
/* Long enough for a terminal on a slow link to answer, short enough that one
 * which will never answer does not hold up a report. */
#define PROBE_TIMEOUT_MS 500

static void usage(FILE *stream)
{
    (void)fprintf(
        stream,
        "usage: " PROGRAM " [<project-id>]\n"
        "       " PROGRAM " --list\n"
        "       " PROGRAM " --doctor [--json]\n"
        "       " PROGRAM " --render <project-id> --out <path.ppm>\n"
        "       " PROGRAM " --help | --version\n"
        "\n"
        "  (no argument)  the library: every project on this machine\n"
        "  <project-id>   the practice view for one project\n"
        "  --list         one project per line, tab separated, no terminal\n"
        "                 needed: id, ready|pending, seconds, tracks,\n"
        "                 lyrics, tab, title, artist\n"
        "  --doctor       what this build can do here, as text or as one\n"
        "                 JSON object.  It exits 0 whenever it produced a\n"
        "                 report, so read the fields for the verdict\n"
        "  --render       draw the practice view once, headless, to a\n"
        "                 binary PPM 1280x720, a quarter of the way in and\n"
        "                 snapped onto a live cue when there are lyrics\n"
        "\n"
        "exit: 0 ok, 1 unexpected failure, 2 invalid input, 3 no such\n"
        "      project, 4 no audio device, 5 no usable terminal\n"
        "\n"
        "Projects are read from the private store --doctor names.  This\n"
        "command never writes one.\n");
}

static void fail(const char *message)
{
    (void)fprintf(stderr, "%s: %s\n", PROGRAM, message);
}

/*
 * One place decides what a reader result means to a caller, so "no such
 * project" cannot be exit 3 on one path and exit 1 on another.  A result
 * name is bounded and says nothing about where the project is or what is in
 * it, which is what makes it safe to print.
 */
static int project_exit(kpa_result result)
{
    if (result == KPA_NOT_FOUND) {
        fail("no such project");
        return KPA_EXIT_NO_PROJECT;
    }
    if (result == KPA_INVALID_ARGUMENT) {
        fail("not a project id");
        return KPA_EXIT_USAGE;
    }
    (void)fprintf(stderr, "%s: cannot open the project (%s)\n", PROGRAM,
                  kpa_result_name(result));
    return KPA_EXIT_FAILED;
}

/* ---------------------------------------------------------------- listing */

/*
 * A title or an artist is whatever the pipeline decoded from the source, and
 * a control byte in one would put a second record on a line a script reads
 * as one.  Column widths are the reader's problem; line structure is ours.
 */
static void print_field(const char *text)
{
    const unsigned char *byte = (const unsigned char *)text;

    for (; *byte != '\0'; byte++) {
        (void)fputc(*byte < 0x20 || *byte == 0x7F ? ' ' : (int)*byte, stdout);
    }
}

static void print_summary(const kpa_project_summary *summary)
{
    (void)printf("%s\t%s\t%.1f\t%u\t%s\t%s\t", summary->id,
                 summary->ready ? "ready" : "pending", summary->duration,
                 summary->track_count, summary->has_lyrics ? "lyrics" : "-",
                 summary->has_tab ? "tab" : "-");
    print_field(summary->title);
    (void)fputc('\t', stdout);
    print_field(summary->artist);
    (void)fputc('\n', stdout);
}

static int command_list(void)
{
    kpa_project_summary *summaries;
    uint32_t count = 0u;
    uint32_t index;
    kpa_result result;

    summaries = calloc(LIST_CAPACITY, sizeof *summaries);
    if (summaries == NULL) {
        fail("out of memory");
        return KPA_EXIT_FAILED;
    }
    result = kpa_project_list(summaries, LIST_CAPACITY, &count);
    /* A store nothing has written yet is an empty library, not a failure,
     * and this command will not create one to prove it. */
    if (result != KPA_OK && result != KPA_NOT_FOUND) {
        free(summaries);
        (void)fprintf(stderr, "%s: cannot read the project store (%s)\n",
                      PROGRAM, kpa_result_name(result));
        return KPA_EXIT_FAILED;
    }
    for (index = 0u; index < count; index++) {
        print_summary(&summaries[index]);
    }
    if (count == LIST_CAPACITY) {
        (void)fprintf(stderr, "%s: the listing stops at %u projects\n",
                      PROGRAM, (unsigned)LIST_CAPACITY);
    }
    free(summaries);
    return KPA_EXIT_OK;
}

/* ---------------------------------------------------------------- probing */

typedef enum probe_state {
    PROBE_NO = 0,
    PROBE_YES = 1,
    PROBE_UNKNOWN = 2
} probe_state;

static const char *probe_word(probe_state state)
{
    switch (state) {
    case PROBE_YES:
        return "yes";
    case PROBE_NO:
        return "no";
    case PROBE_UNKNOWN:
        break;
    }
    return "unknown";
}

static bool audio_device_opens(void)
{
    SDL_AudioSpec desired;
    SDL_AudioSpec obtained;
    SDL_AudioDeviceID device;

    SDL_SetMainReady();
    if (SDL_InitSubSystem(SDL_INIT_AUDIO) != 0) return false;
    (void)memset(&desired, 0, sizeof desired);
    (void)memset(&obtained, 0, sizeof obtained);
    desired.freq = 48000;
    desired.format = AUDIO_F32SYS;
    desired.channels = 2;
    desired.samples = 1024;
    /*
     * No callback, so this is the queue API: the device opens paused and is
     * closed before anything could be heard.  Any change is allowed because
     * the question is whether a device opens at all, not whether it opens at
     * one project's rate - kpa_audio asks that one, strictly, when it has a
     * stem in front of it.
     */
    desired.callback = NULL;
    device = SDL_OpenAudioDevice(NULL, 0, &desired, &obtained,
                                 SDL_AUDIO_ALLOW_ANY_CHANGE);
    if (device != 0u) SDL_CloseAudioDevice(device);
    SDL_QuitSubSystem(SDL_INIT_AUDIO);
    return device != 0u;
}

/*
 * Ask the terminal rather than guess from $TERM.  Sets *detail to a bounded
 * sentence either way, because "no" without a reason sends people looking at
 * the wrong half of the problem.
 */
static probe_state probe_graphics(const char **detail)
{
    kittyfb_options options;
    kittyfb_session *session;
    probe_state state;

    if (!isatty(STDIN_FILENO) || !isatty(STDOUT_FILENO)) {
        *detail = "stdin and stdout are not both terminals";
        return PROBE_NO;
    }
    /*
     * kittyfb_start skips its probe when this is set, and then succeeds
     * without evidence.  Reporting "yes" from that would be a claim nothing
     * measured, so the report says nothing was measured.
     */
    if (getenv("KITTYFB_SKIP_PROBE") != NULL) {
        *detail = "KITTYFB_SKIP_PROBE is set, so nothing was measured";
        return PROBE_UNKNOWN;
    }
    session = calloc(1u, sizeof *session);
    if (session == NULL) {
        *detail = "out of memory";
        return PROBE_UNKNOWN;
    }
    kittyfb_options_init(&options);
    options.probe_graphics = true;
    /*
     * The alternate screen stays managed even though nothing is drawn: a
     * successful start clears the screen it is on, and a report is not worth
     * anyone's scrollback.  A terminal without graphics fails before that
     * write and sees only the query.  The winch handler is off because a
     * probe this short has no use for a resize, and a report has no business
     * leaving a signal handler behind it.
     */
    options.install_winch_handler = false;
    options.probe_timeout_ms = PROBE_TIMEOUT_MS;
    kittyfb_session_init(session);
    if (kittyfb_start(session, STDIN_FILENO, STDOUT_FILENO, &options) == 0) {
        kittyfb_stop(session);
        *detail = "the terminal answered the graphics query";
        state = PROBE_YES;
    } else if (errno == ENOTSUP) {
        *detail = "the terminal did not answer the graphics query";
        state = PROBE_NO;
    } else {
        *detail = "the graphics probe could not run";
        state = PROBE_NO;
    }
    free(session);
    return state;
}

static void sdl_versions(char *linked, size_t linked_size, char *compiled,
                         size_t compiled_size)
{
    SDL_version at_build;
    SDL_version at_run;

    SDL_VERSION(&at_build);
    SDL_GetVersion(&at_run);
    /* Both, because a linked SDL that is not the one this was built against
     * is exactly the sort of thing a doctor exists to catch. */
    (void)snprintf(linked, linked_size, "%u.%u.%u", (unsigned)at_run.major,
                   (unsigned)at_run.minor, (unsigned)at_run.patch);
    (void)snprintf(compiled, compiled_size, "%u.%u.%u",
                   (unsigned)at_build.major, (unsigned)at_build.minor,
                   (unsigned)at_build.patch);
}

static void sndfile_version(char *out, size_t out_size)
{
    int length;

    /* NULL is the documented handle for this query. */
    length = sf_command(NULL, SFC_GET_LIB_VERSION, out, (int)out_size);
    if (length <= 0 || (size_t)length >= out_size) {
        (void)snprintf(out, out_size, "unknown");
        return;
    }
    out[length] = '\0';
}

/* ----------------------------------------------------------------- doctor */

typedef enum store_state {
    STORE_MISSING = 0,
    STORE_PRESENT = 1,
    STORE_UNREADABLE = 2
} store_state;

static const char *store_word(store_state state)
{
    switch (state) {
    case STORE_PRESENT:
        return "present";
    case STORE_MISSING:
        return "missing";
    case STORE_UNREADABLE:
        break;
    }
    return "unreadable";
}

static kpa_result count_projects(uint32_t *out_count)
{
    kpa_project_summary *summaries;
    kpa_result result;

    *out_count = 0u;
    /* The listing is the only way to ask, and asking it here means the
     * doctor's count is the count --list prints rather than a second answer
     * arrived at another way. */
    summaries = calloc(LIST_CAPACITY, sizeof *summaries);
    if (summaries == NULL) return KPA_NO_MEMORY;
    result = kpa_project_list(summaries, LIST_CAPACITY, out_count);
    free(summaries);
    return result;
}

typedef struct doctor_report {
    char sdl_linked[32];
    char sdl_compiled[32];
    char sndfile[128];
    char directory[DIRECTORY_CAPACITY];
    const char *graphics_detail;
    const char *error_name;   /* NULL when nothing failed */
    uint32_t count;
    probe_state graphics;
    store_state store;
    bool audio_device;
    bool have_count;
} doctor_report;

static void doctor_collect(doctor_report *report)
{
    kpa_result result;

    (void)memset(report, 0, sizeof *report);
    report->graphics_detail = "";
    /* Probe first, print second: the graphics probe talks to the same
     * terminal the report is about to be written to. */
    report->graphics = probe_graphics(&report->graphics_detail);
    report->audio_device = audio_device_opens();
    sdl_versions(report->sdl_linked, sizeof report->sdl_linked,
                 report->sdl_compiled, sizeof report->sdl_compiled);
    sndfile_version(report->sndfile, sizeof report->sndfile);

    result = kpa_projects_directory(report->directory,
                                    sizeof report->directory);
    if (result != KPA_OK) {
        report->directory[0] = '\0';
        report->store = STORE_UNREADABLE;
        report->error_name = kpa_result_name(result);
        return;
    }
    result = count_projects(&report->count);
    if (result == KPA_OK) {
        report->store = STORE_PRESENT;
        report->have_count = true;
    } else if (result == KPA_NOT_FOUND) {
        /* Nothing has written a project yet.  Calling that an error would
         * make a working machine look broken on the day it is set up. */
        report->store = STORE_MISSING;
        report->have_count = true;
        report->count = 0u;
    } else {
        report->store = STORE_UNREADABLE;
        report->error_name = kpa_result_name(result);
    }
}

static void doctor_print_text(const doctor_report *report)
{
    (void)printf("%-18s %s %s\n", "program", PROGRAM, PROGRAM_VERSION);
    (void)printf("%-18s %s linked, %s at build\n", "sdl2", report->sdl_linked,
                 report->sdl_compiled);
    (void)printf("%-18s %s\n", "libsndfile", report->sndfile);
    (void)printf("%-18s %s\n", "audio device",
                 report->audio_device ? "opens" : "does not open");
    (void)printf("%-18s %s (%s)\n", "terminal graphics",
                 probe_word(report->graphics), report->graphics_detail);
    (void)printf("%-18s %s\n", "projects directory",
                 report->directory[0] != '\0' ? report->directory
                                              : "unresolved");
    (void)printf("%-18s %s\n", "projects store", store_word(report->store));
    if (report->have_count) {
        (void)printf("%-18s %u\n", "projects found", report->count);
    } else {
        (void)printf("%-18s unknown\n", "projects found");
    }
    if (report->error_name != NULL) {
        (void)printf("%-18s %s\n", "projects error", report->error_name);
    }
}

/*
 * A path is bytes and a JSON string is text.  One that is not valid UTF-8
 * cannot be carried as text, so its high bytes are marked rather than
 * emitted as something a parser would reject: the report stays parseable and
 * says, in the only way JSON can, that this is not what was on disk.
 */
static void json_text(FILE *stream, const char *text)
{
    const unsigned char *byte = (const unsigned char *)text;
    const bool utf8 = kpa_cells_valid_utf8(text, strlen(text));

    (void)fputc('"', stream);
    for (; *byte != '\0'; byte++) {
        if (*byte == '"' || *byte == '\\') {
            (void)fprintf(stream, "\\%c", (int)*byte);
        } else if (*byte < 0x20) {
            (void)fprintf(stream, "\\u%04X", (unsigned)*byte);
        } else if (*byte >= 0x80 && !utf8) {
            (void)fputs("\\uFFFD", stream);
        } else {
            (void)fputc((int)*byte, stream);
        }
    }
    (void)fputc('"', stream);
}

static void json_pair_text(const char *key, const char *value, bool last)
{
    (void)printf("  \"%s\": ", key);
    json_text(stdout, value);
    (void)printf("%s\n", last ? "" : ",");
}

/* For the values JSON spells without quotes: true, false, null, a number. */
static void json_pair_raw(const char *key, const char *value, bool last)
{
    (void)printf("  \"%s\": %s%s\n", key, value, last ? "" : ",");
}

static void doctor_print_json(const doctor_report *report)
{
    char count[32];

    (void)printf("{\n");
    json_pair_text("program", PROGRAM, false);
    json_pair_text("version", PROGRAM_VERSION, false);
    json_pair_text("sdl2_linked", report->sdl_linked, false);
    json_pair_text("sdl2_compiled", report->sdl_compiled, false);
    json_pair_text("libsndfile", report->sndfile, false);
    json_pair_raw("audio_device", report->audio_device ? "true" : "false",
                  false);
    json_pair_text("terminal_graphics", probe_word(report->graphics), false);
    json_pair_text("terminal_graphics_detail", report->graphics_detail,
                   false);
    if (report->directory[0] != '\0') {
        json_pair_text("projects_directory", report->directory, false);
    } else {
        json_pair_raw("projects_directory", "null", false);
    }
    json_pair_text("projects_store", store_word(report->store), false);
    if (report->have_count) {
        (void)snprintf(count, sizeof count, "%u", report->count);
        json_pair_raw("project_count", count, false);
    } else {
        json_pair_raw("project_count", "null", false);
    }
    if (report->error_name != NULL) {
        json_pair_text("projects_error", report->error_name, true);
    } else {
        json_pair_raw("projects_error", "null", true);
    }
    (void)printf("}\n");
}

static int command_doctor(bool json)
{
    doctor_report *report = calloc(1u, sizeof *report);

    if (report == NULL) {
        fail("out of memory");
        return KPA_EXIT_FAILED;
    }
    doctor_collect(report);
    if (json) {
        doctor_print_json(report);
    } else {
        doctor_print_text(report);
    }
    free(report);
    /*
     * A report that was produced is a success, whatever it says.  A script
     * wanting a verdict reads a field; one that wanted the report should not
     * have to treat a machine with no sound card as a broken command.
     */
    return KPA_EXIT_OK;
}

/* ----------------------------------------------------------------- render */

static bool rate_control_available(void)
{
    kpa_audio_options options;
    kpa_audio_session *session = NULL;
    bool available = false;

    /* Offline asks what this build has rather than what this box has, and
     * never opens a device to find out. */
    kpa_audio_options_init(&options);
    options.offline = true;
    if (kpa_audio_create(&session, &options) == KPA_AUDIO_OK) {
        available = kpa_audio_rate_available(session);
        kpa_audio_destroy(session);
    }
    return available;
}

static void copy_text(char *out, size_t out_size, const char *text)
{
    size_t length = strlen(text);

    if (length >= out_size) length = out_size - 1u;
    (void)memcpy(out, text, length);
    out[length] = '\0';
}

/*
 * Where to draw the one frame.  A quarter of the way in, then snapped onto
 * whichever cue is live there, because a picture of an empty player at 0.0
 * proves the program starts and nothing else - and the frame this writes is
 * the acceptance evidence.
 */
static double render_position(const kpa_project *project,
                              const kpa_lyrics *lyrics)
{
    double position = project->duration * 0.25;
    uint32_t index;

    if (!(position > 0.0)) position = 0.0;
    if (lyrics == NULL || lyrics->cue_count == 0u) return position;
    for (index = 0u; index < lyrics->cue_count; index++) {
        if (lyrics->cues[index].end > position) break;
    }
    if (index == lyrics->cue_count) index = lyrics->cue_count - 1u;
    if (position < lyrics->cues[index].start ||
        position >= lyrics->cues[index].end) {
        position = (lyrics->cues[index].start + lyrics->cues[index].end) * 0.5;
    }
    return position;
}

static void build_model(kpa_ui_model *model, const kpa_project *project,
                        const kpa_lyrics *lyrics, const kpa_tab *tab)
{
    uint32_t index;

    (void)memset(model, 0, sizeof *model);
    model->view = KPA_VIEW_PRACTICE;
    model->title = project->title;
    model->artist = project->artist;
    model->duration = project->duration;
    model->position = render_position(project, lyrics);
    model->playing = true;
    model->rate = 1.0;
    model->rate_available = rate_control_available();
    model->track_count = project->track_count;
    /* The reader bounds this already.  The clamp is here because the cost of
     * it being wrong once is a write past the end of model->tracks. */
    if (model->track_count > KPA_MAX_TRACKS) {
        model->track_count = KPA_MAX_TRACKS;
    }
    for (index = 0u; index < model->track_count; index++) {
        kpa_ui_track *track = &model->tracks[index];

        copy_text(track->label, sizeof track->label,
                  project->tracks[index].label);
        copy_text(track->kind, sizeof track->kind,
                  project->tracks[index].kind);
        track->gain = 1.0f;
        /* The manifest's default, which is the mix the practice view opens
         * with; a picture of some other mix is a picture of nothing. */
        track->muted = project->tracks[index].default_muted;
    }
    model->lyrics = lyrics;
    model->lyrics_visible = lyrics != NULL;
    model->tab = tab;
    model->tab_visible = tab != NULL;
    model->active_cue = -1;
    model->active_word = -1;
    if (lyrics != NULL) {
        model->active_cue = kpa_lyrics_cue_at(lyrics, model->position);
        if (model->active_cue >= 0) {
            model->active_word = kpa_lyrics_word_at(lyrics, model->active_cue,
                                                    model->position);
        }
    }
}

static int command_render(const char *project_id, const char *path)
{
    kpa_project *project;
    kpa_ui_model *model;
    kpa_lyrics lyrics;
    kpa_tab tab;
    kpa_result result;
    int status = KPA_EXIT_OK;

    if (!kpa_project_id_valid(project_id)) {
        fail("not a project id");
        return KPA_EXIT_USAGE;
    }
    /* Both are kilobytes rather than bytes; the stack is not where they
     * belong when the failure mode is a surface that will not start. */
    project = calloc(1u, sizeof *project);
    model = calloc(1u, sizeof *model);
    if (project == NULL || model == NULL) {
        free(model);
        free(project);
        fail("out of memory");
        return KPA_EXIT_FAILED;
    }
    (void)memset(&lyrics, 0, sizeof lyrics);
    (void)memset(&tab, 0, sizeof tab);
    result = kpa_project_open(project, project_id);
    if (result != KPA_OK) {
        free(model);
        free(project);
        return project_exit(result);
    }
    /*
     * A layer that will not load is not a reason to refuse the picture: it
     * is a picture of a project that has no lyrics.  Both loaders empty
     * their struct when they fail, so the two frees below are the single
     * owner of whatever did load.
     */
    if (project->has_lyrics) (void)kpa_lyrics_load(project, &lyrics);
    if (project->has_tab) (void)kpa_tab_load(project, &tab);
    build_model(model, project, lyrics.cue_count > 0u ? &lyrics : NULL,
                tab.event_count > 0u ? &tab : NULL);
    if (kpa_ui_render_ppm(model, RENDER_WIDTH, RENDER_HEIGHT, path) != 0) {
        fail("cannot write the render");
        status = KPA_EXIT_FAILED;
    }
    kpa_tab_free(&tab);
    kpa_lyrics_free(&lyrics);
    kpa_project_close(project);
    free(model);
    free(project);
    return status;
}

/* -------------------------------------------------------------- the views */

/*
 * Everything the exit codes promise is decided here, before kpa_ui_run is
 * called, because kpa_ui_run reports a clean exit or an unclean one and
 * nothing in between.  The practice view therefore opens its project twice:
 * once to find out whether it is there, and once inside the surface.  The
 * price of not paying for that open is a program that cannot tell "no such
 * project" from "no sound card", which is the difference between a person
 * fixing it in a second and filing a bug.
 */
static int command_run(const char *project_id)
{
    kpa_project *project;
    kpa_result result;

    if (project_id != NULL) {
        if (!kpa_project_id_valid(project_id)) {
            fail("not a project id");
            return KPA_EXIT_USAGE;
        }
        project = calloc(1u, sizeof *project);
        if (project == NULL) {
            fail("out of memory");
            return KPA_EXIT_FAILED;
        }
        result = kpa_project_open(project, project_id);
        if (result == KPA_OK) kpa_project_close(project);
        free(project);
        if (result != KPA_OK) return project_exit(result);
    }
    /*
     * The surface falls back to cells when a terminal has no graphics path,
     * so a plain xterm is supported and is not this check.  A pipe is not a
     * terminal at all: there is no fallback left below it.
     */
    if (!isatty(STDIN_FILENO) || !isatty(STDOUT_FILENO)) {
        fail("no terminal on stdin and stdout");
        return KPA_EXIT_NO_TERMINAL;
    }
    /* The library view is a list of what this machine has, and reading it
     * needs no sound.  Practice without sound is not practice. */
    if (project_id != NULL && !audio_device_opens()) {
        fail("no audio device is available");
        return KPA_EXIT_NO_AUDIO;
    }
    if (kpa_ui_run(project_id) != 0) {
        fail("the surface stopped with an error");
        return KPA_EXIT_FAILED;
    }
    return KPA_EXIT_OK;
}

static int command_render_argv(int argc, char **argv)
{
    const char *project_id = NULL;
    const char *path = NULL;
    int index = 2;

    if (argc > 2 && argv[2][0] != '-') {
        project_id = argv[2];
        index = 3;
    }
    for (; index < argc; index++) {
        if (strcmp(argv[index], "--out") == 0 && index + 1 < argc) {
            path = argv[++index];
        } else {
            usage(stderr);
            return KPA_EXIT_USAGE;
        }
    }
    if (project_id == NULL || path == NULL) {
        usage(stderr);
        return KPA_EXIT_USAGE;
    }
    return command_render(project_id, path);
}

int main(int argc, char **argv)
{
    const char *command;
    int index;

    if (argc < 2) return command_run(NULL);
    command = argv[1];

    if (strcmp(command, "--help") == 0 || strcmp(command, "-h") == 0) {
        usage(stdout);
        return KPA_EXIT_OK;
    }
    if (strcmp(command, "--version") == 0 || strcmp(command, "-V") == 0) {
        (void)printf("%s %s\n", PROGRAM, PROGRAM_VERSION);
        return KPA_EXIT_OK;
    }
    if (strcmp(command, "--list") == 0) {
        if (argc != 2) {
            usage(stderr);
            return KPA_EXIT_USAGE;
        }
        return command_list();
    }
    if (strcmp(command, "--doctor") == 0) {
        bool json = false;

        for (index = 2; index < argc; index++) {
            if (strcmp(argv[index], "--json") != 0) {
                usage(stderr);
                return KPA_EXIT_USAGE;
            }
            json = true;
        }
        return command_doctor(json);
    }
    if (strcmp(command, "--render") == 0) {
        return command_render_argv(argc, argv);
    }
    /* An unrecognised option is a typo, not a project: project ids cannot
     * begin with '-'. */
    if (command[0] == '-' || argc != 2) {
        usage(stderr);
        return KPA_EXIT_USAGE;
    }
    return command_run(command);
}
