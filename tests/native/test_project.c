/*
 * Tests for the read-only project reader.
 *
 * Every project here is built from scratch in a temporary directory: the
 * manifests are assembled as text, the kilix-state records are framed and
 * CRC'd by hand, and the timed documents are generated.  Nothing under test
 * ships in the repository, and no real song, stem, lyric or tab is read
 * except by the differential mode at the bottom, which reads a project the
 * user already has and prints values rather than copying bytes anywhere.
 *
 * The security cases are grouped by what an attacker would be trying to do
 * rather than by which line refuses them, and the two that matter most are
 * the swaps: a path that validated as a plain relative path and then became a
 * symlink before the open, at the last component and at an interior one.
 * That pair is the whole reason the reader walks components under a held
 * descriptor instead of resolving a path once and trusting the answer.
 *
 * Differential mode:
 *
 *     ./test_project --dump <project-id>
 *
 * prints every value the C reader extracts, one per line, in a form the
 * Python reader can reproduce exactly.  tests/native/test_project.c carries
 * no Python; the comparison is run by piping this output against the same
 * dump produced through src/kilix_playalong/state.py.  The exact Python is
 * recorded in the report that accompanied this file so the two dumps stay a
 * byte-for-byte diff rather than a description of one.
 */

#include "kilix_playalong/kpa_project.h"

#include "kilix_state.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define CHECK(condition)                                                      \
    do {                                                                      \
        if (!(condition)) {                                                   \
            (void)fprintf(stderr, "%s:%d: check failed: %s\n",                \
                          __FILE__, __LINE__, #condition);                    \
            return false;                                                     \
        }                                                                     \
    } while (false)

#define CHECK_RESULT(expression, expected)                                    \
    do {                                                                      \
        const kpa_result actual_ = (expression);                              \
        if (actual_ != (expected)) {                                          \
            (void)fprintf(stderr,                                             \
                          "%s:%d: %s gave %s, wanted %s\n", __FILE__,         \
                          __LINE__, #expression, kpa_result_name(actual_),    \
                          kpa_result_name(expected));                         \
            return false;                                                     \
        }                                                                     \
    } while (false)

#define PATH_CAPACITY 1024u
#define RECORD_HEADER_SIZE 16u

typedef struct kpa_fixture {
    char root[PATH_CAPACITY];       /* the mkdtemp directory */
    char data[PATH_CAPACITY];       /* root/data, the private data home */
    char projects[PATH_CAPACITY];   /* root/data/projects */
} kpa_fixture;

/* ------------------------------------------------------ filesystem tools */

static bool join(char *out, size_t capacity, const char *base,
                 const char *leaf)
{
    const int length = snprintf(out, capacity, "%s/%s", base, leaf);

    return length > 0 && (size_t)length < capacity;
}

static bool make_directory(const char *path)
{
    if (mkdir(path, 0700) == 0) return true;
    return errno == EEXIST;
}

/* mkdir -p for the directory part of a project-relative path. */
static bool make_parents(const char *base, const char *relative)
{
    char path[PATH_CAPACITY];
    size_t index;

    if (!join(path, sizeof path, base, relative)) return false;
    for (index = strlen(base) + 1u; path[index] != '\0'; index++) {
        if (path[index] != '/') continue;
        path[index] = '\0';
        if (!make_directory(path)) return false;
        path[index] = '/';
    }
    return true;
}

static bool write_bytes(const char *path, const void *bytes, size_t size)
{
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    const unsigned char *source = bytes;
    size_t offset = 0u;

    if (fd < 0) return false;
    while (offset < size) {
        const ssize_t count = write(fd, source + offset, size - offset);
        if (count > 0) offset += (size_t)count;
        else if (count < 0 && errno == EINTR) continue;
        else {
            (void)close(fd);
            return false;
        }
    }
    return close(fd) == 0;
}

static bool write_artifact(const kpa_fixture *fixture, const char *id,
                           const char *relative, const char *text)
{
    char directory[PATH_CAPACITY];
    char path[PATH_CAPACITY];

    if (!join(directory, sizeof directory, fixture->projects, id)) return false;
    if (!make_parents(directory, relative)) return false;
    if (!join(path, sizeof path, directory, relative)) return false;
    return write_bytes(path, text, strlen(text));
}

static void put_u32(unsigned char *bytes, uint32_t value)
{
    bytes[0] = (unsigned char)(value & 0xffu);
    bytes[1] = (unsigned char)((value >> 8) & 0xffu);
    bytes[2] = (unsigned char)((value >> 16) & 0xffu);
    bytes[3] = (unsigned char)((value >> 24) & 0xffu);
}

/*
 * A kilix-state CRC record, framed here rather than through kilixstate_save
 * so a test can hand the reader a record that no writer would ever produce:
 * a wrong checksum, a truncated tail, a stale length.
 */
static bool write_record(const char *path, const char *payload, size_t size,
                         uint32_t crc_damage, size_t truncate_to)
{
    unsigned char *record;
    size_t total = RECORD_HEADER_SIZE + size;
    bool ok;

    record = malloc(total);
    if (record == NULL) return false;
    memcpy(record, "KST1", 4u);
    put_u32(record + 4u, 1u);
    put_u32(record + 8u, (uint32_t)size);
    put_u32(record + 12u,
            kilixstate_crc32(payload, size) ^ crc_damage);
    memcpy(record + RECORD_HEADER_SIZE, payload, size);
    if (truncate_to != 0u && truncate_to < total) total = truncate_to;
    ok = write_bytes(path, record, total);
    free(record);
    return ok;
}

static bool install_manifest(const kpa_fixture *fixture, const char *id,
                             const char *payload, uint32_t crc_damage,
                             size_t truncate_to)
{
    char directory[PATH_CAPACITY];
    char path[PATH_CAPACITY];

    if (!join(directory, sizeof directory, fixture->projects, id)) return false;
    if (!make_directory(directory)) return false;
    if (!join(path, sizeof path, directory, "project.state")) return false;
    return write_record(path, payload, strlen(payload), crc_damage,
                        truncate_to);
}

static bool remove_tree(const char *path)
{
    DIR *stream = opendir(path);
    const struct dirent *entry;

    if (stream == NULL) return unlink(path) == 0 || errno == ENOENT;
    while ((entry = readdir(stream)) != NULL) {
        char child[PATH_CAPACITY];
        struct stat status;

        if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0)
            continue;
        if (!join(child, sizeof child, path, entry->d_name)) continue;
        if (lstat(child, &status) != 0) continue;
        if (S_ISDIR(status.st_mode)) (void)remove_tree(child);
        else (void)unlink(child);
    }
    (void)closedir(stream);
    return rmdir(path) == 0;
}

/* ------------------------------------------------------ manifest builder */

typedef struct kpa_manifest_options {
    const char *schema;
    const char *id;
    const char *title;
    const char *artist;
    const char *source;      /* raw object text */
    const char *tracks;      /* raw array text */
    const char *stages;      /* raw object text, NULL for seven done stages */
    const char *lyrics;      /* raw value text */
    const char *tablature;   /* raw value text */
    const char *extra;       /* raw members, each ending in a comma */
} kpa_manifest_options;

#define DEFAULT_TRACKS \
    "[{\"id\":\"vocals\",\"label\":\"Vocals\",\"kind\":\"stem\"," \
    "\"path\":\"stems/vocals.wav\",\"sha256\":\"" \
    "0000000000000000000000000000000000000000000000000000000000000000\"," \
    "\"size\":44,\"default_muted\":false}," \
    "{\"id\":\"guitar\",\"label\":\"Guitar\",\"kind\":\"stem\"," \
    "\"path\":\"stems/guitar.wav\",\"sha256\":\"" \
    "1111111111111111111111111111111111111111111111111111111111111111\"," \
    "\"size\":48,\"default_muted\":true}]"

#define DEFAULT_LYRICS \
    "{\"path\":\"lyrics/lyrics.json\",\"source\":\"youtube-captions\"," \
    "\"language\":\"en\",\"visible\":true}"

#define DEFAULT_TABLATURE \
    "{\"path\":\"tab/guitar-tab.json\"," \
    "\"ascii_path\":\"exports/guitar-tab.txt\"," \
    "\"midi_path\":\"midi/guitar.mid\",\"visible\":true," \
    "\"tuning\":[40,45,50,55,59,64],\"max_fret\":20}"

static void options_init(kpa_manifest_options *options)
{
    options->schema = KPA_PROJECT_SCHEMA;
    options->id = NULL;
    options->title = "Sample Title";
    options->artist = "Sample Artist";
    options->source = "{\"kind\":\"youtube\",\"duration\":241.5}";
    options->tracks = DEFAULT_TRACKS;
    options->stages = NULL;
    options->lyrics = DEFAULT_LYRICS;
    options->tablature = DEFAULT_TABLATURE;
    options->extra = "";
}

static const char *const test_stage_names[] = {
    "download", "normalize", "separate", "lyrics",
    "transcribe-guitar", "tablature", "export"
};

static bool build_stages(char *out, size_t capacity, const char *status)
{
    size_t used = 0u;
    size_t index;

    for (index = 0u; index < sizeof test_stage_names /
                             sizeof test_stage_names[0]; index++) {
        const char *artifacts =
            strcmp(test_stage_names[index], "export") == 0 ?
            "[{\"path\":\"exports/playalong.html\",\"sha256\":\"" \
            "2222222222222222222222222222222222222222222222222222222222222222"
            "\",\"size\":12}]" : "[]";
        const int length = snprintf(out + used, capacity - used,
                                    "%s\"%s\":{\"status\":\"%s\","
                                    "\"started_at\":null,\"finished_at\":null,"
                                    "\"provider\":null,\"artifacts\":%s,"
                                    "\"error\":null}",
                                    index == 0u ? "{" : ",",
                                    test_stage_names[index], status,
                                    artifacts);
        if (length < 0 || (size_t)length >= capacity - used) return false;
        used += (size_t)length;
    }
    if (used + 2u > capacity) return false;
    out[used] = '}';
    out[used + 1u] = '\0';
    return true;
}

static bool build_manifest(char *out, size_t capacity,
                           const kpa_manifest_options *options)
{
    char stages[4096];
    int length;

    if (options->stages == NULL && !build_stages(stages, sizeof stages, "done"))
        return false;
    length = snprintf(out, capacity,
                      "{%s\"artist\":\"%s\","
                      "\"created_at\":\"2026-01-01T00:00:00+00:00\","
                      "\"id\":\"%s\",\"lyrics\":%s,\"schema\":\"%s\","
                      "\"settings\":{},\"source\":%s,\"stages\":%s,"
                      "\"tablature\":%s,\"title\":\"%s\","
                      "\"tracks\":%s,"
                      "\"updated_at\":\"2026-01-02T00:00:00+00:00\"}\n",
                      options->extra, options->artist, options->id,
                      options->lyrics, options->schema, options->source,
                      options->stages != NULL ? options->stages : stages,
                      options->tablature, options->title, options->tracks);
    return length > 0 && (size_t)length < capacity;
}

/* The manifest, the lyrics and the tab of the one project every positive
 * case is built from. */
#define SAMPLE_LYRICS \
    "{\"cues\":[" \
    "{\"start\":1.0,\"end\":3.0,\"text\":\"one two\",\"words\":[" \
    "{\"start\":1.0,\"end\":1.5,\"text\":\"one\"}," \
    "{\"start\":1.5,\"end\":3.0,\"text\":\"two\"}]}," \
    "{\"start\":2.5,\"end\":5.0,\"text\":\"three\",\"words\":[" \
    "{\"start\":2.5,\"end\":5.0,\"text\":\"three\"}]}," \
    "{\"start\":7.0,\"end\":9.0,\"text\":\"f\\u00f8ur\",\"words\":[]}]," \
    "\"language\":\"en\",\"schema\":\"" KPA_LYRICS_SCHEMA "\"," \
    "\"source\":\"youtube-captions\"}\n"

