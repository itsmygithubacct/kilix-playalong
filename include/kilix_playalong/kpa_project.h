#ifndef KILIX_PLAYALONG_KPA_PROJECT_H
#define KILIX_PLAYALONG_KPA_PROJECT_H

/*
 * Read-only reader for the schema family the Python implementation owns:
 *
 *   kilix.playalong.project/v1   project.state, a kilix-state CRC record
 *   kilix.playalong.lyrics/v1    lyrics/lyrics.json
 *   kilix.playalong.tab/v1       tab/guitar-tab.json
 *
 * This reader never writes a project.  A native surface that cannot safely
 * understand a project opens it read-only or refuses it; it does not rewrite
 * bytes it did not fully understand, and unknown compatible fields are
 * preserved by the simple expedient of never writing the document back.
 *
 * Every artifact is resolved beneath a held project-directory descriptor with
 * O_NOFOLLOW at each component, so an absolute path, a "..", a symlink or a
 * path replaced between validation and open is refused rather than followed.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define KPA_PROJECT_SCHEMA "kilix.playalong.project/v1"
#define KPA_LYRICS_SCHEMA "kilix.playalong.lyrics/v1"
#define KPA_TAB_SCHEMA "kilix.playalong.tab/v1"

#define KPA_MAX_TRACKS 16u
#define KPA_MAX_CUES 8192u
#define KPA_MAX_WORDS 65536u
#define KPA_MAX_TAB_EVENTS 65536u
#define KPA_MAX_TAB_POSITIONS 262144u
#define KPA_STRING_COUNT 6u
#define KPA_TEXT_CAPACITY 512u
#define KPA_PATH_CAPACITY 256u
#define KPA_ID_CAPACITY 72u
#define KPA_MAX_MANIFEST_BYTES (2u * 1024u * 1024u)
#define KPA_MAX_DOCUMENT_BYTES (16u * 1024u * 1024u)

typedef enum kpa_result {
    KPA_OK = 0,
    KPA_INVALID_ARGUMENT = 1,
    KPA_NOT_FOUND = 2,
    KPA_CORRUPT = 3,          /* record or JSON failed structural validation */
    KPA_SCHEMA = 4,           /* a schema string this build does not accept */
    KPA_TOO_LARGE = 5,        /* beyond a declared bound above */
    KPA_SECURITY = 6,         /* traversal, symlink, absolute path, bad mode */
    KPA_IO = 7,
    KPA_NO_MEMORY = 8,
    KPA_UNSUPPORTED = 9       /* understood, but this build cannot present it */
} kpa_result;

const char *kpa_result_name(kpa_result result);

typedef enum kpa_stage_status {
    KPA_STAGE_PENDING = 0,
    KPA_STAGE_RUNNING = 1,
    KPA_STAGE_DONE = 2,
    KPA_STAGE_ERROR = 3
} kpa_stage_status;

typedef struct kpa_track {
    char id[KPA_ID_CAPACITY];
    char label[KPA_TEXT_CAPACITY];
    char kind[KPA_ID_CAPACITY];
    char path[KPA_PATH_CAPACITY];
    uint64_t size;
    bool default_muted;
} kpa_track;

typedef struct kpa_word {
    double start;
    double end;
    const char *text;    /* borrowed from kpa_lyrics.text_bytes */
    uint32_t length;
} kpa_word;

typedef struct kpa_cue {
    double start;
    double end;
    const char *text;    /* borrowed from kpa_lyrics.text_bytes */
    uint32_t length;
    uint32_t first_word;
    uint32_t word_count;
} kpa_cue;

/*
 * Where a cue's times came from.  A span that was measured against the audio
 * and one that was spread evenly across the duration highlight a line with
 * exactly the same confidence, and they have earned very different amounts of
 * it, so the document says which it is rather than leaving a consumer to take
 * a provider string apart looking for a suffix.
 *
 * KPA_TIMING_UNKNOWN is a document written before the field existed.  It is
 * not a quieter word for estimated: it is the absence of a claim, and a
 * surface that drew it as a guess would be inventing one.
 */
typedef enum kpa_lyrics_timing {
    KPA_TIMING_UNKNOWN = 0,
    KPA_TIMING_AUTHORED = 1,    /* the source carried its own stamps */
    KPA_TIMING_MEASURED = 2,    /* alignment placed the words on the audio */
    KPA_TIMING_ESTIMATED = 3    /* spans invented by spreading the text out */
} kpa_lyrics_timing;

/*
 * What the alignment said about itself, and only ever for a measured
 * document: numbers describing a measurement that did not happen would be
 * worse than no numbers at all.  `present` is what separates a document that
 * reported nothing from one that reported zeros, and zeros here are a perfect
 * alignment rather than a missing one.
 */
typedef struct kpa_lyrics_alignment {
    bool present;
    double matched_fraction;      /* 0..1; outside it the load is refused */
    uint32_t interpolated_words;  /* placed between matches, not measured */
    double mean_displacement;     /* seconds, finite and not negative */
    bool usable;                  /* the producer's own verdict on the above */
} kpa_lyrics_alignment;