#define SAMPLE_TAB \
    "{\"events\":[" \
    "{\"start\":1.0,\"end\":4.0,\"positions\":[" \
    "{\"fret\":3,\"pitch\":43,\"string\":0}]}," \
    "{\"start\":2.0,\"end\":2.5,\"positions\":[" \
    "{\"fret\":0,\"pitch\":64,\"string\":5}," \
    "{\"fret\":1,\"pitch\":60,\"string\":4}]}," \
    "{\"start\":6.0,\"end\":6.5,\"positions\":[" \
    "{\"fret\":2,\"pitch\":47,\"string\":1}]}]," \
    "\"provider\":\"kilix-playalong-fingering-v1\"," \
    "\"schema\":\"" KPA_TAB_SCHEMA "\",\"source_midi\":\"midi/guitar.mid\"," \
    "\"stats\":{\"events\":3,\"omitted_notes\":0}," \
    "\"tuning\":{\"labels\":[\"E\",\"A\",\"D\",\"G\",\"B\",\"e\"]," \
    "\"max_fret\":20,\"midi\":[40,45,50,55,59,64]}}\n"

/* Build a complete, well-formed project and every file it names. */
static bool install_sample(const kpa_fixture *fixture, const char *id,
                           const kpa_manifest_options *options)
{
    char manifest[8192];
    kpa_manifest_options local;

    if (options == NULL) {
        options_init(&local);
        local.id = id;
        options = &local;
    }
    if (!build_manifest(manifest, sizeof manifest, options)) return false;
    if (!install_manifest(fixture, id, manifest, 0u, 0u)) return false;
    return write_artifact(fixture, id, "lyrics/lyrics.json", SAMPLE_LYRICS) &&
           write_artifact(fixture, id, "tab/guitar-tab.json", SAMPLE_TAB) &&
           write_artifact(fixture, id, "stems/vocals.wav", "RIFFvocals") &&
           write_artifact(fixture, id, "stems/guitar.wav", "RIFFguitar") &&
           write_artifact(fixture, id, "exports/playalong.html", "<html>") &&
           write_artifact(fixture, id, "exports/guitar-tab.txt", "e|--0--") &&
           write_artifact(fixture, id, "midi/guitar.mid", "MThd");
}

/* ------------------------------------------------------------- the tests */

static bool test_result_names(void)
{
    CHECK(strcmp(kpa_result_name(KPA_OK), "ok") == 0);
    CHECK(strcmp(kpa_result_name(KPA_SECURITY), "refused") == 0);
    CHECK(strcmp(kpa_result_name(KPA_SCHEMA), "unsupported schema") == 0);
    CHECK(strcmp(kpa_result_name((kpa_result)99), "unknown") == 0);
    return true;
}

static bool test_project_id_grammar(void)
{
    CHECK(kpa_project_id_valid("song-2577068441cc"));
    CHECK(kpa_project_id_valid("00000000"));
    CHECK(kpa_project_id_valid("0aaaaaaa"));
    CHECK(!kpa_project_id_valid(NULL));
    CHECK(!kpa_project_id_valid(""));
    CHECK(!kpa_project_id_valid("0000000"));          /* seven is too few */
    CHECK(!kpa_project_id_valid("-0000000"));         /* leading hyphen */
    CHECK(!kpa_project_id_valid("Song-000000000"));   /* uppercase */
    CHECK(!kpa_project_id_valid("song_000000000"));   /* underscore */
    CHECK(!kpa_project_id_valid("song/000000000"));   /* separator */
    CHECK(!kpa_project_id_valid("song.000000000"));
    CHECK(!kpa_project_id_valid(".."));
    {
        char oversized[80];
        memset(oversized, 'a', sizeof oversized);
        oversized[64] = '\0';
        CHECK(kpa_project_id_valid(oversized));       /* exactly 64 */
        oversized[64] = 'a';
        oversized[65] = '\0';
        CHECK(!kpa_project_id_valid(oversized));      /* 65 */
    }
    return true;
}

static bool expect_directory(const char *expected)
{
    char actual[PATH_CAPACITY];

    CHECK_RESULT(kpa_projects_directory(actual, sizeof actual), KPA_OK);
    if (strcmp(actual, expected) != 0) {
        (void)fprintf(stderr, "%s:%d: resolved %s, wanted %s\n", __FILE__,
                      __LINE__, actual, expected);
        return false;
    }
    return true;
}

static bool test_projects_directory(const kpa_fixture *fixture)
{
    char saved_home[PATH_CAPACITY];
    char buffer[PATH_CAPACITY];
    const char *home = getenv("HOME");
    bool ok;

    saved_home[0] = '\0';
    if (home != NULL) (void)snprintf(saved_home, sizeof saved_home, "%s", home);

    CHECK_RESULT(kpa_projects_directory(NULL, sizeof buffer),
                 KPA_INVALID_ARGUMENT);
    CHECK_RESULT(kpa_projects_directory(buffer, 0u), KPA_INVALID_ARGUMENT);

    /* The application override wins, and is the data home itself: paths.py
     * does not append the application directory to it. */
    CHECK(setenv("KILIX_PLAYALONG_DATA_HOME", "/tmp/kpa-one", 1) == 0);
    CHECK(setenv("XDG_DATA_HOME", "/tmp/kpa-two", 1) == 0);
    ok = expect_directory("/tmp/kpa-one/projects");
    CHECK(ok);
    /* Trailing separators collapse the way pathlib collapses them. */
    CHECK(setenv("KILIX_PLAYALONG_DATA_HOME", "/tmp/kpa-one///", 1) == 0);
    ok = expect_directory("/tmp/kpa-one/projects");
    CHECK(ok);
    /* An empty variable is unset, so XDG_DATA_HOME takes over. */
    CHECK(setenv("KILIX_PLAYALONG_DATA_HOME", "", 1) == 0);
    ok = expect_directory("/tmp/kpa-two/kilix-playalong/projects");
    CHECK(ok);
    CHECK(unsetenv("KILIX_PLAYALONG_DATA_HOME") == 0);
    ok = expect_directory("/tmp/kpa-two/kilix-playalong/projects");
    CHECK(ok);
    /* A relative value is an error, not something to resolve against the
     * working directory: paths.py:_absolute_env raises on it. */
    CHECK(setenv("XDG_DATA_HOME", "relative/share", 1) == 0);
    CHECK_RESULT(kpa_projects_directory(buffer, sizeof buffer),
                 KPA_INVALID_ARGUMENT);
    CHECK(setenv("KILIX_PLAYALONG_DATA_HOME", "also/relative", 1) == 0);
    CHECK_RESULT(kpa_projects_directory(buffer, sizeof buffer),
                 KPA_INVALID_ARGUMENT);
    CHECK(unsetenv("KILIX_PLAYALONG_DATA_HOME") == 0);
    CHECK(unsetenv("XDG_DATA_HOME") == 0);

    /* Neither set: ~/.local/share, and a relative HOME is refused too. */
    CHECK(setenv("HOME", "/tmp/kpa-home", 1) == 0);
    ok = expect_directory(
        "/tmp/kpa-home/.local/share/kilix-playalong/projects");
    CHECK(ok);
    CHECK(setenv("HOME", "kpa-home", 1) == 0);
    CHECK_RESULT(kpa_projects_directory(buffer, sizeof buffer),
                 KPA_INVALID_ARGUMENT);

    /* A buffer that cannot hold the answer is refused, never truncated. */
    CHECK(setenv("KILIX_PLAYALONG_DATA_HOME", "/tmp/kpa-one", 1) == 0);
    CHECK_RESULT(kpa_projects_directory(buffer, 8u), KPA_TOO_LARGE);
    CHECK(buffer[0] == '\0');

    if (saved_home[0] != '\0') CHECK(setenv("HOME", saved_home, 1) == 0);
    else CHECK(unsetenv("HOME") == 0);
    CHECK(setenv("KILIX_PLAYALONG_DATA_HOME", fixture->data, 1) == 0);
    ok = expect_directory(fixture->projects);
    CHECK(ok);
    return true;
}

static bool test_well_formed(const kpa_fixture *fixture)
{
    const char *id = "song-well-formed";
    kpa_project project;
    kpa_lyrics lyrics;
    kpa_tab tab;
    kpa_result error = KPA_OK;
    int fd;

    CHECK(install_sample(fixture, id, NULL));
    CHECK_RESULT(kpa_project_open(&project, id), KPA_OK);
    CHECK(strcmp(project.id, id) == 0);
    CHECK(strcmp(project.title, "Sample Title") == 0);
    CHECK(strcmp(project.artist, "Sample Artist") == 0);
    CHECK(project.duration == 241.5);
    CHECK(project.track_count == 2u);
    CHECK(strcmp(project.tracks[0].id, "vocals") == 0);
    CHECK(strcmp(project.tracks[0].label, "Vocals") == 0);
    CHECK(strcmp(project.tracks[0].kind, "stem") == 0);
    CHECK(strcmp(project.tracks[0].path, "stems/vocals.wav") == 0);
    CHECK(project.tracks[0].size == 44u && !project.tracks[0].default_muted);
    CHECK(strcmp(project.tracks[1].id, "guitar") == 0);
    CHECK(project.tracks[1].size == 48u && project.tracks[1].default_muted);
    CHECK(project.stage_count == 7u);
    for (uint32_t index = 0u; index < project.stage_count; index++)
        CHECK(project.stages[index] == KPA_STAGE_DONE);
    CHECK(project.has_lyrics && project.has_tab);
    CHECK(strcmp(project.lyrics_path, "lyrics/lyrics.json") == 0);
    CHECK(strcmp(project.tab_path, "tab/guitar-tab.json") == 0);
    CHECK(strcmp(project.ascii_tab_path, "exports/guitar-tab.txt") == 0);
    CHECK(strcmp(project.midi_path, "midi/guitar.mid") == 0);
    CHECK(strcmp(project.printable_path, "exports/playalong.html") == 0);
    CHECK(project.directory_fd >= 0);

    fd = kpa_project_open_artifact(&project, "stems/vocals.wav", &error);
    CHECK(fd >= 0 && error == KPA_OK);
    {
        char buffer[16];
        const ssize_t count = read(fd, buffer, sizeof buffer);
        CHECK(count == 10 && memcmp(buffer, "RIFFvocals", 10u) == 0);
    }
    CHECK(close(fd) == 0);

    CHECK_RESULT(kpa_lyrics_load(&project, &lyrics), KPA_OK);
    CHECK(lyrics.cue_count == 3u && lyrics.word_count == 3u);
    CHECK(strcmp(lyrics.language, "en") == 0);
    CHECK(strcmp(lyrics.source, "youtube-captions") == 0);
    CHECK(lyrics.cues[0].start == 1.0 && lyrics.cues[0].end == 3.0);
    CHECK(lyrics.cues[0].length == 7u);
    CHECK(memcmp(lyrics.cues[0].text, "one two", 7u) == 0);
    CHECK(lyrics.cues[0].text[lyrics.cues[0].length] == '\0');
    CHECK(lyrics.cues[0].first_word == 0u && lyrics.cues[0].word_count == 2u);
    /* Real captions overlap; the reader accepts that and does not reorder. */
    CHECK(lyrics.cues[1].start == 2.5 && lyrics.cues[1].end == 5.0);
    CHECK(lyrics.cues[1].first_word == 2u && lyrics.cues[1].word_count == 1u);
    /* The escaped o-slash in the third cue decodes to five UTF-8 bytes in
     * the owned text block, which is where the copy has to land. */
    CHECK(lyrics.cues[2].length == 5u);
    CHECK(memcmp(lyrics.cues[2].text, "f\xc3\xb8ur", 5u) == 0);
    CHECK(lyrics.cues[2].word_count == 0u);
    CHECK(lyrics.words[0].length == 3u);
    CHECK(memcmp(lyrics.words[0].text, "one", 3u) == 0);
    CHECK(lyrics.text_size > 0u);
    kpa_lyrics_free(&lyrics);
    CHECK(lyrics.cues == NULL && lyrics.text_bytes == NULL);

    CHECK_RESULT(kpa_tab_load(&project, &tab), KPA_OK);
    CHECK(tab.event_count == 3u && tab.position_count == 4u);
    CHECK(tab.string_count == 6u && tab.max_fret == 20u);
    /* Index 0 is the low E, which is what the solver and the API agree on;
     * inverting for a player who counts the high e as string one is the
     * display's job and not this reader's. */
    CHECK(tab.tuning_midi[0] == 40 && tab.tuning_midi[5] == 64);
    CHECK(strcmp(tab.tuning_labels[0], "E") == 0);
    CHECK(strcmp(tab.tuning_labels[5], "e") == 0);
    CHECK(tab.events[0].start == 1.0 && tab.events[0].end == 4.0);
    CHECK(tab.events[0].first_position == 0u &&
          tab.events[0].position_count == 1u);
    CHECK(tab.positions[0].string_index == 0u && tab.positions[0].fret == 3u &&
          tab.positions[0].pitch == 43u);
    CHECK(tab.events[1].first_position == 1u &&
          tab.events[1].position_count == 2u);
    CHECK(tab.positions[1].string_index == 5u);
    kpa_tab_free(&tab);
    CHECK(tab.events == NULL);

    kpa_project_close(&project);
    CHECK(project.directory_fd == -1 && project.track_count == 0u);
    kpa_project_close(&project);      /* idempotent */
    return true;
}

static bool test_missing_project_is_not_created(const kpa_fixture *fixture)
{
    kpa_project project;
    char path[PATH_CAPACITY];
    struct stat status;

    CHECK_RESULT(kpa_project_open(&project, "song-never-existed"),
                 KPA_NOT_FOUND);
    CHECK(project.directory_fd == -1);
    /*
     * kilix-state creates the directories it resolves, so a reader that asked
     * it first would bring a project into being by looking for one.  Nothing
     * may appear on disk here.
     */
    CHECK(join(path, sizeof path, fixture->projects, "song-never-existed"));
    CHECK(stat(path, &status) != 0 && errno == ENOENT);
    CHECK_RESULT(kpa_project_open(&project, "short"), KPA_INVALID_ARGUMENT);
    CHECK_RESULT(kpa_project_open(&project, NULL), KPA_INVALID_ARGUMENT);
    CHECK_RESULT(kpa_project_open(NULL, "song-well-formed"),
                 KPA_INVALID_ARGUMENT);
    return true;
}

static bool test_record_failures(const kpa_fixture *fixture)
{
    char manifest[8192];
    kpa_manifest_options options;
    kpa_project project;
    char directory[PATH_CAPACITY];
    char path[PATH_CAPACITY];

    options_init(&options);
    options.id = "song-record-fail";
    CHECK(build_manifest(manifest, sizeof manifest, &options));

    /* A flipped checksum bit. */
    CHECK(install_manifest(fixture, options.id, manifest, 1u, 0u));
    CHECK_RESULT(kpa_project_open(&project, options.id), KPA_CORRUPT);

    /* A record whose payload was cut short of its declared length. */
    CHECK(install_manifest(fixture, options.id, manifest, 0u,
                           RECORD_HEADER_SIZE + 32u));
    CHECK_RESULT(kpa_project_open(&project, options.id), KPA_CORRUPT);

    /* A record shorter than its own header. */
    CHECK(install_manifest(fixture, options.id, manifest, 0u, 8u));
    CHECK_RESULT(kpa_project_open(&project, options.id), KPA_CORRUPT);

    /* A project directory with no manifest in it at all. */
    CHECK(join(directory, sizeof directory, fixture->projects, options.id));
    CHECK(join(path, sizeof path, directory, "project.state"));
    CHECK(unlink(path) == 0);
    CHECK_RESULT(kpa_project_open(&project, options.id), KPA_NOT_FOUND);

    /* Correct framing around a payload that is not JSON. */
    CHECK(install_manifest(fixture, options.id, "not json at all", 0u, 0u));
    CHECK_RESULT(kpa_project_open(&project, options.id), KPA_CORRUPT);
    CHECK(install_manifest(fixture, options.id, "", 0u, 0u));
    CHECK_RESULT(kpa_project_open(&project, options.id), KPA_CORRUPT);
    /* Valid JSON that is not an object. */
    CHECK(install_manifest(fixture, options.id, "[1,2,3]", 0u, 0u));
    CHECK_RESULT(kpa_project_open(&project, options.id), KPA_CORRUPT);
    /* A manifest that claims to be a different project than its directory. */
    options.id = "song-record-other";
    CHECK(build_manifest(manifest, sizeof manifest, &options));
    CHECK(install_manifest(fixture, "song-record-fail", manifest, 0u, 0u));
    CHECK_RESULT(kpa_project_open(&project, "song-record-fail"), KPA_CORRUPT);
    return true;
}

static bool test_schema_failures(const kpa_fixture *fixture)
{
    const char *id = "song-schema-bad";
    char manifest[8192];
    kpa_manifest_options options;
    kpa_project project;
    kpa_lyrics lyrics;
    kpa_tab tab;

    options_init(&options);
    options.id = id;
    options.schema = "kilix.playalong.project/v2";
    CHECK(build_manifest(manifest, sizeof manifest, &options));
    CHECK(install_manifest(fixture, id, manifest, 0u, 0u));
    CHECK_RESULT(kpa_project_open(&project, id), KPA_SCHEMA);

    /* A missing schema is structural damage rather than a version this build
     * declines: there is no version in it to decline. */
    options.schema = KPA_PROJECT_SCHEMA;
    CHECK(build_manifest(manifest, sizeof manifest, &options));
    {
        char *found = strstr(manifest, "\"schema\"");
        CHECK(found != NULL);
        memcpy(found, "\"scheme\"", 8u);
    }
    CHECK(install_manifest(fixture, id, manifest, 0u, 0u));
    CHECK_RESULT(kpa_project_open(&project, id), KPA_CORRUPT);

    CHECK(install_sample(fixture, id, NULL));
    CHECK_RESULT(kpa_project_open(&project, id), KPA_OK);
    CHECK(write_artifact(fixture, id, "lyrics/lyrics.json",
                         "{\"cues\":[],\"schema\":"
                         "\"kilix.playalong.lyrics/v2\"}"));
    CHECK_RESULT(kpa_lyrics_load(&project, &lyrics), KPA_SCHEMA);
    CHECK(lyrics.cues == NULL);
    CHECK(write_artifact(fixture, id, "tab/guitar-tab.json",
                         "{\"events\":[],\"schema\":"
                         "\"kilix.playalong.tab/v2\"}"));
    CHECK_RESULT(kpa_tab_load(&project, &tab), KPA_SCHEMA);
    CHECK(tab.events == NULL);
    kpa_project_close(&project);
    return true;
}

/*
 * Forward compatibility, both halves.  A member this build has never heard of
 * is ignored wherever it appears, and because the reader never writes a
 * document back, ignoring it is the same as preserving it.
 */
static bool test_unknown_fields(const kpa_fixture *fixture)
{
    const char *id = "song-unknown-key";
    kpa_manifest_options options;
    kpa_project project;
    kpa_lyrics lyrics;
    kpa_tab tab;
    char stages[4096];
    char decorated[1024];

    CHECK(build_stages(stages, sizeof stages, "done"));
    {
        char *closing = strrchr(stages, '}');
        CHECK(closing != NULL);
        (void)snprintf(closing, sizeof stages - (size_t)(closing - stages),
                       ",\"colourise\":{\"status\":\"pending\","
                       "\"artifacts\":[]}}");
    }
    options_init(&options);
    options.id = id;
    options.stages = stages;
    options.extra = "\"aardvark\":{\"nested\":[1,2,{\"deep\":true}]},";
    (void)snprintf(decorated, sizeof decorated,
                   "{\"path\":\"lyrics/lyrics.json\",\"source\":\"x\","
                   "\"language\":\"en\",\"future\":[1,2,3]}");
    options.lyrics = decorated;
    CHECK(install_sample(fixture, id, &options));
    CHECK(write_artifact(fixture, id, "lyrics/lyrics.json",
                         "{\"cues\":[{\"start\":0.5,\"end\":1.5,"
                         "\"text\":\"hi\",\"words\":[],\"speaker\":\"a\"}],"
                         "\"language\":\"en\",\"schema\":\"" KPA_LYRICS_SCHEMA
                         "\",\"source\":\"x\",\"confidence\":0.9}"));
    CHECK(write_artifact(fixture, id, "tab/guitar-tab.json",
                         "{\"events\":[{\"start\":0.5,\"end\":1.5,"
                         "\"positions\":[{\"string\":0,\"fret\":1,"
                         "\"pitch\":41,\"finger\":2}],"
                         "\"technique\":\"slide\"}],"
                         "\"schema\":\"" KPA_TAB_SCHEMA "\","
                         "\"tuning\":{\"midi\":[40,45,50,55,59,64],"
                         "\"labels\":[\"E\",\"A\",\"D\",\"G\",\"B\",\"e\"],"
                         "\"max_fret\":20,\"capo\":0},\"future\":null}"));
    CHECK_RESULT(kpa_project_open(&project, id), KPA_OK);
    CHECK(project.track_count == 2u);
    CHECK(project.stage_count == 7u);
    CHECK(project.has_lyrics);
    CHECK_RESULT(kpa_lyrics_load(&project, &lyrics), KPA_OK);
    CHECK(lyrics.cue_count == 1u && lyrics.cues[0].start == 0.5);
    kpa_lyrics_free(&lyrics);
    CHECK_RESULT(kpa_tab_load(&project, &tab), KPA_OK);
    CHECK(tab.event_count == 1u && tab.positions[0].fret == 1u);
    kpa_tab_free(&tab);
    kpa_project_close(&project);
    return true;
}