typedef struct kpa_lyrics {
    kpa_cue *cues;
    uint32_t cue_count;
    kpa_word *words;
    uint32_t word_count;
    /* One owned copy of every decoded cue/word string, validated UTF-8. */
    char *text_bytes;
    size_t text_size;
    char language[KPA_ID_CAPACITY];
    char source[KPA_ID_CAPACITY];
    /* Added to the schema after the first documents were written, so absence
     * is ordinary: it reads as unknown timing and no report, which is what
     * every document from before it is. */
    kpa_lyrics_timing timing;
    kpa_lyrics_alignment alignment;
} kpa_lyrics;

typedef struct kpa_tab_position {
    uint8_t string_index;   /* 0 = low E, ascending pitch: API order */
    uint8_t fret;
    uint8_t pitch;
} kpa_tab_position;

typedef struct kpa_tab_event {
    double start;
    double end;
    uint32_t first_position;
    uint32_t position_count;
} kpa_tab_event;

typedef struct kpa_tab {
    kpa_tab_event *events;
    uint32_t event_count;
    kpa_tab_position *positions;
    uint32_t position_count;
    /* Index 0 is the low E string, matching the API and the solver. A display
     * that numbers strings the way players do must invert this. */
    int32_t tuning_midi[KPA_STRING_COUNT];
    char tuning_labels[KPA_STRING_COUNT][8];
    uint32_t string_count;
    uint32_t max_fret;
} kpa_tab;

typedef struct kpa_project {
    char id[KPA_ID_CAPACITY];
    char title[KPA_TEXT_CAPACITY];
    char artist[KPA_TEXT_CAPACITY];
    double duration;
    kpa_track tracks[KPA_MAX_TRACKS];
    uint32_t track_count;
    bool has_lyrics;
    bool has_tab;
    char lyrics_path[KPA_PATH_CAPACITY];
    char tab_path[KPA_PATH_CAPACITY];
    char printable_path[KPA_PATH_CAPACITY];
    char ascii_tab_path[KPA_PATH_CAPACITY];
    char midi_path[KPA_PATH_CAPACITY];
    kpa_stage_status stages[8];
    uint32_t stage_count;
    /* Held for the project's lifetime; every artifact opens beneath it. */
    int directory_fd;
} kpa_project;

/* Bounded listing of the private project store, newest first. */
typedef struct kpa_project_summary {
    char id[KPA_ID_CAPACITY];
    char title[KPA_TEXT_CAPACITY];
    char artist[KPA_TEXT_CAPACITY];
    double duration;
    uint32_t track_count;
    bool ready;          /* every stage this build needs is done */
    bool has_lyrics;
    bool has_tab;
} kpa_project_summary;

/*
 * Resolve the private project root: $KILIX_PLAYALONG_DATA_HOME, else
 * $XDG_DATA_HOME/kilix-playalong, else ~/.local/share/kilix-playalong, then
 * "projects" beneath it.  Matches paths.py exactly; a divergence here strands
 * projects, so the differential test asserts the same answer as Python.
 */
kpa_result kpa_projects_directory(char *out, size_t out_size);

kpa_result kpa_project_list(kpa_project_summary *out, uint32_t capacity,
                            uint32_t *out_count);

/* project_id is validated against the same grammar as paths.py. */
bool kpa_project_id_valid(const char *project_id);

kpa_result kpa_project_open(kpa_project *project, const char *project_id);
void kpa_project_close(kpa_project *project);

kpa_result kpa_lyrics_load(const kpa_project *project, kpa_lyrics *lyrics);
void kpa_lyrics_free(kpa_lyrics *lyrics);

kpa_result kpa_tab_load(const kpa_project *project, kpa_tab *tab);
void kpa_tab_free(kpa_tab *tab);

/*
 * Open one artifact beneath the project directory.  Returns a read-only
 * descriptor the caller owns, or -1 with a kpa_result in *error.  This is the
 * only sanctioned way to reach project bytes: it is what keeps a manifest
 * path from naming /etc/shadow.
 */
int kpa_project_open_artifact(const kpa_project *project,
                              const char *relative_path, kpa_result *error);

/*
 * Active cue for a playback position, or -1.  Cues are sorted by start, which
 * load enforces; they are NOT disjoint, and rolling captions from a video
 * platform routinely overlap.  The active cue is therefore the last one that
 * has started and has not ended - the newest line, which is the one being
 * sung.  The span is half open, so a position exactly on an end belongs to
 * what comes next rather than to two cues at once.  The search is binary and
 * never scans the whole song per frame.
 */
int32_t kpa_lyrics_cue_at(const kpa_lyrics *lyrics, double seconds);
int32_t kpa_lyrics_word_at(const kpa_lyrics *lyrics, int32_t cue_index,
                           double seconds);
/* First event with end > seconds: the left edge of a rolling tab window. */
uint32_t kpa_tab_first_after(const kpa_tab *tab, double seconds);

#ifdef __cplusplus
}
#endif

#endif