static bool test_null_sections(const kpa_fixture *fixture)
{
    const char *id = "song-no-sections";
    kpa_manifest_options options;
    kpa_project project;
    kpa_lyrics lyrics;
    kpa_tab tab;
    char stages[4096];

    CHECK(build_stages(stages, sizeof stages, "pending"));
    options_init(&options);
    options.id = id;
    options.lyrics = "null";
    options.tablature = "null";
    options.tracks = "[]";
    options.stages = stages;
    CHECK(install_sample(fixture, id, &options));
    CHECK_RESULT(kpa_project_open(&project, id), KPA_OK);
    CHECK(!project.has_lyrics && !project.has_tab);
    CHECK(project.lyrics_path[0] == '\0' && project.tab_path[0] == '\0');
    CHECK(project.printable_path[0] == '\0');   /* export never ran */
    CHECK(project.track_count == 0u);
    CHECK(project.stages[6] == KPA_STAGE_PENDING);
    CHECK_RESULT(kpa_lyrics_load(&project, &lyrics), KPA_NOT_FOUND);
    CHECK(lyrics.cue_count == 0u && lyrics.cues == NULL);
    CHECK_RESULT(kpa_tab_load(&project, &tab), KPA_NOT_FOUND);
    CHECK(tab.event_count == 0u);
    /* The searches stay defined on an empty document. */
    CHECK(kpa_lyrics_cue_at(&lyrics, 1.0) == -1);
    CHECK(kpa_lyrics_word_at(&lyrics, 0, 1.0) == -1);
    CHECK(kpa_tab_first_after(&tab, 1.0) == 0u);
    CHECK(kpa_lyrics_cue_at(NULL, 1.0) == -1);
    CHECK(kpa_tab_first_after(NULL, 1.0) == 0u);
    kpa_project_close(&project);

    /* Keys that are simply absent read the same as null, the way state.py
     * turns a missing key into None, and a source with no duration is zero
     * rather than a refusal. */
    {
        char manifest[8192];
        const int length = snprintf(manifest, sizeof manifest,
                                    "{\"artist\":\"\",\"created_at\":\"\","
                                    "\"id\":\"%s\",\"schema\":\"%s\","
                                    "\"settings\":{},\"source\":{},"
                                    "\"stages\":%s,\"title\":\"\","
                                    "\"tracks\":[],\"updated_at\":\"\"}",
                                    id, KPA_PROJECT_SCHEMA, stages);

        CHECK(length > 0 && (size_t)length < sizeof manifest);
        CHECK(install_manifest(fixture, id, manifest, 0u, 0u));
        CHECK_RESULT(kpa_project_open(&project, id), KPA_OK);
        CHECK(!project.has_lyrics && !project.has_tab);
        CHECK(project.duration == 0.0);
        CHECK(project.title[0] == '\0' && project.artist[0] == '\0');
        kpa_project_close(&project);
    }
    return true;
}

/*
 * A manifest path is attacker-controlled the moment anything but this program
 * can write a project.state, so every shape of escape is refused while the
 * manifest is being read rather than when the artifact is opened.
 */
static bool refuse_lyrics_path(const kpa_fixture *fixture, const char *id,
                               const char *path, kpa_result expected)
{
    kpa_manifest_options options;
    kpa_project project;
    char section[1024];
    char manifest[8192];
    const int length = snprintf(section, sizeof section,
                                "{\"path\":\"%s\",\"source\":\"x\","
                                "\"language\":\"en\"}", path);

    CHECK(length > 0 && (size_t)length < sizeof section);
    options_init(&options);
    options.id = id;
    options.lyrics = section;
    CHECK(build_manifest(manifest, sizeof manifest, &options));
    CHECK(install_manifest(fixture, id, manifest, 0u, 0u));
    {
        const kpa_result actual = kpa_project_open(&project, id);
        if (actual != expected) {
            (void)fprintf(stderr, "%s:%d: path %s gave %s, wanted %s\n",
                          __FILE__, __LINE__, path,
                          kpa_result_name(actual),
                          kpa_result_name(expected));
            kpa_project_close(&project);
            return false;
        }
    }
    kpa_project_close(&project);
    return true;
}

static bool test_manifest_path_security(const kpa_fixture *fixture)
{
    const char *id = "song-path-abuse";
    char oversized[KPA_PATH_CAPACITY + 64u];
    char deep[128];
    uint32_t index;

    CHECK(refuse_lyrics_path(fixture, id, "/etc/shadow", KPA_SECURITY));
    CHECK(refuse_lyrics_path(fixture, id, "/", KPA_SECURITY));
    CHECK(refuse_lyrics_path(fixture, id, "../../etc/shadow", KPA_SECURITY));
    CHECK(refuse_lyrics_path(fixture, id, "lyrics/../../escape",
                             KPA_SECURITY));
    CHECK(refuse_lyrics_path(fixture, id, "..", KPA_SECURITY));
    CHECK(refuse_lyrics_path(fixture, id, ".", KPA_SECURITY));
    CHECK(refuse_lyrics_path(fixture, id, "./lyrics.json", KPA_SECURITY));
    CHECK(refuse_lyrics_path(fixture, id, "lyrics//lyrics.json",
                             KPA_SECURITY));
    CHECK(refuse_lyrics_path(fixture, id, "lyrics/", KPA_SECURITY));
    CHECK(refuse_lyrics_path(fixture, id, "", KPA_SECURITY));
    /* A NUL inside the JSON string: the reader refuses to shorten a path at
     * the NUL, which is exactly how "a.wav\0../../etc" gets past a check. */
    CHECK(refuse_lyrics_path(fixture, id, "lyrics\\u0000/../../etc/shadow",
                             KPA_SECURITY));
    CHECK(refuse_lyrics_path(fixture, id, "lyrics\\u0000.json", KPA_SECURITY));
    /* Control bytes cannot appear in an artifact this pipeline wrote. */
    CHECK(refuse_lyrics_path(fixture, id, "lyrics\\u0001.json", KPA_SECURITY));

    /* One byte past the field that has to hold it, and far past it. */
    memset(oversized, 'a', sizeof oversized);
    oversized[KPA_PATH_CAPACITY] = '\0';
    CHECK(refuse_lyrics_path(fixture, id, oversized, KPA_TOO_LARGE));
    /* The longest path the field can hold is accepted; whether anything is
     * there is a question for the open, not for the manifest. */
    oversized[KPA_PATH_CAPACITY - 1u] = '\0';
    CHECK(refuse_lyrics_path(fixture, id, oversized, KPA_OK));
    /* Deeper than the reader will walk, while still short enough to fit. */
    for (index = 0u; index < 33u; index++) {
        deep[index * 2u] = 'd';
        deep[index * 2u + 1u] = '/';
    }
    deep[65u] = '\0';
    CHECK(refuse_lyrics_path(fixture, id, deep, KPA_TOO_LARGE));

    /* The same refusals reach a stage's recorded artifacts, which this
     * reader never opens but will not carry either. */
    {
        kpa_manifest_options options;
        kpa_project project;
        char manifest[8192];

        options_init(&options);
        options.id = id;
        options.stages =
            "{\"download\":{\"status\":\"done\",\"artifacts\":"
            "[{\"path\":\"../../../.ssh/id_ed25519\",\"sha256\":\"\","
            "\"size\":0}]},"
            "\"normalize\":{\"status\":\"pending\",\"artifacts\":[]},"
            "\"separate\":{\"status\":\"pending\",\"artifacts\":[]},"
            "\"lyrics\":{\"status\":\"pending\",\"artifacts\":[]},"
            "\"transcribe-guitar\":{\"status\":\"pending\",\"artifacts\":[]},"
            "\"tablature\":{\"status\":\"pending\",\"artifacts\":[]},"
            "\"export\":{\"status\":\"pending\",\"artifacts\":[]}}";
        CHECK(build_manifest(manifest, sizeof manifest, &options));
        CHECK(install_manifest(fixture, id, manifest, 0u, 0u));
        CHECK_RESULT(kpa_project_open(&project, id), KPA_SECURITY);
    }
    return true;
}

static bool test_artifact_walk(const kpa_fixture *fixture)
{
    const char *id = "song-walk-guard";
    kpa_project project;
    kpa_result error = KPA_OK;
    char directory[PATH_CAPACITY];
    char path[PATH_CAPACITY];
    char outside[PATH_CAPACITY];

    CHECK(install_sample(fixture, id, NULL));
    CHECK(join(directory, sizeof directory, fixture->projects, id));
    CHECK(join(outside, sizeof outside, fixture->root, "outside.txt"));
    CHECK(write_bytes(outside, "secret", 6u));
    CHECK_RESULT(kpa_project_open(&project, id), KPA_OK);

    CHECK(kpa_project_open_artifact(NULL, "a", &error) < 0);
    CHECK(error == KPA_INVALID_ARGUMENT);
    CHECK(kpa_project_open_artifact(&project, NULL, &error) < 0);
    CHECK(error == KPA_INVALID_ARGUMENT);
    /* The error pointer is optional; the refusal is not. */
    CHECK(kpa_project_open_artifact(&project, "/etc/shadow", NULL) < 0);
    CHECK(kpa_project_open_artifact(&project, "/etc/shadow", &error) < 0);
    CHECK(error == KPA_SECURITY);
    CHECK(kpa_project_open_artifact(&project, "../outside.txt", &error) < 0);
    CHECK(error == KPA_SECURITY);
    CHECK(kpa_project_open_artifact(&project, "stems/../../outside.txt",
                                    &error) < 0);
    CHECK(error == KPA_SECURITY);
    CHECK(kpa_project_open_artifact(&project, "stems/missing.wav", &error) < 0);
    CHECK(error == KPA_NOT_FOUND);
    /* A directory is not an artifact, and neither is a fifo that would park
     * the caller inside open(). */
    CHECK(kpa_project_open_artifact(&project, "stems", &error) < 0);
    CHECK(error == KPA_SECURITY);
    CHECK(join(path, sizeof path, directory, "pipe"));
    CHECK(mkfifo(path, 0600) == 0);
    CHECK(kpa_project_open_artifact(&project, "pipe", &error) < 0);
    CHECK(error == KPA_SECURITY);
    /* A component that is a regular file cannot be walked through. */
    CHECK(kpa_project_open_artifact(&project, "pipe/inside", &error) < 0);
    CHECK(error == KPA_SECURITY);
    CHECK(unlink(path) == 0);

    /* A symlink at the last component, pointing out of the project. */
    CHECK(join(path, sizeof path, directory, "leak.txt"));
    CHECK(symlink(outside, path) == 0);
    CHECK(kpa_project_open_artifact(&project, "leak.txt", &error) < 0);
    CHECK(error == KPA_SECURITY);
    /* A symlink that points at a perfectly ordinary file inside the project
     * is refused too: the walk refuses links, not destinations. */
    CHECK(unlink(path) == 0);
    CHECK(symlink("stems/vocals.wav", path) == 0);
    CHECK(kpa_project_open_artifact(&project, "leak.txt", &error) < 0);
    CHECK(error == KPA_SECURITY);
    CHECK(unlink(path) == 0);

    /* A symlink at an interior component. */
    CHECK(join(path, sizeof path, directory, "escape"));
    CHECK(symlink(fixture->root, path) == 0);
    CHECK(kpa_project_open_artifact(&project, "escape/outside.txt",
                                    &error) < 0);
    CHECK(error == KPA_SECURITY);
    CHECK(unlink(path) == 0);

    /*
     * The swap.  The path validated as an ordinary relative path while the
     * manifest was read; the file it names becomes a symlink before the open.
     * A reader that resolved once and trusted the answer would follow it.
     */
    CHECK(join(path, sizeof path, directory, "lyrics/lyrics.json"));
    CHECK(unlink(path) == 0);
    CHECK(symlink(outside, path) == 0);
    {
        kpa_lyrics lyrics;
        CHECK_RESULT(kpa_lyrics_load(&project, &lyrics), KPA_SECURITY);
        CHECK(lyrics.cues == NULL);
    }
    CHECK(unlink(path) == 0);
    CHECK(write_artifact(fixture, id, "lyrics/lyrics.json", SAMPLE_LYRICS));

    /* The same swap one level up, at a directory this time. */
    {
        char decoy[PATH_CAPACITY];
        char decoy_file[PATH_CAPACITY];
        kpa_lyrics lyrics;

        CHECK(join(decoy, sizeof decoy, fixture->root, "decoy"));
        CHECK(make_directory(decoy));
        CHECK(join(decoy_file, sizeof decoy_file, decoy, "lyrics.json"));
        CHECK(write_bytes(decoy_file, SAMPLE_LYRICS, strlen(SAMPLE_LYRICS)));
        CHECK(join(path, sizeof path, directory, "lyrics"));
        CHECK(remove_tree(path));
        CHECK(symlink(decoy, path) == 0);
        CHECK_RESULT(kpa_lyrics_load(&project, &lyrics), KPA_SECURITY);
        CHECK(unlink(path) == 0);
        CHECK(write_artifact(fixture, id, "lyrics/lyrics.json",
                             SAMPLE_LYRICS));
        CHECK(remove_tree(decoy));
    }

    /* And the whole project directory replaced by a link to another one. */
    {
        kpa_project linked;
        char link_path[PATH_CAPACITY];

        CHECK(join(link_path, sizeof link_path, fixture->projects,
                   "song-linked-away"));
        CHECK(symlink(directory, link_path) == 0);
        CHECK_RESULT(kpa_project_open(&linked, "song-linked-away"),
                     KPA_SECURITY);
        CHECK(unlink(link_path) == 0);
    }
    kpa_project_close(&project);
    return true;
}

static bool test_bounds(const kpa_fixture *fixture)
{
    const char *id = "song-over-bounds";
    kpa_manifest_options options;
    kpa_project project;
    kpa_lyrics lyrics;
    kpa_tab tab;
    char *buffer;
    char manifest[8192];
    size_t used;
    uint32_t index;

    /* The failure label closes the project, so it has to be closeable from
     * the first line rather than only after a successful open. */
    memset(&project, 0, sizeof project);
    project.directory_fd = -1;
    buffer = malloc(4u * 1024u * 1024u);
    CHECK(buffer != NULL);

    /* One track too many.  The array count is refused before any element is
     * looked at, which is why the elements can be bare zeroes. */
    used = 0u;
    buffer[used++] = '[';
    for (index = 0u; index <= KPA_MAX_TRACKS; index++) {
        buffer[used++] = '0';
        buffer[used++] = ',';
    }
    buffer[used - 1u] = ']';
    buffer[used] = '\0';
    options_init(&options);
    options.id = id;
    options.tracks = buffer;
    CHECK(build_manifest(manifest, sizeof manifest, &options));
    if (!install_manifest(fixture, id, manifest, 0u, 0u)) goto close_failure;
    if (kpa_project_open(&project, id) != KPA_TOO_LARGE) goto close_failure;

    /* Exactly at the bound is accepted. */
    used = 0u;
    buffer[used++] = '[';
    for (index = 0u; index < KPA_MAX_TRACKS; index++) {
        const int length = snprintf(buffer + used, 1024u,
                                    "%s{\"id\":\"t\",\"label\":\"l\","
                                    "\"kind\":\"stem\",\"path\":\"a.wav\","
                                    "\"sha256\":\"\",\"size\":0,"
                                    "\"default_muted\":false}",
                                    index == 0u ? "" : ",");
        if (length <= 0) goto failure;
        used += (size_t)length;
    }
    buffer[used++] = ']';
    buffer[used] = '\0';
    CHECK(build_manifest(manifest, sizeof manifest, &options));
    if (!install_manifest(fixture, id, manifest, 0u, 0u)) goto close_failure;
    if (kpa_project_open(&project, id) != KPA_OK) goto close_failure;
    if (project.track_count != KPA_MAX_TRACKS) goto close_failure;
    kpa_project_close(&project);

    /* A manifest bigger than the record may carry is refused by size, before
     * any of it is read. */
    {
        char *oversized = malloc(KPA_MAX_MANIFEST_BYTES + 32u);
        char path[PATH_CAPACITY];
        char directory[PATH_CAPACITY];
        bool ok;

        if (oversized == NULL) goto failure;
        memset(oversized, 'x', KPA_MAX_MANIFEST_BYTES + 31u);
        oversized[KPA_MAX_MANIFEST_BYTES + 31u] = '\0';
        ok = join(directory, sizeof directory, fixture->projects, id) &&
             make_directory(directory) &&
             join(path, sizeof path, directory, "project.state") &&
             write_record(path, oversized, KPA_MAX_MANIFEST_BYTES + 31u, 0u,
                          0u);
        free(oversized);
        if (!ok) goto close_failure;
        if (kpa_project_open(&project, id) != KPA_TOO_LARGE) goto close_failure;
    }

    /* Back to a sound manifest so the document bounds can be exercised. */
    if (!install_sample(fixture, id, NULL)) goto close_failure;
    if (kpa_project_open(&project, id) != KPA_OK) goto close_failure;

    used = (size_t)snprintf(buffer, 1024u, "{\"schema\":\"%s\",\"cues\":[",
                            KPA_LYRICS_SCHEMA);
    for (index = 0u; index <= KPA_MAX_CUES; index++) {
        buffer[used++] = '0';
        buffer[used++] = ',';
    }
    buffer[used - 1u] = ']';
    buffer[used] = '}';
    buffer[used + 1u] = '\0';
    if (!write_artifact(fixture, id, "lyrics/lyrics.json", buffer))
        goto close_failure;
    if (kpa_lyrics_load(&project, &lyrics) != KPA_TOO_LARGE)
        goto close_failure;

    /* Words are counted across every cue, not per cue. */
    used = (size_t)snprintf(buffer, 1024u, "{\"schema\":\"%s\",\"cues\":[",
                            KPA_LYRICS_SCHEMA);
    for (index = 0u; index < 2u; index++) {
        uint32_t word;
        used += (size_t)snprintf(buffer + used, 1024u, "%s{\"words\":[",
                                 index == 0u ? "" : ",");
        for (word = 0u; word <= KPA_MAX_WORDS / 2u; word++) {
            buffer[used++] = '0';
            buffer[used++] = ',';
        }
        buffer[used - 1u] = ']';
        buffer[used++] = '}';
    }
    buffer[used++] = ']';
    buffer[used++] = '}';
    buffer[used] = '\0';
    if (!write_artifact(fixture, id, "lyrics/lyrics.json", buffer))
        goto close_failure;
    if (kpa_lyrics_load(&project, &lyrics) != KPA_TOO_LARGE)
        goto close_failure;

    used = (size_t)snprintf(buffer, 1024u,
                            "{\"schema\":\"%s\",\"tuning\":{\"midi\":"
                            "[40,45,50,55,59,64],\"labels\":"
                            "[\"E\",\"A\",\"D\",\"G\",\"B\",\"e\"],"
                            "\"max_fret\":20},\"events\":[", KPA_TAB_SCHEMA);
    for (index = 0u; index <= KPA_MAX_TAB_EVENTS; index++) {
        buffer[used++] = '0';
        buffer[used++] = ',';
    }
    buffer[used - 1u] = ']';
    buffer[used] = '}';
    buffer[used + 1u] = '\0';
    if (!write_artifact(fixture, id, "tab/guitar-tab.json", buffer))
        goto close_failure;
    if (kpa_tab_load(&project, &tab) != KPA_TOO_LARGE) goto close_failure;

    used = (size_t)snprintf(buffer, 1024u,
                            "{\"schema\":\"%s\",\"tuning\":{\"midi\":"
                            "[40,45,50,55,59,64],\"labels\":"
                            "[\"E\",\"A\",\"D\",\"G\",\"B\",\"e\"],"
                            "\"max_fret\":20},\"events\":[{\"start\":0,"
                            "\"end\":1,\"positions\":[", KPA_TAB_SCHEMA);
    for (index = 0u; index <= KPA_MAX_TAB_POSITIONS; index++) {
        buffer[used++] = '0';
        buffer[used++] = ',';
    }
    buffer[used - 1u] = ']';
    buffer[used++] = '}';
    buffer[used++] = ']';
    buffer[used++] = '}';
    buffer[used] = '\0';
    if (!write_artifact(fixture, id, "tab/guitar-tab.json", buffer))
        goto close_failure;
    if (kpa_tab_load(&project, &tab) != KPA_TOO_LARGE) goto close_failure;

    /* A document larger than the reader will hold is refused on its size
     * alone; the sparse file below is never read. */
    {
        char path[PATH_CAPACITY];
        char directory[PATH_CAPACITY];
        int fd;

        if (!join(directory, sizeof directory, fixture->projects, id) ||
            !join(path, sizeof path, directory, "lyrics/lyrics.json"))
            goto close_failure;
        fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
        if (fd < 0) goto close_failure;
        if (ftruncate(fd, (off_t)KPA_MAX_DOCUMENT_BYTES + 1) != 0 ||
            close(fd) != 0) goto close_failure;
        if (kpa_lyrics_load(&project, &lyrics) != KPA_TOO_LARGE)
            goto close_failure;
    }

    /* A title longer than the field that has to hold it. */
    {
        char *title = malloc(KPA_TEXT_CAPACITY + 16u);

        if (title == NULL) goto close_failure;
        memset(title, 'T', KPA_TEXT_CAPACITY);
        title[KPA_TEXT_CAPACITY] = '\0';
        options_init(&options);
        options.id = id;
        options.title = title;
        if (!build_manifest(manifest, sizeof manifest, &options) ||
            !install_manifest(fixture, id, manifest, 0u, 0u)) {
            free(title);
            goto close_failure;
        }
        free(title);
        kpa_project_close(&project);
        if (kpa_project_open(&project, id) != KPA_TOO_LARGE) goto close_failure;
    }
    free(buffer);
    return true;

close_failure:
    kpa_project_close(&project);
failure:
    free(buffer);
    (void)fprintf(stderr, "%s: bounds check failed\n", __FILE__);
    return false;
}

/*
 * Ordering.  Unsorted input is rejected rather than sorted on load: this
 * reader hands out indices, and an index only means something if it means the
 * same thing in state.py and in the browser player.  Overlap is a separate
 * question and is accepted, because real captions and real chords overlap.
 */
static bool test_ordering(const kpa_fixture *fixture)
{
    const char *id = "song-out-of-order";
    kpa_project project;
    kpa_lyrics lyrics;
    kpa_tab tab;
    char *buffer;
    size_t used;
    uint32_t index;
    bool ok = false;

    CHECK(install_sample(fixture, id, NULL));
    CHECK_RESULT(kpa_project_open(&project, id), KPA_OK);
    buffer = malloc(256u * 1024u);
    if (buffer == NULL) goto done;

    if (!write_artifact(fixture, id, "lyrics/lyrics.json",
                        "{\"schema\":\"" KPA_LYRICS_SCHEMA "\",\"cues\":["
                        "{\"start\":5.0,\"end\":6.0,\"text\":\"b\","
                        "\"words\":[]},"
                        "{\"start\":1.0,\"end\":2.0,\"text\":\"a\","
                        "\"words\":[]}]}")) goto done;
    if (kpa_lyrics_load(&project, &lyrics) != KPA_CORRUPT) goto done;
    if (!write_artifact(fixture, id, "lyrics/lyrics.json",
                        "{\"schema\":\"" KPA_LYRICS_SCHEMA "\",\"cues\":["
                        "{\"start\":5.0,\"end\":4.0,\"text\":\"b\","
                        "\"words\":[]}]}")) goto done;
    if (kpa_lyrics_load(&project, &lyrics) != KPA_CORRUPT) goto done;
    if (!write_artifact(fixture, id, "lyrics/lyrics.json",
                        "{\"schema\":\"" KPA_LYRICS_SCHEMA "\",\"cues\":["
                        "{\"start\":1.0,\"end\":9.0,\"text\":\"b\","
                        "\"words\":[{\"start\":3.0,\"end\":4.0,\"text\":\"x\"},"
                        "{\"start\":2.0,\"end\":5.0,\"text\":\"y\"}]}]}"))
        goto done;
    if (kpa_lyrics_load(&project, &lyrics) != KPA_CORRUPT) goto done;
    /* Overlapping cues are ordinary, not corrupt. */
    if (!write_artifact(fixture, id, "lyrics/lyrics.json", SAMPLE_LYRICS))
        goto done;
    if (kpa_lyrics_load(&project, &lyrics) != KPA_OK) goto done;
    kpa_lyrics_free(&lyrics);

    if (!write_artifact(fixture, id, "tab/guitar-tab.json",
                        "{\"schema\":\"" KPA_TAB_SCHEMA "\",\"tuning\":"
                        "{\"midi\":[40,45,50,55,59,64],\"labels\":"
                        "[\"E\",\"A\",\"D\",\"G\",\"B\",\"e\"],"
                        "\"max_fret\":20},\"events\":["
                        "{\"start\":5.0,\"end\":6.0,\"positions\":[]},"
                        "{\"start\":1.0,\"end\":2.0,\"positions\":[]}]}"))
        goto done;
    if (kpa_tab_load(&project, &tab) != KPA_CORRUPT) goto done;

    /*
     * The overlap depth kpa_tab_first_after leans on.  An event that reaches
     * exactly KPA_TAB_MAX_OVERLAP events ahead is the last accepted shape;
     * one that reaches further is refused, because the correction after the
     * binary search would no longer be able to see it.
     */
    for (index = 0u; index < 2u; index++) {
        const unsigned int span = 256u + index;   /* 256 fits, 257 does not */
        unsigned int event;

        used = (size_t)snprintf(buffer, 1024u,
                                "{\"schema\":\"%s\",\"tuning\":{\"midi\":"
                                "[40,45,50,55,59,64],\"labels\":"
                                "[\"E\",\"A\",\"D\",\"G\",\"B\",\"e\"],"
                                "\"max_fret\":20},\"events\":[",
                                KPA_TAB_SCHEMA);
        for (event = 0u; event < 300u; event++)
            used += (size_t)snprintf(buffer + used, 1024u,
                                     "%s{\"start\":%u,\"end\":%u,"
                                     "\"positions\":[]}",
                                     event == 0u ? "" : ",", event,
                                     event + span);
        used += (size_t)snprintf(buffer + used, 1024u, "]}");
        if (!write_artifact(fixture, id, "tab/guitar-tab.json", buffer))
            goto done;
        if (kpa_tab_load(&project, &tab) !=
            (index == 0u ? KPA_OK : KPA_CORRUPT)) goto done;
        kpa_tab_free(&tab);
    }
    ok = true;

done:
    free(buffer);
    kpa_project_close(&project);
    if (!ok) (void)fprintf(stderr, "%s: ordering check failed\n", __FILE__);
    return ok;
}

/*
 * The searches, at every boundary the header promises: before the first item,
 * exactly on a start, exactly on an end, inside a gap, and after the last.
 * Spans are half open, so a position exactly on an end belongs to what comes
 * next rather than to two items at once.
 */
static bool test_searches(const kpa_fixture *fixture)
{
    const char *id = "song-search-edge";
    kpa_project project;
    kpa_lyrics lyrics;
    kpa_tab tab;

    CHECK(install_sample(fixture, id, NULL));
    CHECK_RESULT(kpa_project_open(&project, id), KPA_OK);
    CHECK_RESULT(kpa_lyrics_load(&project, &lyrics), KPA_OK);

    CHECK(kpa_lyrics_cue_at(&lyrics, -1.0) == -1);      /* before the first */
    CHECK(kpa_lyrics_cue_at(&lyrics, 0.999) == -1);
    CHECK(kpa_lyrics_cue_at(&lyrics, 1.0) == 0);        /* on a start */
    CHECK(kpa_lyrics_cue_at(&lyrics, 2.0) == 0);
    CHECK(kpa_lyrics_cue_at(&lyrics, 2.4999) == 0);
    /* Overlap: the cue that started most recently is the one being sung. */
    CHECK(kpa_lyrics_cue_at(&lyrics, 2.5) == 1);
    CHECK(kpa_lyrics_cue_at(&lyrics, 3.0) == 1);        /* on cue zero's end */
    CHECK(kpa_lyrics_cue_at(&lyrics, 4.999) == 1);
    CHECK(kpa_lyrics_cue_at(&lyrics, 5.0) == -1);       /* on an end */
    CHECK(kpa_lyrics_cue_at(&lyrics, 6.0) == -1);       /* in the gap */
    CHECK(kpa_lyrics_cue_at(&lyrics, 7.0) == 2);
    CHECK(kpa_lyrics_cue_at(&lyrics, 8.999) == 2);
    CHECK(kpa_lyrics_cue_at(&lyrics, 9.0) == -1);       /* on the last end */
    CHECK(kpa_lyrics_cue_at(&lyrics, 1000.0) == -1);    /* after the last */

    CHECK(kpa_lyrics_word_at(&lyrics, 0, 0.9) == -1);
    CHECK(kpa_lyrics_word_at(&lyrics, 0, 1.0) == 0);
    CHECK(kpa_lyrics_word_at(&lyrics, 0, 1.4999) == 0);
    CHECK(kpa_lyrics_word_at(&lyrics, 0, 1.5) == 1);    /* on a word boundary */
    CHECK(kpa_lyrics_word_at(&lyrics, 0, 2.9999) == 1);
    CHECK(kpa_lyrics_word_at(&lyrics, 0, 3.0) == -1);
    /* Word indices are global, so cue one's only word is number two. */
    CHECK(kpa_lyrics_word_at(&lyrics, 1, 2.5) == 2);
    CHECK(kpa_lyrics_word_at(&lyrics, 1, 5.0) == -1);
    CHECK(kpa_lyrics_word_at(&lyrics, 2, 8.0) == -1);   /* cue with no words */
    CHECK(kpa_lyrics_word_at(&lyrics, -1, 1.0) == -1);
    CHECK(kpa_lyrics_word_at(&lyrics, 3, 1.0) == -1);
    CHECK(kpa_lyrics_word_at(NULL, 0, 1.0) == -1);

    CHECK_RESULT(kpa_tab_load(&project, &tab), KPA_OK);
    CHECK(kpa_tab_first_after(&tab, 0.0) == 0u);
    CHECK(kpa_tab_first_after(&tab, 1.0) == 0u);
    /*
     * The case a binary search on start alone gets wrong: at 3.0 the second
     * event has already ended, but the first is still ringing, so the window
     * opens at the first and not at the third.
     */
    CHECK(kpa_tab_first_after(&tab, 3.0) == 0u);
    CHECK(kpa_tab_first_after(&tab, 3.9999) == 0u);
    CHECK(kpa_tab_first_after(&tab, 4.0) == 2u);        /* on an end */
    CHECK(kpa_tab_first_after(&tab, 5.0) == 2u);        /* in the gap */
    CHECK(kpa_tab_first_after(&tab, 6.0) == 2u);        /* on a start */
    CHECK(kpa_tab_first_after(&tab, 6.4999) == 2u);
    CHECK(kpa_tab_first_after(&tab, 6.5) == 3u);        /* past the last end */
    CHECK(kpa_tab_first_after(&tab, 1000.0) == 3u);

    kpa_lyrics_free(&lyrics);
    kpa_tab_free(&tab);
    kpa_project_close(&project);
    return true;
}

static bool test_listing(const kpa_fixture *fixture)
{
    char data[PATH_CAPACITY];
    char projects[PATH_CAPACITY];
    char path[PATH_CAPACITY];
    kpa_fixture local;
    kpa_project_summary summaries[8];
    uint32_t count = 0u;
    uint32_t index;

    /* A store of its own, so the projects the other tests left behind cannot
     * change what this one sees. */
    CHECK(join(data, sizeof data, fixture->root, "library"));
    CHECK(make_directory(data));
    CHECK(join(projects, sizeof projects, data, "projects"));
    CHECK(make_directory(projects));
    (void)snprintf(local.root, sizeof local.root, "%s", fixture->root);
    (void)snprintf(local.data, sizeof local.data, "%s", data);
    (void)snprintf(local.projects, sizeof local.projects, "%s", projects);
    CHECK(setenv("KILIX_PLAYALONG_DATA_HOME", data, 1) == 0);

    CHECK(install_sample(&local, "song-listing-old", NULL));
    CHECK(install_sample(&local, "song-listing-mid", NULL));
    CHECK(install_sample(&local, "song-listing-new", NULL));
    /* A project whose manifest cannot be read is left out of the listing,
     * the way pipeline.py:list_projects skips one that will not load. */
    CHECK(install_manifest(&local, "song-listing-bad", "not json", 0u, 0u));
    /* Neither a directory nor a project id belongs in the answer. */
    CHECK(join(path, sizeof path, projects, "loose-file"));
    CHECK(write_bytes(path, "x", 1u));
    CHECK(join(path, sizeof path, projects, "TooShort"));
    CHECK(make_directory(path));

    /*
     * mtimes are the sort key, so they are set rather than raced for -- and
     * they are set twice, in opposite orders.  Directory order is fixed
     * across the two listings, so a reader that returned it unsorted could
     * agree with at most one of them.
     */
    {
        const char *const names[] = {"song-listing-old", "song-listing-mid",
                                     "song-listing-new"};
        uint32_t pass;

        for (pass = 0u; pass < 2u; pass++) {
            for (index = 0u; index < 3u; index++) {
                struct timespec times[2];
                const long rank = pass == 0u ? (long)index : (long)(2u - index);

                times[0].tv_sec = 1000000 + rank * 100;
                times[0].tv_nsec = 0;
                times[1] = times[0];
                CHECK(join(path, sizeof path, projects, names[index]));
                CHECK(utimensat(AT_FDCWD, path, times, 0) == 0);
            }
            CHECK_RESULT(kpa_project_list(summaries, 8u, &count), KPA_OK);
            CHECK(count == 3u);
            for (index = 0u; index < 3u; index++)
                CHECK(strcmp(summaries[index].id,
                             names[pass == 0u ? 2u - index : index]) == 0);
        }
        /* Leave the newest one newest for the checks below. */
        for (index = 0u; index < 3u; index++) {
            struct timespec times[2];

            times[0].tv_sec = 1000000 + (long)index * 100;
            times[0].tv_nsec = 0;
            times[1] = times[0];
            CHECK(join(path, sizeof path, projects, names[index]));
            CHECK(utimensat(AT_FDCWD, path, times, 0) == 0);
        }
    }

    CHECK_RESULT(kpa_project_list(summaries, 8u, &count), KPA_OK);
    CHECK(count == 3u);
    CHECK(strcmp(summaries[0].id, "song-listing-new") == 0);
    CHECK(strcmp(summaries[1].id, "song-listing-mid") == 0);
    CHECK(strcmp(summaries[2].id, "song-listing-old") == 0);
    CHECK(strcmp(summaries[0].title, "Sample Title") == 0);
    CHECK(strcmp(summaries[0].artist, "Sample Artist") == 0);
    CHECK(summaries[0].duration == 241.5);
    CHECK(summaries[0].track_count == 2u);
    CHECK(summaries[0].ready && summaries[0].has_lyrics &&
          summaries[0].has_tab);
    /* Ready means what cli.py means by it: the export stage finished. */
    {
        kpa_manifest_options options;
        char stages[4096];
        CHECK(build_stages(stages, sizeof stages, "running"));
        options_init(&options);
        options.id = "song-listing-new";
        options.stages = stages;
        CHECK(install_sample(&local, "song-listing-new", &options));
        CHECK_RESULT(kpa_project_list(summaries, 8u, &count), KPA_OK);
        CHECK(count == 3u && !summaries[0].ready);
    }
    /* A caller with room for one gets the newest one. */
    CHECK_RESULT(kpa_project_list(summaries, 1u, &count), KPA_OK);
    CHECK(count == 1u && strcmp(summaries[0].id, "song-listing-new") == 0);
    CHECK_RESULT(kpa_project_list(NULL, 1u, &count), KPA_INVALID_ARGUMENT);
    CHECK_RESULT(kpa_project_list(summaries, 0u, &count),
                 KPA_INVALID_ARGUMENT);
    CHECK_RESULT(kpa_project_list(summaries, 1u, NULL),
                 KPA_INVALID_ARGUMENT);
    /* An empty store lists nothing rather than failing. */
    CHECK(join(data, sizeof data, fixture->root, "empty"));
    CHECK(make_directory(data));
    CHECK(setenv("KILIX_PLAYALONG_DATA_HOME", data, 1) == 0);
    CHECK_RESULT(kpa_project_list(summaries, 8u, &count), KPA_NOT_FOUND);
    CHECK(join(path, sizeof path, data, "projects"));
    CHECK(make_directory(path));
    CHECK_RESULT(kpa_project_list(summaries, 8u, &count), KPA_OK);
    CHECK(count == 0u);

    CHECK(setenv("KILIX_PLAYALONG_DATA_HOME", fixture->data, 1) == 0);
    return true;
}

/*
 * The rest of the refusals, one shape per line: a field with the wrong type,
 * a count outside what a guitar has, a fret off the neck.  Each one is a
 * document a writer could produce by accident and an attacker on purpose.
 */
static bool reject_lyrics(const kpa_fixture *fixture, const char *id,
                          const kpa_project *project, const char *document,
                          kpa_result expected)
{
    kpa_lyrics lyrics;
    kpa_result actual;

    CHECK(write_artifact(fixture, id, "lyrics/lyrics.json", document));
    actual = kpa_lyrics_load(project, &lyrics);
    if (actual != expected) {
        (void)fprintf(stderr, "%s:%d: %s gave %s, wanted %s\n", __FILE__,
                      __LINE__, document, kpa_result_name(actual),
                      kpa_result_name(expected));
        kpa_lyrics_free(&lyrics);
        return false;
    }
    kpa_lyrics_free(&lyrics);
    return true;
}

static bool reject_tab(const kpa_fixture *fixture, const char *id,
                       const kpa_project *project, const char *document,
                       kpa_result expected)
{
    kpa_tab tab;
    kpa_result actual;

    CHECK(write_artifact(fixture, id, "tab/guitar-tab.json", document));
    actual = kpa_tab_load(project, &tab);
    if (actual != expected) {
        (void)fprintf(stderr, "%s:%d: %s gave %s, wanted %s\n", __FILE__,
                      __LINE__, document, kpa_result_name(actual),
                      kpa_result_name(expected));
        kpa_tab_free(&tab);
        return false;
    }
    kpa_tab_free(&tab);
    return true;
}

static bool reject_manifest(const kpa_fixture *fixture, const char *id,
                            const kpa_manifest_options *options,
                            kpa_result expected)
{
    char manifest[8192];
    kpa_project project;
    kpa_result actual;

    CHECK(build_manifest(manifest, sizeof manifest, options));
    CHECK(install_manifest(fixture, id, manifest, 0u, 0u));
    actual = kpa_project_open(&project, id);
    if (actual != expected) {
        (void)fprintf(stderr, "%s:%d: manifest gave %s, wanted %s\n",
                      __FILE__, __LINE__, kpa_result_name(actual),
                      kpa_result_name(expected));
        kpa_project_close(&project);
        return false;
    }
    kpa_project_close(&project);
    return true;
}

#define TUNING_HEAD \
    "{\"schema\":\"" KPA_TAB_SCHEMA "\",\"tuning\":{\"midi\":"

static bool test_malformed(const kpa_fixture *fixture)
{
    const char *id = "song-malformed-x";
    kpa_manifest_options options;
    kpa_project project;
    kpa_lyrics lyrics;
    kpa_tab tab;

    /* Manifest fields, one damaged shape at a time. */
    options_init(&options);
    options.id = id;
    options.source = "{\"duration\":-1.0}";
    CHECK(reject_manifest(fixture, id, &options, KPA_CORRUPT));
    options.source = "{\"duration\":\"241.5\"}";
    CHECK(reject_manifest(fixture, id, &options, KPA_CORRUPT));
    options.source = "[]";
    CHECK(reject_manifest(fixture, id, &options, KPA_CORRUPT));
    options_init(&options);
    options.id = id;
    options.tracks = "{}";
    CHECK(reject_manifest(fixture, id, &options, KPA_CORRUPT));
    options.tracks = "[{\"id\":\"a\",\"label\":\"a\",\"kind\":\"a\","
                     "\"path\":\"a.wav\",\"sha256\":\"a\",\"size\":1}]";
    CHECK(reject_manifest(fixture, id, &options, KPA_CORRUPT));
    options.tracks = "[{\"id\":\"a\",\"label\":\"a\",\"kind\":\"a\","
                     "\"path\":\"a.wav\",\"sha256\":\"a\",\"size\":1.5,"
                     "\"default_muted\":false}]";
    CHECK(reject_manifest(fixture, id, &options, KPA_CORRUPT));
    options.tracks = "[{\"id\":\"a\",\"label\":\"a\",\"kind\":\"a\","
                     "\"path\":\"a.wav\",\"size\":1,"
                     "\"default_muted\":false}]";
    CHECK(reject_manifest(fixture, id, &options, KPA_CORRUPT));
    options_init(&options);
    options.id = id;
    /* A stage this build needs, missing; and a status it has never heard of. */
    options.stages = "{\"download\":{\"status\":\"done\",\"artifacts\":[]}}";
    CHECK(reject_manifest(fixture, id, &options, KPA_CORRUPT));
    options.stages =
        "{\"download\":{\"status\":\"finished\",\"artifacts\":[]},"
        "\"normalize\":{\"status\":\"pending\",\"artifacts\":[]},"
        "\"separate\":{\"status\":\"pending\",\"artifacts\":[]},"
        "\"lyrics\":{\"status\":\"pending\",\"artifacts\":[]},"
        "\"transcribe-guitar\":{\"status\":\"pending\",\"artifacts\":[]},"
        "\"tablature\":{\"status\":\"pending\",\"artifacts\":[]},"
        "\"export\":{\"status\":\"pending\",\"artifacts\":[]}}";
    CHECK(reject_manifest(fixture, id, &options, KPA_CORRUPT));
    options_init(&options);
    options.id = id;
    options.lyrics = "[]";
    CHECK(reject_manifest(fixture, id, &options, KPA_CORRUPT));
    options.lyrics = "{\"source\":\"x\"}";      /* an object with no path */
    CHECK(reject_manifest(fixture, id, &options, KPA_CORRUPT));

    /* A sound project to hang the document cases off. */
    CHECK(install_sample(fixture, id, NULL));
    CHECK_RESULT(kpa_project_open(&project, id), KPA_OK);

    CHECK_RESULT(kpa_lyrics_load(&project, NULL), KPA_INVALID_ARGUMENT);
    CHECK_RESULT(kpa_lyrics_load(NULL, &lyrics), KPA_INVALID_ARGUMENT);
    CHECK_RESULT(kpa_tab_load(&project, NULL), KPA_INVALID_ARGUMENT);
    CHECK_RESULT(kpa_tab_load(NULL, &tab), KPA_INVALID_ARGUMENT);

    CHECK(reject_lyrics(fixture, id, &project, "[]", KPA_CORRUPT));
    CHECK(reject_lyrics(fixture, id, &project, "{\"cues\":[]}", KPA_CORRUPT));
    CHECK(reject_lyrics(fixture, id, &project,
                        "{\"schema\":\"" KPA_LYRICS_SCHEMA "\"}",
                        KPA_CORRUPT));
    CHECK(reject_lyrics(fixture, id, &project,
                        "{\"schema\":\"" KPA_LYRICS_SCHEMA "\","
                        "\"cues\":[{\"start\":1,\"end\":2}]}",
                        KPA_CORRUPT));   /* a cue with no text */
    CHECK(reject_lyrics(fixture, id, &project,
                        "{\"schema\":\"" KPA_LYRICS_SCHEMA "\","
                        "\"cues\":[{\"start\":\"1\",\"end\":2,"
                        "\"text\":\"a\"}]}", KPA_CORRUPT));
    CHECK(reject_lyrics(fixture, id, &project,
                        "{\"schema\":\"" KPA_LYRICS_SCHEMA "\","
                        "\"cues\":[{\"start\":1,\"end\":2,\"text\":\"a\","
                        "\"words\":{}}]}", KPA_CORRUPT));
    CHECK(reject_lyrics(fixture, id, &project,
                        "{\"schema\":\"" KPA_LYRICS_SCHEMA "\","
                        "\"cues\":[],\"language\":"
                        "\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                        "aaaaaaaaaaaaaaaaaaaaaaaaaaaa\"}", KPA_TOO_LARGE));
    /* Truncated JSON, and a byte sequence that is not UTF-8. */
    CHECK(reject_lyrics(fixture, id, &project,
                        "{\"schema\":\"" KPA_LYRICS_SCHEMA "\",\"cues\":[",
                        KPA_CORRUPT));
    CHECK(reject_lyrics(fixture, id, &project,
                        "{\"schema\":\"" KPA_LYRICS_SCHEMA "\","
                        "\"cues\":[],\"source\":\"\xff\xfe\"}",
                        KPA_CORRUPT));

    /* A guitar this build cannot draw is understood but unsupported. */
    CHECK(reject_tab(fixture, id, &project,
                     TUNING_HEAD "[40,45,50,55,59,64,69],\"labels\":"
                     "[\"E\",\"A\",\"D\",\"G\",\"B\",\"e\",\"b\"],"
                     "\"max_fret\":20},\"events\":[]}", KPA_UNSUPPORTED));
    CHECK(reject_tab(fixture, id, &project,
                     TUNING_HEAD "[40,45,50,55,59],\"labels\":"
                     "[\"E\",\"A\",\"D\",\"G\",\"B\"],"
                     "\"max_fret\":20},\"events\":[]}", KPA_UNSUPPORTED));
    /* pipeline.py refuses a tuning that does not ascend, and so does this. */
    CHECK(reject_tab(fixture, id, &project,
                     TUNING_HEAD "[40,45,50,55,64,59],\"labels\":"
                     "[\"E\",\"A\",\"D\",\"G\",\"B\",\"e\"],"
                     "\"max_fret\":20},\"events\":[]}", KPA_CORRUPT));
    CHECK(reject_tab(fixture, id, &project,
                     TUNING_HEAD "[40,45,50,55,59,200],\"labels\":"
                     "[\"E\",\"A\",\"D\",\"G\",\"B\",\"e\"],"
                     "\"max_fret\":20},\"events\":[]}", KPA_CORRUPT));
    CHECK(reject_tab(fixture, id, &project,
                     TUNING_HEAD "[40,45,50,55,59,64],\"labels\":"
                     "[\"E\",\"A\",\"D\",\"G\",\"B\",\"toolonglabel\"],"
                     "\"max_fret\":20},\"events\":[]}", KPA_TOO_LARGE));
    CHECK(reject_tab(fixture, id, &project,
                     "{\"schema\":\"" KPA_TAB_SCHEMA "\",\"events\":[]}",
                     KPA_CORRUPT));
    CHECK(reject_tab(fixture, id, &project,
                     TUNING_HEAD "[40,45,50,55,59,64],\"labels\":"
                     "[\"E\",\"A\",\"D\",\"G\",\"B\",\"e\"],"
                     "\"max_fret\":20}}", KPA_CORRUPT));
    /* A string, a fret and a pitch that are not on the instrument. */
    CHECK(reject_tab(fixture, id, &project,
                     TUNING_HEAD "[40,45,50,55,59,64],\"labels\":"
                     "[\"E\",\"A\",\"D\",\"G\",\"B\",\"e\"],"
                     "\"max_fret\":20},\"events\":[{\"start\":0,\"end\":1,"
                     "\"positions\":[{\"string\":6,\"fret\":1,"
                     "\"pitch\":40}]}]}", KPA_CORRUPT));
    CHECK(reject_tab(fixture, id, &project,
                     TUNING_HEAD "[40,45,50,55,59,64],\"labels\":"
                     "[\"E\",\"A\",\"D\",\"G\",\"B\",\"e\"],"
                     "\"max_fret\":20},\"events\":[{\"start\":0,\"end\":1,"
                     "\"positions\":[{\"string\":0,\"fret\":21,"
                     "\"pitch\":40}]}]}", KPA_CORRUPT));
    CHECK(reject_tab(fixture, id, &project,
                     TUNING_HEAD "[40,45,50,55,59,64],\"labels\":"
                     "[\"E\",\"A\",\"D\",\"G\",\"B\",\"e\"],"
                     "\"max_fret\":20},\"events\":[{\"start\":0,\"end\":1,"
                     "\"positions\":[{\"string\":0,\"fret\":1,"
                     "\"pitch\":128}]}]}", KPA_CORRUPT));
    CHECK(reject_tab(fixture, id, &project,
                     TUNING_HEAD "[40,45,50,55,59,64],\"labels\":"
                     "[\"E\",\"A\",\"D\",\"G\",\"B\",\"e\"],"
                     "\"max_fret\":20},\"events\":[{\"start\":0,"
                     "\"end\":1}]}", KPA_CORRUPT));

    /* Once the project is closed, nothing more opens beneath it. */
    kpa_project_close(&project);
    {
        kpa_result error = KPA_OK;
        CHECK(kpa_project_open_artifact(&project, "stems/vocals.wav",
                                        &error) < 0);
        CHECK(error == KPA_INVALID_ARGUMENT);
        CHECK_RESULT(kpa_lyrics_load(&project, &lyrics), KPA_INVALID_ARGUMENT);
        CHECK_RESULT(kpa_tab_load(&project, &tab), KPA_INVALID_ARGUMENT);
    }
    return true;
}

/* ------------------------------------------------------ differential mode */

/*
 * Every value the reader extracted, one per line, in a form the Python reader
 * can reproduce byte for byte.  Numbers print with seventeen significant
 * digits so a disagreement in the last bit of a timestamp shows up as a diff
 * rather than rounding away, and text prints with control bytes escaped so a
 * newline inside a caption cannot forge a line.
 */
static void dump_text(const char *label, const char *text, size_t length)
{
    size_t index;

    (void)fputs(label, stdout);
    (void)fputc('=', stdout);
    for (index = 0u; index < length; index++) {
        const unsigned char byte = (unsigned char)text[index];

        if (byte < 0x20u || byte == 0x7fu || byte == '\\')
            (void)printf("\\x%02x", byte);
        else (void)fputc((int)byte, stdout);
    }
    (void)fputc('\n', stdout);
}

static void dump_string(const char *label, const char *text)
{
    dump_text(label, text, strlen(text));
}

static void dump_indexed(const char *prefix, uint32_t index, const char *leaf,
                         const char *text)
{
    char label[128];

    (void)snprintf(label, sizeof label, "%s.%u.%s", prefix, index, leaf);
    dump_string(label, text);
}

static const char *status_name(kpa_stage_status status)
{
    switch (status) {
    case KPA_STAGE_PENDING: return "pending";
    case KPA_STAGE_RUNNING: return "running";
    case KPA_STAGE_DONE:    return "done";
    case KPA_STAGE_ERROR:   return "error";
    default:                break;
    }
    return "unknown";
}

static int dump_project(const char *project_id)
{
    kpa_project project;
    kpa_lyrics lyrics;
    kpa_tab tab;
    kpa_result result;
    uint32_t index;

    result = kpa_project_open(&project, project_id);
    if (result != KPA_OK) {
        (void)fprintf(stderr, "cannot open %s: %s\n", project_id,
                      kpa_result_name(result));
        return EXIT_FAILURE;
    }
    dump_string("project.id", project.id);
    dump_string("project.title", project.title);
    dump_string("project.artist", project.artist);
    (void)printf("project.duration=%.17g\n", project.duration);
    (void)printf("project.tracks=%u\n", project.track_count);
    for (index = 0u; index < project.track_count; index++) {
        const kpa_track *track = &project.tracks[index];

        dump_indexed("track", index, "id", track->id);
        dump_indexed("track", index, "label", track->label);
        dump_indexed("track", index, "kind", track->kind);
        dump_indexed("track", index, "path", track->path);
        (void)printf("track.%u.size=%llu\n", index,
                     (unsigned long long)track->size);
        (void)printf("track.%u.default_muted=%d\n", index,
                     track->default_muted ? 1 : 0);
    }
    for (index = 0u; index < project.stage_count; index++)
        (void)printf("stage.%s=%s\n", test_stage_names[index],
                     status_name(project.stages[index]));
    (void)printf("project.has_lyrics=%d\n", project.has_lyrics ? 1 : 0);
    (void)printf("project.has_tab=%d\n", project.has_tab ? 1 : 0);
    dump_string("project.lyrics_path", project.lyrics_path);
    dump_string("project.tab_path", project.tab_path);
    dump_string("project.ascii_tab_path", project.ascii_tab_path);
    dump_string("project.midi_path", project.midi_path);
    dump_string("project.printable_path", project.printable_path);

    if (project.has_lyrics) {
        result = kpa_lyrics_load(&project, &lyrics);
        if (result != KPA_OK) {
            (void)fprintf(stderr, "cannot load lyrics: %s\n",
                          kpa_result_name(result));
            kpa_project_close(&project);
            return EXIT_FAILURE;
        }
        dump_string("lyrics.language", lyrics.language);
        dump_string("lyrics.source", lyrics.source);
        (void)printf("lyrics.cues=%u\n", lyrics.cue_count);
        (void)printf("lyrics.words=%u\n", lyrics.word_count);
        for (index = 0u; index < lyrics.cue_count; index++) {
            const kpa_cue *cue = &lyrics.cues[index];
            char label[128];

            (void)printf("cue.%u.start=%.17g\n", index, cue->start);
            (void)printf("cue.%u.end=%.17g\n", index, cue->end);
            (void)snprintf(label, sizeof label, "cue.%u.text", index);
            dump_text(label, cue->text, cue->length);
            (void)printf("cue.%u.words=%u\n", index, cue->word_count);
        }
        for (index = 0u; index < lyrics.word_count; index++) {
            const kpa_word *word = &lyrics.words[index];
            char label[128];

            (void)printf("word.%u.start=%.17g\n", index, word->start);
            (void)printf("word.%u.end=%.17g\n", index, word->end);
            (void)snprintf(label, sizeof label, "word.%u.text", index);
            dump_text(label, word->text, word->length);
        }
        kpa_lyrics_free(&lyrics);
    }
    if (project.has_tab) {
        result = kpa_tab_load(&project, &tab);
        if (result != KPA_OK) {
            (void)fprintf(stderr, "cannot load tab: %s\n",
                          kpa_result_name(result));
            kpa_project_close(&project);
            return EXIT_FAILURE;
        }
        (void)printf("tab.strings=%u\n", tab.string_count);
        (void)printf("tab.max_fret=%u\n", tab.max_fret);
        for (index = 0u; index < tab.string_count; index++) {
            (void)printf("tab.tuning.%u.midi=%d\n", index,
                         tab.tuning_midi[index]);
            dump_indexed("tab.tuning", index, "label",
                         tab.tuning_labels[index]);
        }
        (void)printf("tab.events=%u\n", tab.event_count);
        (void)printf("tab.positions=%u\n", tab.position_count);
        for (index = 0u; index < tab.event_count; index++) {
            const kpa_tab_event *event = &tab.events[index];

            (void)printf("event.%u.start=%.17g\n", index, event->start);
            (void)printf("event.%u.end=%.17g\n", index, event->end);
            (void)printf("event.%u.positions=%u\n", index,
                         event->position_count);
        }
        for (index = 0u; index < tab.position_count; index++) {
            const kpa_tab_position *position = &tab.positions[index];

            (void)printf("position.%u=%u,%u,%u\n", index,
                         position->string_index, position->fret,
                         position->pitch);
        }
        kpa_tab_free(&tab);
    }
    kpa_project_close(&project);
    return EXIT_SUCCESS;
}

/* ------------------------------------------------------------------ main */

static bool run_suite(const kpa_fixture *fixture)
{
    return test_result_names() && test_project_id_grammar() &&
           test_projects_directory(fixture) && test_well_formed(fixture) &&
           test_missing_project_is_not_created(fixture) &&
           test_record_failures(fixture) && test_schema_failures(fixture) &&
           test_unknown_fields(fixture) && test_null_sections(fixture) &&
           test_manifest_path_security(fixture) &&
           test_artifact_walk(fixture) && test_bounds(fixture) &&
           test_ordering(fixture) && test_malformed(fixture) &&
           test_searches(fixture) &&
           test_listing(fixture);
}

int main(int argc, char **argv)
{
    char template[] = "/tmp/kpa-project-test-XXXXXX";
    kpa_fixture fixture;
    const char *root;
    bool ok;

    /* Differential mode reads a project the user already has, through the
     * store the environment already points at, and prints what it found. */
    if (argc == 3 && strcmp(argv[1], "--dump") == 0)
        return dump_project(argv[2]);
    if (argc != 1) {
        (void)fprintf(stderr, "usage: %s [--dump <project-id>]\n", argv[0]);
        return EXIT_FAILURE;
    }

    root = mkdtemp(template);
    if (root == NULL) return EXIT_FAILURE;
    (void)snprintf(fixture.root, sizeof fixture.root, "%s", root);
    if (!join(fixture.data, sizeof fixture.data, fixture.root, "data") ||
        !make_directory(fixture.data) ||
        !join(fixture.projects, sizeof fixture.projects, fixture.data,
              "projects") ||
        !make_directory(fixture.projects) ||
        setenv("KILIX_PLAYALONG_DATA_HOME", fixture.data, 1) != 0 ||
        unsetenv("XDG_DATA_HOME") != 0) {
        (void)remove_tree(root);
        return EXIT_FAILURE;
    }
    ok = run_suite(&fixture);
    if (!remove_tree(root)) ok = false;
    if (!ok) return EXIT_FAILURE;
    (void)printf("ok: kpa_project (%zu stages, %u tracks, %u cues, %u words, "
                 "%u tab events, %u positions bounded)\n",
                 sizeof test_stage_names / sizeof test_stage_names[0],
                 KPA_MAX_TRACKS, KPA_MAX_CUES, KPA_MAX_WORDS,
                 KPA_MAX_TAB_EVENTS, KPA_MAX_TAB_POSITIONS);
    return EXIT_SUCCESS;
}
