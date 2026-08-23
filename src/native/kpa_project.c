/*
 * Read-only reader for a private project directory.  kpa_project.h holds the
 * contract; what follows are the five decisions the rest of this file turns
 * on.
 *
 * Artifacts are reached one path component at a time with openat and
 * O_NOFOLLOW beneath a descriptor held open for the project's whole lifetime.
 * A realpath check answers a question about the past: between the answer and
 * the open, any component can be replaced by a symlink.  Under the held
 * descriptor the kernel answers about the object it is opening right now, so
 * a component swapped after validation fails with ELOOP instead of being
 * followed out of the project.
 *
 * project.state is read through kilix-state instead of by parsing its 16 byte
 * record here, so the magic, the version, the size agreement and the CRC stay
 * the property of the module that writes them.  The project directory is
 * opened first, because that store creates the directories it resolves: a
 * project that does not exist has to read as missing rather than be brought
 * into being by the act of looking for it.
 *
 * A timed document whose cues or events are not sorted by start is rejected,
 * never sorted on load.  Sorting would renumber the array, and an index this
 * reader hands a caller has to mean what the same index means in state.py and
 * in the browser player.  Overlap is a different matter and is allowed: real
 * captions overlap and real chords ring into each other, so the searches are
 * defined for overlapping input rather than pretending it cannot happen.
 *
 * Every arena is sized from the document's own length, clamped to what the
 * KPA_MAX_* bounds can possibly need, so a hostile document costs a bounded
 * amount of memory and an oversized one reports KPA_TOO_LARGE before a byte
 * of it is read.
 *
 * Nothing here writes to a project.  Unknown members inside an accepted
 * schema are ignored rather than rejected, and because the document is never
 * written back they survive by construction.
 */

#include "kilix_playalong/kpa_project.h"

#include "kilix_playalong/kpa_json.h"
#include "kilix_state.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <math.h>
#include <pwd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#ifndef NAME_MAX
#define NAME_MAX 255
#endif

#define KPA_ROOT_CAPACITY 4096u
#define KPA_MAX_PATH_COMPONENTS 32u
#define KPA_MAX_LIST_ENTRIES 4096u
#define KPA_MANIFEST_NAME "project.state"
#define KPA_APP_DIRECTORY "kilix-playalong"
#define KPA_PROJECTS_DIRECTORY "projects"
#define KPA_DEFAULT_PRINTABLE "exports/playalong.html"
#define KPA_DEFAULT_ASCII_TAB "exports/guitar-tab.txt"
#define KPA_DEFAULT_MIDI "midi/guitar.mid"

/*
 * How many later events one event may overlap.  kpa_tab_first_after binary
 * searches on start and then corrects backwards, and this bound is what makes
 * that correction a constant rather than a scan of the song: an event at or
 * before index p - KPA_TAB_MAX_OVERLAP provably ended before any event at
 * index p began.  The two projects on this machine peak at 18, and the
 * densest run infer_fingerings can emit is one chord per 0.09s, so 256 is a
 * note that rings for twenty-three seconds over an unbroken run of chords.
 * A document that breaks it is rejected rather than silently searched wrong.
 */
#define KPA_TAB_MAX_OVERLAP 256u

/*
 * Node budgets.  A cue costs an object, start, end, text and a words array;
 * a word, a position and an event cost four; one node each is left over for
 * an unknown member.  The arena is the smaller of this and half the document
 * length, since no value can be encoded in fewer than two bytes.
 */
#define KPA_MANIFEST_NODE_CAP (KPA_MAX_MANIFEST_BYTES / 2u + 8u)
#define KPA_LYRICS_NODE_CAP (6u * KPA_MAX_CUES + 5u * KPA_MAX_WORDS + 256u)
#define KPA_TAB_NODE_CAP \
    (6u * KPA_MAX_TAB_EVENTS + 5u * KPA_MAX_TAB_POSITIONS + 256u)

/* An exact JSON integer this reader will look at. */
#define KPA_MAX_EXACT_INTEGER UINT64_C(9007199254740992)

static const char *const stage_names[] = {
    "download", "normalize", "separate", "lyrics",
    "transcribe-guitar", "tablature", "export"
};

#define KPA_STAGE_COUNT (sizeof stage_names / sizeof stage_names[0])
#define KPA_STAGE_EXPORT 6u

_Static_assert(KPA_STAGE_COUNT == 7u, "state.py:STAGE_NAMES has seven stages");
_Static_assert(KPA_STAGE_COUNT <= 8u, "kpa_project.stages holds eight slots");
_Static_assert(KPA_MAX_MANIFEST_BYTES <= KILIXSTATE_MAX_PAYLOAD,
               "kilix-state must be able to carry a whole manifest");
/* The searches below read start and end out of a cue or a word through one
 * stride, which is only sound while both spans begin the same way. */
_Static_assert(offsetof(kpa_cue, start) == 0u &&
                   offsetof(kpa_cue, end) == sizeof(double),
               "kpa_cue must open with start then end");
_Static_assert(offsetof(kpa_word, start) == 0u &&
                   offsetof(kpa_word, end) == sizeof(double),
               "kpa_word must open with start then end");
_Static_assert(offsetof(kpa_tab_event, start) == 0u &&
                   offsetof(kpa_tab_event, end) == sizeof(double),
               "kpa_tab_event must open with start then end");

const char *kpa_result_name(kpa_result result)
{
    switch (result) {
    case KPA_OK:               return "ok";
    case KPA_INVALID_ARGUMENT: return "invalid argument";
    case KPA_NOT_FOUND:        return "not found";
    case KPA_CORRUPT:          return "corrupt";
    case KPA_SCHEMA:           return "unsupported schema";
    case KPA_TOO_LARGE:        return "too large";
    case KPA_SECURITY:         return "refused";
    case KPA_IO:               return "io error";
    case KPA_NO_MEMORY:        return "out of memory";
    case KPA_UNSUPPORTED:      return "unsupported";
    default:                   break;
    }
    return "unknown";
}

/* ------------------------------------------------------------- errno map */

static kpa_result result_from_open_errno(int failure)
{
    switch (failure) {
    /* O_NOFOLLOW on a symlink, or a component that is not a directory: both
     * mean the path did not stay inside the shape we validated. */
    case ELOOP:
    case ENOTDIR:
        return KPA_SECURITY;
    case ENOENT:
        return KPA_NOT_FOUND;
    case ENAMETOOLONG:
        return KPA_TOO_LARGE;
    case ENOMEM:
        return KPA_NO_MEMORY;
    default:
        break;
    }
    return KPA_IO;
}

static kpa_result result_from_state(kilixstate_result state)
{
    switch (state) {
    case KILIXSTATE_OK:                return KPA_OK;
    case KILIXSTATE_NOT_FOUND:         return KPA_NOT_FOUND;
    case KILIXSTATE_TOO_LARGE:         return KPA_TOO_LARGE;
    case KILIXSTATE_CORRUPT:           return KPA_CORRUPT;
    case KILIXSTATE_SECURITY:          return KPA_SECURITY;
    case KILIXSTATE_IO_ERROR:          return KPA_IO;
    case KILIXSTATE_BUFFER_TOO_SMALL:  return KPA_TOO_LARGE;
    case KILIXSTATE_INVALID:
    case KILIXSTATE_NOT_INITIALIZED:
    default:
        break;
    }
    return KPA_INVALID_ARGUMENT;
}

/* --------------------------------------------------------------- paths */

/*
 * paths.py:_absolute_env.  An unset or empty variable is absent; a relative
 * one is an error rather than something to resolve against the process
 * working directory, which is not the user's private store and is not stable
 * across the surfaces that share this reader.
 */
static kpa_result absolute_env(const char *name, const char **out)
{
    const char *value = getenv(name);

    *out = NULL;
    if (value == NULL || value[0] == '\0') return KPA_OK;
    if (value[0] != '/') return KPA_INVALID_ARGUMENT;
    *out = value;
    return KPA_OK;
}

static kpa_result path_assign(char *path, size_t capacity, const char *value)
{
    const size_t length = strlen(value);

    if (length + 1u > capacity) return KPA_TOO_LARGE;
    memcpy(path, value, length + 1u);
    return KPA_OK;
}

/* Append one component the way pathlib does: trailing slashes on the left
 * side collapse, and "/" stays a single separator. */
static kpa_result path_append(char *path, size_t capacity,
                              const char *component)
{
    size_t length = strlen(path);
    const size_t added = strlen(component);

    while (length > 1u && path[length - 1u] == '/') path[--length] = '\0';
    if (length + 1u + added + 1u > capacity) return KPA_TOO_LARGE;
    if (length != 1u || path[0] != '/') path[length++] = '/';
    memcpy(path + length, component, added);
    path[length + added] = '\0';
    return KPA_OK;
}

static kpa_result home_directory(char *out, size_t capacity)
{
    const char *home = getenv("HOME");
    struct passwd entry;
    struct passwd *found = NULL;
    char buffer[1024];

    if (home != NULL && home[0] == '/') return path_assign(out, capacity, home);
    /* A relative HOME cannot name a private store; an empty one is unset. */
    if (home != NULL && home[0] != '\0') return KPA_INVALID_ARGUMENT;
    if (getpwuid_r(getuid(), &entry, buffer, sizeof buffer, &found) != 0 ||
        found == NULL || found->pw_dir == NULL || found->pw_dir[0] != '/')
        return KPA_NOT_FOUND;
    return path_assign(out, capacity, found->pw_dir);
}

kpa_result kpa_projects_directory(char *out, size_t out_size)
{
    char root[KPA_ROOT_CAPACITY];
    const char *value;
    kpa_result result;

    if (out == NULL || out_size == 0u) return KPA_INVALID_ARGUMENT;
    out[0] = '\0';
    result = absolute_env("KILIX_PLAYALONG_DATA_HOME", &value);
    if (result != KPA_OK) return result;
    if (value != NULL) {
        /* paths.py returns the override as the data home itself; the
         * application directory is not appended to it. */
        result = path_assign(root, sizeof root, value);
    } else {
        result = absolute_env("XDG_DATA_HOME", &value);
        if (result != KPA_OK) return result;
        if (value != NULL) {
            result = path_assign(root, sizeof root, value);
        } else {
            result = home_directory(root, sizeof root);
            if (result == KPA_OK)
                result = path_append(root, sizeof root, ".local");
            if (result == KPA_OK)
                result = path_append(root, sizeof root, "share");
        }
        if (result == KPA_OK)
            result = path_append(root, sizeof root, KPA_APP_DIRECTORY);
    }
    if (result != KPA_OK) return result;
    result = path_append(root, sizeof root, KPA_PROJECTS_DIRECTORY);
    if (result != KPA_OK) return result;
    return path_assign(out, out_size, root);
}

bool kpa_project_id_valid(const char *project_id)
{
    size_t length;
    size_t index;

    if (project_id == NULL) return false;
    length = strlen(project_id);
    /* paths.py:_PROJECT_ID is ^[a-z0-9][a-z0-9-]{7,63}$. */
    if (length < 8u || length > 64u) return false;
    if (length + 1u > KPA_ID_CAPACITY) return false;
    if ((project_id[0] < 'a' || project_id[0] > 'z') &&
        (project_id[0] < '0' || project_id[0] > '9')) return false;
    for (index = 1u; index < length; index++) {
        const char byte = project_id[index];
        if ((byte < 'a' || byte > 'z') && (byte < '0' || byte > '9') &&
            byte != '-') return false;
    }
    return true;
}

/*
 * Syntax of a manifest-supplied relative path, checked before any of it
 * reaches the kernel.  Length failures report KPA_TOO_LARGE; everything a
 * traversal needs -- a leading slash, an empty component, "." or ".." -- is
 * KPA_SECURITY.  An embedded NUL cannot arrive here because the JSON reader
 * refuses to copy a string containing one into a C buffer.
 */
static kpa_result validate_relative_path(const char *path)
{
    size_t components = 0u;
    size_t index = 0u;

    if (path == NULL || path[0] == '\0') return KPA_SECURITY;
    if (strlen(path) + 1u > KPA_PATH_CAPACITY) return KPA_TOO_LARGE;
    if (path[0] == '/') return KPA_SECURITY;
    while (path[index] != '\0') {
        size_t length = 0u;

        while (path[index + length] != '\0' && path[index + length] != '/')
            length++;
        if (length == 0u) return KPA_SECURITY;      /* "a//b" or "a/" */
        if (length > (size_t)NAME_MAX) return KPA_TOO_LARGE;
        if (length == 1u && path[index] == '.') return KPA_SECURITY;
        if (length == 2u && path[index] == '.' && path[index + 1u] == '.')
            return KPA_SECURITY;
        for (size_t offset = 0u; offset < length; offset++) {
            const unsigned char byte = (unsigned char)path[index + offset];
            if (byte < 0x20u || byte == 0x7fu) return KPA_SECURITY;
        }
        components++;
        if (components > KPA_MAX_PATH_COMPONENTS) return KPA_TOO_LARGE;
        index += length;
        if (path[index] != '/') continue;
        index++;
        /* A trailing separator is an empty last component, and it is how a
         * directory gets asked for where a file was promised. */
        if (path[index] == '\0') return KPA_SECURITY;
    }
    return KPA_OK;
}

int kpa_project_open_artifact(const kpa_project *project,
                              const char *relative_path, kpa_result *error)
{
    kpa_result discarded;
    struct stat status;
    const char *cursor;
    int current;
    bool owned = false;
    int fd = -1;

    if (error == NULL) error = &discarded;
    if (project == NULL || project->directory_fd < 0 ||
        relative_path == NULL) {
        *error = KPA_INVALID_ARGUMENT;
        return -1;
    }
    *error = validate_relative_path(relative_path);
    if (*error != KPA_OK) return -1;

    current = project->directory_fd;
    cursor = relative_path;
    for (;;) {
        char component[NAME_MAX + 1u];
        const char *slash = strchr(cursor, '/');
        const size_t length = (slash != NULL) ? (size_t)(slash - cursor)
                                              : strlen(cursor);
        int next;

        memcpy(component, cursor, length);
        component[length] = '\0';
        if (slash == NULL) {
            /* O_NONBLOCK so a fifo planted in the project cannot park the
             * caller in open(); the S_ISREG check below then refuses it. */
            fd = openat(current, component,
                        O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
            if (fd < 0) *error = result_from_open_errno(errno);
            break;
        }
        next = openat(current, component,
                      O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
        if (next < 0) {
            *error = result_from_open_errno(errno);
            break;
        }
        if (owned) (void)close(current);
        current = next;
        owned = true;
        cursor = slash + 1;
    }
    if (owned) (void)close(current);
    if (fd < 0) return -1;
    if (fstat(fd, &status) != 0) {
        (void)close(fd);
        *error = KPA_IO;
        return -1;
    }
    /* A device, a fifo or another user's file inside a private project is
     * not an artifact this project produced. */
    if (!S_ISREG(status.st_mode) || status.st_uid != geteuid()) {
        (void)close(fd);
        *error = KPA_SECURITY;
        return -1;
    }
    *error = KPA_OK;
    return fd;
}

/* Read a whole artifact, refusing an oversized one before allocating. */
static kpa_result read_document(const kpa_project *project,
                                const char *relative, size_t limit,
                                char **out_bytes, size_t *out_size)
{
    kpa_result result;
    struct stat status;
    char *bytes;
    size_t size;
    size_t offset = 0u;
    int fd;

    *out_bytes = NULL;
    *out_size = 0u;
    fd = kpa_project_open_artifact(project, relative, &result);
    if (fd < 0) return result;
    if (fstat(fd, &status) != 0) {
        (void)close(fd);
        return KPA_IO;
    }
    if ((uintmax_t)status.st_size > (uintmax_t)limit) {
        (void)close(fd);
        return KPA_TOO_LARGE;
    }
    size = (size_t)status.st_size;
    bytes = malloc(size + 1u);
    if (bytes == NULL) {
        (void)close(fd);
        return KPA_NO_MEMORY;
    }
    while (offset < size) {
        const ssize_t count = read(fd, bytes + offset, size - offset);
        if (count > 0) {
            offset += (size_t)count;
        } else if (count == 0 || errno != EINTR) {
            free(bytes);
            (void)close(fd);
            return (count == 0) ? KPA_CORRUPT : KPA_IO;
        }
    }
    {
        /* A file that grew between the fstat and here is not the file we
         * sized, and its tail was never bounded. */
        char extra;
        ssize_t count;

        do {
            count = read(fd, &extra, 1u);
        } while (count < 0 && errno == EINTR);
        if (count != 0) {
            free(bytes);
            (void)close(fd);
            return (count > 0) ? KPA_CORRUPT : KPA_IO;
        }
    }
    (void)close(fd);
    bytes[size] = '\0';
    *out_bytes = bytes;
    *out_size = size;
    return KPA_OK;
}

/* ---------------------------------------------------------------- arena */

typedef struct kpa_arena {
    kpa_json_document document;
    kpa_json_node *nodes;
    char *scratch;
} kpa_arena;

static void arena_free(kpa_arena *arena)
{
    free(arena->nodes);
    free(arena->scratch);
    arena->nodes = NULL;
    arena->scratch = NULL;
    kpa_json_document_init(&arena->document, NULL, 0u, NULL, 0u);
}

/*
 * One node per two input bytes is an upper bound on any document, since the
 * shortest value plus its separator is two bytes; `cap` is what the declared
 * KPA_MAX_* counts could need.  Decoded escapes are never longer than the
 * escape they came from, so the input length also bounds the scratch tail.
 */
static kpa_result arena_init(kpa_arena *arena, size_t length, uint32_t cap)
{
    size_t nodes = length / 2u + 8u;

    arena->nodes = NULL;
    arena->scratch = NULL;
    if (nodes > (size_t)cap) nodes = (size_t)cap;
    if (nodes > SIZE_MAX / sizeof *arena->nodes) return KPA_TOO_LARGE;
    arena->nodes = malloc(nodes * sizeof *arena->nodes);
    arena->scratch = malloc(length + 1u);
    if (arena->nodes == NULL || arena->scratch == NULL) {
        arena_free(arena);
        return KPA_NO_MEMORY;
    }
    kpa_json_document_init(&arena->document, arena->nodes, (uint32_t)nodes,
                           arena->scratch, length + 1u);
    return KPA_OK;
}

static kpa_result result_from_json(kpa_json_result result)
{
    /* An arena exhausted by a document inside the size limit means the
     * document declares more structure than the bounds allow. */
    if (result == KPA_JSON_NO_SPACE) return KPA_TOO_LARGE;
    if (result == KPA_JSON_INVALID_ARGUMENT) return KPA_INVALID_ARGUMENT;
    return (result == KPA_JSON_OK) ? KPA_OK : KPA_CORRUPT;
}

/* ------------------------------------------------------- json accessors */

static const kpa_json_node *node_first_child(const kpa_json_document *document,
                                             const kpa_json_node *node)
{
    if (node == NULL || node->first_child == 0u) return NULL;
    return kpa_json_at(document, node->first_child);
}

static const kpa_json_node *node_next(const kpa_json_document *document,
                                      const kpa_json_node *node)
{
    if (node == NULL || node->next_sibling == 0u) return NULL;
    return kpa_json_at(document, node->next_sibling);
}

/*
 * Sibling iteration, not kpa_json_element: the accessor walks from the first
 * child on every call, so indexing a 65536 element array with it is
 * quadratic and this reader is allowed 65536 element arrays.
 */
#define KPA_FOR_EACH(document, container, item)                               \
    for ((item) = node_first_child((document), (container)); (item) != NULL;  \
         (item) = node_next((document), (item)))

static const kpa_json_node *member_of(const kpa_json_document *document,
                                      const kpa_json_node *node,
                                      const char *key, kpa_json_type type)
{
    const kpa_json_node *found = kpa_json_member(document, node, key);

    return (found != NULL && found->type == type) ? found : NULL;
}

/* Absent members are the caller's business, so this reports the three
 * outcomes apart: missing, present but unusable, and copied. */
static kpa_result copy_text(const kpa_json_document *document,
                            const kpa_json_node *node, const char *key,
                            char *out, size_t out_size)
{
    const kpa_json_node *member = kpa_json_member(document, node, key);

    if (member == NULL) return KPA_NOT_FOUND;
    if (member->type != KPA_JSON_STRING) return KPA_CORRUPT;
    if (member->length + 1u > out_size) return KPA_TOO_LARGE;
    if (!kpa_json_string_copy(document, node, key, out, out_size))
        return KPA_SECURITY;   /* the only remaining refusal is a NUL byte */
    return KPA_OK;
}

static kpa_result copy_required_text(const kpa_json_document *document,
                                     const kpa_json_node *node,
                                     const char *key, char *out,
                                     size_t out_size)
{
    const kpa_result result = copy_text(document, node, key, out, out_size);

    return (result == KPA_NOT_FOUND) ? KPA_CORRUPT : result;
}

static kpa_result copy_relative_path(const kpa_json_document *document,
                                     const kpa_json_node *node,
                                     const char *key, char *out,
                                     size_t out_size)
{
    const kpa_result result = copy_text(document, node, key, out, out_size);

    if (result != KPA_OK) return result;
    return validate_relative_path(out);
}

static bool number_of(const kpa_json_document *document,
                      const kpa_json_node *node, const char *key,
                      double *out)
{
    double value;

    if (!kpa_json_number(document, node, key, &value)) return false;
    if (!isfinite(value)) return false;
    *out = value;
    return true;
}

/* A JSON number used as a count or a size has to be an exact non-negative
 * integer; 3.5 tracks and 1e300 bytes are both refusals, not roundings. */
static bool integer_of(const kpa_json_document *document,
                       const kpa_json_node *node, const char *key,
                       uint64_t limit, uint64_t *out)
{
    double value;
    uint64_t integer;

    if (limit > KPA_MAX_EXACT_INTEGER) return false;
    if (!number_of(document, node, key, &value)) return false;
    if (!(value >= 0.0) || !(value <= (double)limit)) return false;
    integer = (uint64_t)value;
    if ((double)integer != value) return false;
    *out = integer;
    return true;
}

/* ------------------------------------------------------------- manifest */

static bool text_ends_with(const char *text, const char *suffix)
{
    const size_t length = strlen(text);
    const size_t tail = strlen(suffix);

    return length >= tail && strcmp(text + length - tail, suffix) == 0;
}

static kpa_result parse_track(const kpa_json_document *document,
                              const kpa_json_node *node, kpa_track *track)
{
    uint64_t size = 0u;
    kpa_result result;
    bool muted = false;

    if (node->type != KPA_JSON_OBJECT) return KPA_CORRUPT;
    result = copy_required_text(document, node, "id", track->id,
                                sizeof track->id);
    if (result == KPA_OK)
        result = copy_required_text(document, node, "label", track->label,
                                    sizeof track->label);
    if (result == KPA_OK)
        result = copy_required_text(document, node, "kind", track->kind,
                                    sizeof track->kind);
    if (result == KPA_OK)
        result = copy_relative_path(document, node, "path", track->path,
                                    sizeof track->path);
    if (result == KPA_NOT_FOUND) result = KPA_CORRUPT;
    if (result != KPA_OK) return result;
    /* state.py:_valid_track wants the digest present and a string; the reader
     * has nowhere to put it and no bytes hashed to compare it against. */
    if (member_of(document, node, "sha256", KPA_JSON_STRING) == NULL)
        return KPA_CORRUPT;
    if (!integer_of(document, node, "size", KPA_MAX_EXACT_INTEGER, &size))
        return KPA_CORRUPT;
    if (!kpa_json_bool(document, node, "default_muted", &muted))
        return KPA_CORRUPT;
    track->size = size;
    track->default_muted = muted;
    return KPA_OK;
}

static kpa_result parse_tracks(const kpa_json_document *document,
                               const kpa_json_node *root,
                               kpa_project *project)
{
    const kpa_json_node *tracks = member_of(document, root, "tracks",
                                            KPA_JSON_ARRAY);
    const kpa_json_node *item;

    if (tracks == NULL) return KPA_CORRUPT;
    if (tracks->child_count > KPA_MAX_TRACKS) return KPA_TOO_LARGE;
    KPA_FOR_EACH(document, tracks, item) {
        kpa_result result;

        /* The chain cannot be longer than the count it was built from, but
         * the array this writes into is the one an attacker would want. */
        if (project->track_count >= tracks->child_count) return KPA_CORRUPT;
        result = parse_track(document, item,
                             &project->tracks[project->track_count]);
        if (result != KPA_OK) return result;
        project->track_count++;
    }
    return KPA_OK;
}

static kpa_result stage_status_of(const kpa_json_document *document,
                                  const kpa_json_node *stage,
                                  kpa_stage_status *out)
{
    const kpa_json_node *status = member_of(document, stage, "status",
                                            KPA_JSON_STRING);

    if (status == NULL) return KPA_CORRUPT;
    if (kpa_json_string_equals(status, "pending"))
        *out = KPA_STAGE_PENDING;
    else if (kpa_json_string_equals(status, "running"))
        *out = KPA_STAGE_RUNNING;
    else if (kpa_json_string_equals(status, "done"))
        *out = KPA_STAGE_DONE;
    else if (kpa_json_string_equals(status, "error"))
        *out = KPA_STAGE_ERROR;
    else return KPA_CORRUPT;
    return KPA_OK;
}

/*
 * Every artifact path a stage records is validated even though this reader
 * only ever opens a handful of them: a manifest that carries an escape in a
 * stage it will never run is one to refuse, not one to half-read.
 */
static kpa_result parse_stage_artifacts(const kpa_json_document *document,
                                        const kpa_json_node *stage,
                                        char *printable, size_t printable_size)
{
    const kpa_json_node *artifacts = member_of(document, stage, "artifacts",
                                               KPA_JSON_ARRAY);
    const kpa_json_node *item;

    if (artifacts == NULL) return KPA_CORRUPT;
    KPA_FOR_EACH(document, artifacts, item) {
        char path[KPA_PATH_CAPACITY];
        kpa_result result;

        if (item->type != KPA_JSON_OBJECT) return KPA_CORRUPT;
        result = copy_relative_path(document, item, "path", path,
                                    sizeof path);
        if (result == KPA_NOT_FOUND) result = KPA_CORRUPT;
        if (result != KPA_OK) return result;
        if (printable != NULL && printable[0] == '\0' &&
            text_ends_with(path, ".html"))
            (void)path_assign(printable, printable_size, path);
    }
    return KPA_OK;
}

static kpa_result parse_stages(const kpa_json_document *document,
                               const kpa_json_node *root,
                               kpa_project *project)
{
    const kpa_json_node *stages = member_of(document, root, "stages",
                                            KPA_JSON_OBJECT);
    size_t index;

    if (stages == NULL) return KPA_CORRUPT;
    for (index = 0u; index < KPA_STAGE_COUNT; index++) {
        const kpa_json_node *stage = member_of(document, stages,
                                               stage_names[index],
                                               KPA_JSON_OBJECT);
        kpa_result result;

        /* A stage name this build does not know is ignored, but every stage
         * it does know has to be there: state.py writes all seven. */
        if (stage == NULL) return KPA_CORRUPT;
        result = stage_status_of(document, stage, &project->stages[index]);
        if (result != KPA_OK) return result;
        result = parse_stage_artifacts(document, stage,
                                       (index == KPA_STAGE_EXPORT)
                                           ? project->printable_path : NULL,
                                       sizeof project->printable_path);
        if (result != KPA_OK) return result;
    }
    project->stage_count = (uint32_t)KPA_STAGE_COUNT;
    if (project->stages[KPA_STAGE_EXPORT] != KPA_STAGE_DONE)
        project->printable_path[0] = '\0';
    else if (project->printable_path[0] == '\0')
        (void)path_assign(project->printable_path,
                          sizeof project->printable_path,
                          KPA_DEFAULT_PRINTABLE);
    return KPA_OK;
}

/* lyrics and tablature are objects or null; state.py treats a missing key the
 * same as null, so absent is absent rather than corrupt. */
static kpa_result optional_section(const kpa_json_document *document,
                                   const kpa_json_node *root, const char *key,
                                   const kpa_json_node **out)
{
    const kpa_json_node *section = kpa_json_member(document, root, key);

    *out = NULL;
    if (section == NULL || section->type == KPA_JSON_NULL) return KPA_OK;
    if (section->type != KPA_JSON_OBJECT) return KPA_CORRUPT;
    *out = section;
    return KPA_OK;
}

static kpa_result parse_sections(const kpa_json_document *document,
                                 const kpa_json_node *root,
                                 kpa_project *project)
{
    const kpa_json_node *section;
    kpa_result result;

    result = optional_section(document, root, "lyrics", &section);
    if (result != KPA_OK) return result;
    if (section != NULL) {
        result = copy_relative_path(document, section, "path",
                                    project->lyrics_path,
                                    sizeof project->lyrics_path);
        if (result == KPA_NOT_FOUND) result = KPA_CORRUPT;
        if (result != KPA_OK) return result;
        project->has_lyrics = true;
    }
    result = optional_section(document, root, "tablature", &section);
    if (result != KPA_OK) return result;
    if (section == NULL) return KPA_OK;
    result = copy_relative_path(document, section, "path", project->tab_path,
                                sizeof project->tab_path);
    if (result == KPA_NOT_FOUND) result = KPA_CORRUPT;
    if (result != KPA_OK) return result;
    project->has_tab = true;
    /* pipeline.py records both of these; an older manifest that predates them
     * gets the conventional path, which still has to survive the same walk. */
    result = copy_relative_path(document, section, "ascii_path",
                                project->ascii_tab_path,
                                sizeof project->ascii_tab_path);
    if (result == KPA_NOT_FOUND)
        result = path_assign(project->ascii_tab_path,
                             sizeof project->ascii_tab_path,
                             KPA_DEFAULT_ASCII_TAB);
    if (result != KPA_OK) return result;
    result = copy_relative_path(document, section, "midi_path",
                                project->midi_path, sizeof project->midi_path);
    if (result == KPA_NOT_FOUND)
        result = path_assign(project->midi_path, sizeof project->midi_path,
                             KPA_DEFAULT_MIDI);
    return result;
}

static kpa_result parse_manifest(kpa_project *project, const char *bytes,
                                 size_t size, const char *project_id)
{
    kpa_arena arena;
    const kpa_json_node *root;
    const kpa_json_node *schema;
    const kpa_json_node *source;
    kpa_result result;
    double duration = 0.0;

    result = arena_init(&arena, size, KPA_MANIFEST_NODE_CAP);
    if (result != KPA_OK) return result;
    result = result_from_json(kpa_json_parse(&arena.document, bytes, size));
    if (result != KPA_OK) goto done;
    root = kpa_json_root(&arena.document);
    if (root == NULL || root->type != KPA_JSON_OBJECT) {
        result = KPA_CORRUPT;
        goto done;
    }
    schema = member_of(&arena.document, root, "schema", KPA_JSON_STRING);
    if (schema == NULL) {
        result = KPA_CORRUPT;
        goto done;
    }
    if (!kpa_json_string_equals(schema, KPA_PROJECT_SCHEMA)) {
        result = KPA_SCHEMA;
        goto done;
    }
    result = copy_required_text(&arena.document, root, "id", project->id,
                                sizeof project->id);
    if (result != KPA_OK) goto done;
    /* The directory name is the authority: a manifest that claims another
     * project's id would let one project answer for another. */
    if (!kpa_project_id_valid(project->id) ||
        strcmp(project->id, project_id) != 0) {
        result = KPA_CORRUPT;
        goto done;
    }
    result = copy_required_text(&arena.document, root, "title",
                                project->title, sizeof project->title);
    if (result != KPA_OK) goto done;
    result = copy_required_text(&arena.document, root, "artist",
                                project->artist, sizeof project->artist);
    if (result != KPA_OK) goto done;
    source = member_of(&arena.document, root, "source", KPA_JSON_OBJECT);
    if (source == NULL) {
        result = KPA_CORRUPT;
        goto done;
    }
    if (kpa_json_member(&arena.document, source, "duration") != NULL) {
        if (!number_of(&arena.document, source, "duration", &duration) ||
            duration < 0.0) {
            result = KPA_CORRUPT;
            goto done;
        }
    }
    project->duration = duration;
    result = parse_tracks(&arena.document, root, project);
    if (result != KPA_OK) goto done;
    result = parse_stages(&arena.document, root, project);
    if (result != KPA_OK) goto done;
    result = parse_sections(&arena.document, root, project);

done:
    arena_free(&arena);
    return result;
}

/*
 * Confirm that the store resolves where this reader believes it does before
 * any of its bytes are trusted: base_directory/app_id/filename, which is
 * projects/<id>/project.state.
 */
static kpa_result confirm_store_path(kilixstate_store *store,
                                     const char *projects,
                                     const char *project_id)
{
    char expected[KPA_ROOT_CAPACITY];
    char actual[KPA_ROOT_CAPACITY];
    kpa_result result;

    result = path_assign(expected, sizeof expected, projects);
    if (result == KPA_OK)
        result = path_append(expected, sizeof expected, project_id);
    if (result == KPA_OK)
        result = path_append(expected, sizeof expected, KPA_MANIFEST_NAME);
    if (result != KPA_OK) return result;
    if (kilixstate_store_path(store, actual, sizeof actual) != KILIXSTATE_OK)
        return KPA_IO;
    return (strcmp(expected, actual) == 0) ? KPA_OK : KPA_SECURITY;
}

static kpa_result load_manifest(kpa_project *project, const char *projects,
                                const char *project_id)
{
    kilixstate_options options;
    kilixstate_store store;
    kilixstate_result state;
    kpa_result result;
    char *bytes = NULL;
    size_t required = 0u;
    size_t size = 0u;

    kilixstate_options_init(&options);
    options.app_id = project_id;
    options.filename = KPA_MANIFEST_NAME;
    options.base_directory = projects;
    options.max_payload = KPA_MAX_MANIFEST_BYTES;
    options.format = KILIXSTATE_FORMAT_CRC32;
    state = kilixstate_store_init(&store, &options);
    if (state != KILIXSTATE_OK) return result_from_state(state);
    result = confirm_store_path(&store, projects, project_id);
    if (result != KPA_OK) {
        kilixstate_store_close(&store);
        return result;
    }
    /* Size the payload first so an oversized manifest is refused by the
     * store's own bound instead of being read into memory here. */
    state = kilixstate_load(&store, NULL, 0u, &required);
    if (state != KILIXSTATE_OK && state != KILIXSTATE_BUFFER_TOO_SMALL) {
        kilixstate_store_close(&store);
        return result_from_state(state);
    }
    if (required > KPA_MAX_MANIFEST_BYTES) {
        kilixstate_store_close(&store);
        return KPA_TOO_LARGE;
    }
    bytes = malloc(required + 1u);
    if (bytes == NULL) {
        kilixstate_store_close(&store);
        return KPA_NO_MEMORY;
    }
    state = kilixstate_load(&store, bytes, required + 1u, &size);
    kilixstate_store_close(&store);
    if (state != KILIXSTATE_OK) {
        free(bytes);
        return result_from_state(state);
    }
    bytes[size] = '\0';
    result = parse_manifest(project, bytes, size, project_id);
    free(bytes);
    return result;
}

kpa_result kpa_project_open(kpa_project *project, const char *project_id)
{
    char projects[KPA_ROOT_CAPACITY];
    struct stat status;
    kpa_result result;
    int projects_fd;
    int directory_fd;

    if (project == NULL) return KPA_INVALID_ARGUMENT;
    memset(project, 0, sizeof *project);
    project->directory_fd = -1;
    if (!kpa_project_id_valid(project_id)) return KPA_INVALID_ARGUMENT;
    result = kpa_projects_directory(projects, sizeof projects);
    if (result != KPA_OK) return result;
    projects_fd = open(projects, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (projects_fd < 0) return result_from_open_errno(errno);
    /*
     * Open the project directory before the store does.  kilix-state creates
     * the directories it resolves, so asking it about a project that does not
     * exist would create one; this refuses first and never writes.
     */
    directory_fd = openat(projects_fd, project_id,
                          O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    (void)close(projects_fd);
    if (directory_fd < 0) return result_from_open_errno(errno);
    if (fstat(directory_fd, &status) != 0) {
        (void)close(directory_fd);
        return KPA_IO;
    }
    if (status.st_uid != geteuid()) {
        (void)close(directory_fd);
        return KPA_SECURITY;
    }
    project->directory_fd = directory_fd;
    result = load_manifest(project, projects, project_id);
    if (result != KPA_OK) kpa_project_close(project);
    return result;
}

void kpa_project_close(kpa_project *project)
{
    if (project == NULL) return;
    if (project->directory_fd >= 0) (void)close(project->directory_fd);
    memset(project, 0, sizeof *project);
    /* Zero is stdin, so the invalid descriptor has to be written back after
     * the wipe rather than left as the memset's idea of empty. */
    project->directory_fd = -1;
}

/* --------------------------------------------------------------- lyrics */

/*
 * Cue and word text is copied once into a single owned block.  Decoding an
 * escape never produces more bytes than the escape occupied, and the raw
 * strings do not overlap, so the document length plus one terminator per
 * string bounds the block exactly.
 */
typedef struct kpa_text_writer {
    char *bytes;
    size_t capacity;
    size_t used;
} kpa_text_writer;

static kpa_result text_take(kpa_text_writer *writer,
                            const kpa_json_document *document,
                            const kpa_json_node *node, const char *key,
                            const char **out_text, uint32_t *out_length)
{
    const kpa_json_node *member = member_of(document, node, key,
                                            KPA_JSON_STRING);
    size_t length;

    if (member == NULL) return KPA_CORRUPT;
    length = member->length;
    if (length > UINT32_MAX) return KPA_TOO_LARGE;
    if (length + 1u > writer->capacity - writer->used) return KPA_TOO_LARGE;
    if (length > 0u) memcpy(writer->bytes + writer->used, member->text, length);
    writer->bytes[writer->used + length] = '\0';
    *out_text = writer->bytes + writer->used;
    *out_length = (uint32_t)length;
    writer->used += length + 1u;
    return KPA_OK;
}

/* start and end of one timed item, with the ordering rule that makes the
 * binary searches meaningful. */
static kpa_result timed_span(const kpa_json_document *document,
                             const kpa_json_node *node, double previous_start,
                             double *out_start, double *out_end)
{
    double start;
    double end;

    if (!number_of(document, node, "start", &start) ||
        !number_of(document, node, "end", &end))
        return KPA_CORRUPT;
    if (end < start) return KPA_CORRUPT;
    /* Rejected, not sorted on load: renumbering the array would make index N
     * here mean a different cue than index N in state.py or the player. */
    if (start < previous_start) return KPA_CORRUPT;
    *out_start = start;
    *out_end = end;
    return KPA_OK;
}

static kpa_result count_lyrics(const kpa_json_document *document,
                               const kpa_json_node *cues,
                               uint32_t *out_words)
{
    const kpa_json_node *cue;
    uint64_t words = 0u;

    if (cues->child_count > KPA_MAX_CUES) return KPA_TOO_LARGE;
    KPA_FOR_EACH(document, cues, cue) {
        const kpa_json_node *list;

        if (cue->type != KPA_JSON_OBJECT) return KPA_CORRUPT;
        list = kpa_json_member(document, cue, "words");
        if (list == NULL) continue;
        if (list->type != KPA_JSON_ARRAY) return KPA_CORRUPT;
        words += list->child_count;
        if (words > KPA_MAX_WORDS) return KPA_TOO_LARGE;
    }
    *out_words = (uint32_t)words;
    return KPA_OK;
}

static kpa_result fill_words(const kpa_json_document *document,
                             const kpa_json_node *cue, kpa_lyrics *lyrics,
                             uint32_t capacity, kpa_text_writer *writer,
                             kpa_cue *out)
{
    const kpa_json_node *list = kpa_json_member(document, cue, "words");
    const kpa_json_node *item;
    double previous = -HUGE_VAL;

    out->first_word = lyrics->word_count;
    out->word_count = 0u;
    if (list == NULL) return KPA_OK;
    KPA_FOR_EACH(document, list, item) {
        kpa_word *word;
        kpa_result result;

        if (item->type != KPA_JSON_OBJECT) return KPA_CORRUPT;
        if (lyrics->word_count >= capacity) return KPA_CORRUPT;
        word = &lyrics->words[lyrics->word_count];
        result = timed_span(document, item, previous, &word->start,
                            &word->end);
        if (result != KPA_OK) return result;
        previous = word->start;
        result = text_take(writer, document, item, "text", &word->text,
                           &word->length);
        if (result != KPA_OK) return result;
        lyrics->word_count++;
        out->word_count++;
    }
    return KPA_OK;
}

static kpa_result parse_lyrics(const kpa_json_document *document,
                               kpa_lyrics *lyrics, size_t size)
{
    const kpa_json_node *root = kpa_json_root(document);
    const kpa_json_node *schema;
    const kpa_json_node *cues;
    const kpa_json_node *item;
    kpa_text_writer writer;
    uint32_t word_count = 0u;
    double previous = -HUGE_VAL;
    kpa_result result;

    if (root == NULL || root->type != KPA_JSON_OBJECT) return KPA_CORRUPT;
    schema = member_of(document, root, "schema", KPA_JSON_STRING);
    if (schema == NULL) return KPA_CORRUPT;
    if (!kpa_json_string_equals(schema, KPA_LYRICS_SCHEMA)) return KPA_SCHEMA;
    cues = member_of(document, root, "cues", KPA_JSON_ARRAY);
    if (cues == NULL) return KPA_CORRUPT;
    result = copy_text(document, root, "language", lyrics->language,
                       sizeof lyrics->language);
    if (result != KPA_OK && result != KPA_NOT_FOUND) return result;
    result = copy_text(document, root, "source", lyrics->source,
                       sizeof lyrics->source);
    if (result != KPA_OK && result != KPA_NOT_FOUND) return result;
    result = count_lyrics(document, cues, &word_count);
    if (result != KPA_OK) return result;

    writer.capacity = size + (size_t)cues->child_count + word_count + 1u;
    writer.used = 0u;
    writer.bytes = malloc(writer.capacity);
    lyrics->text_bytes = writer.bytes;
    lyrics->cues = calloc(cues->child_count + 1u, sizeof *lyrics->cues);
    lyrics->words = calloc((size_t)word_count + 1u, sizeof *lyrics->words);
    if (writer.bytes == NULL || lyrics->cues == NULL || lyrics->words == NULL)
        return KPA_NO_MEMORY;

    KPA_FOR_EACH(document, cues, item) {
        kpa_cue *cue;

        if (lyrics->cue_count >= cues->child_count) return KPA_CORRUPT;
        cue = &lyrics->cues[lyrics->cue_count];
        result = timed_span(document, item, previous, &cue->start, &cue->end);
        if (result != KPA_OK) return result;
        previous = cue->start;
        result = text_take(&writer, document, item, "text", &cue->text,
                           &cue->length);
        if (result != KPA_OK) return result;
        result = fill_words(document, item, lyrics, word_count, &writer,
                            cue);
        if (result != KPA_OK) return result;
        lyrics->cue_count++;
    }
    lyrics->text_size = writer.used;
    return KPA_OK;
}

kpa_result kpa_lyrics_load(const kpa_project *project, kpa_lyrics *lyrics)
{
    kpa_arena arena;
    kpa_result result;
    char *bytes = NULL;
    size_t size = 0u;

    if (lyrics == NULL) return KPA_INVALID_ARGUMENT;
    memset(lyrics, 0, sizeof *lyrics);
    if (project == NULL || project->directory_fd < 0)
        return KPA_INVALID_ARGUMENT;
    if (!project->has_lyrics) return KPA_NOT_FOUND;
    result = read_document(project, project->lyrics_path,
                           KPA_MAX_DOCUMENT_BYTES, &bytes, &size);
    if (result != KPA_OK) return result;
    result = arena_init(&arena, size, KPA_LYRICS_NODE_CAP);
    if (result == KPA_OK) {
        result = result_from_json(kpa_json_parse(&arena.document, bytes,
                                                 size));
        if (result == KPA_OK) result = parse_lyrics(&arena.document, lyrics,
                                                    size);
        arena_free(&arena);
    }
    free(bytes);
    if (result != KPA_OK) kpa_lyrics_free(lyrics);
    return result;
}

void kpa_lyrics_free(kpa_lyrics *lyrics)
{
    if (lyrics == NULL) return;
    free(lyrics->cues);
    free(lyrics->words);
    free(lyrics->text_bytes);
    memset(lyrics, 0, sizeof *lyrics);
}

/* ------------------------------------------------------------------ tab */

static kpa_result parse_tuning(const kpa_json_document *document,
                               const kpa_json_node *root, kpa_tab *tab)
{
    const kpa_json_node *tuning = member_of(document, root, "tuning",
                                            KPA_JSON_OBJECT);
    const kpa_json_node *midi;
    const kpa_json_node *labels;
    const kpa_json_node *item;
    uint64_t max_fret = 0u;
    uint32_t index = 0u;
    int32_t previous = -1;

    if (tuning == NULL) return KPA_CORRUPT;
    midi = member_of(document, tuning, "midi", KPA_JSON_ARRAY);
    labels = member_of(document, tuning, "labels", KPA_JSON_ARRAY);
    if (midi == NULL || labels == NULL) return KPA_CORRUPT;
    /* Understood, but this build draws exactly six strings. */
    if (midi->child_count != KPA_STRING_COUNT ||
        labels->child_count != KPA_STRING_COUNT) return KPA_UNSUPPORTED;
    if (!integer_of(document, tuning, "max_fret", 255u, &max_fret))
        return KPA_CORRUPT;
    KPA_FOR_EACH(document, midi, item) {
        double value;

        if (item->type != KPA_JSON_NUMBER || index >= KPA_STRING_COUNT)
            return KPA_CORRUPT;
        value = item->number;
        if (!isfinite(value) || value < 0.0 || value > 127.0 ||
            (double)(int32_t)value != value)
            return KPA_CORRUPT;
        /* pipeline.py rejects a tuning that is not strictly ascending, and
         * index 0 is the low E: the display inverts, this reader does not. */
        if ((int32_t)value <= previous) return KPA_CORRUPT;
        previous = (int32_t)value;
        tab->tuning_midi[index] = previous;
        index++;
    }
    index = 0u;
    KPA_FOR_EACH(document, labels, item) {
        if (item->type != KPA_JSON_STRING || index >= KPA_STRING_COUNT)
            return KPA_CORRUPT;
        if (item->length + 1u > sizeof tab->tuning_labels[index])
            return KPA_TOO_LARGE;
        if (item->length > 0u) {
            if (memchr(item->text, 0, item->length) != NULL)
                return KPA_SECURITY;
            memcpy(tab->tuning_labels[index], item->text, item->length);
        }
        tab->tuning_labels[index][item->length] = '\0';
        index++;
    }
    tab->string_count = KPA_STRING_COUNT;
    tab->max_fret = (uint32_t)max_fret;
    return KPA_OK;
}

static kpa_result count_tab(const kpa_json_document *document,
                            const kpa_json_node *events,
                            uint32_t *out_positions)
{
    const kpa_json_node *event;
    uint64_t positions = 0u;

    if (events->child_count > KPA_MAX_TAB_EVENTS) return KPA_TOO_LARGE;
    KPA_FOR_EACH(document, events, event) {
        const kpa_json_node *list;

        if (event->type != KPA_JSON_OBJECT) return KPA_CORRUPT;
        list = member_of(document, event, "positions", KPA_JSON_ARRAY);
        if (list == NULL) return KPA_CORRUPT;
        positions += list->child_count;
        if (positions > KPA_MAX_TAB_POSITIONS) return KPA_TOO_LARGE;
    }
    *out_positions = (uint32_t)positions;
    return KPA_OK;
}

static kpa_result fill_positions(const kpa_json_document *document,
                                 const kpa_json_node *event, kpa_tab *tab,
                                 uint32_t capacity, kpa_tab_event *out)
{
    const kpa_json_node *list = member_of(document, event, "positions",
                                          KPA_JSON_ARRAY);
    const kpa_json_node *item;

    out->first_position = tab->position_count;
    out->position_count = 0u;
    if (list == NULL) return KPA_CORRUPT;
    KPA_FOR_EACH(document, list, item) {
        kpa_tab_position *position;
        uint64_t string_index = 0u;
        uint64_t fret = 0u;
        uint64_t pitch = 0u;

        if (item->type != KPA_JSON_OBJECT) return KPA_CORRUPT;
        if (tab->position_count >= capacity) return KPA_CORRUPT;
        position = &tab->positions[tab->position_count];
        if (!integer_of(document, item, "string", tab->string_count - 1u,
                        &string_index) ||
            !integer_of(document, item, "fret", tab->max_fret, &fret) ||
            !integer_of(document, item, "pitch", 127u, &pitch))
            return KPA_CORRUPT;
        position->string_index = (uint8_t)string_index;
        position->fret = (uint8_t)fret;
        position->pitch = (uint8_t)pitch;
        tab->position_count++;
        out->position_count++;
    }
    return KPA_OK;
}

static kpa_result parse_tab(const kpa_json_document *document, kpa_tab *tab)
{
    const kpa_json_node *root = kpa_json_root(document);
    const kpa_json_node *schema;
    const kpa_json_node *events;
    const kpa_json_node *item;
    uint32_t position_count = 0u;
    double previous = -HUGE_VAL;
    kpa_result result;

    if (root == NULL || root->type != KPA_JSON_OBJECT) return KPA_CORRUPT;
    schema = member_of(document, root, "schema", KPA_JSON_STRING);
    if (schema == NULL) return KPA_CORRUPT;
    if (!kpa_json_string_equals(schema, KPA_TAB_SCHEMA)) return KPA_SCHEMA;
    result = parse_tuning(document, root, tab);
    if (result != KPA_OK) return result;
    events = member_of(document, root, "events", KPA_JSON_ARRAY);
    if (events == NULL) return KPA_CORRUPT;
    result = count_tab(document, events, &position_count);
    if (result != KPA_OK) return result;
    tab->events = calloc((size_t)events->child_count + 1u,
                         sizeof *tab->events);
    tab->positions = calloc((size_t)position_count + 1u,
                            sizeof *tab->positions);
    if (tab->events == NULL || tab->positions == NULL) return KPA_NO_MEMORY;
    KPA_FOR_EACH(document, events, item) {
        kpa_tab_event *event;

        if (tab->event_count >= events->child_count) return KPA_CORRUPT;
        event = &tab->events[tab->event_count];
        result = timed_span(document, item, previous, &event->start,
                            &event->end);
        if (result != KPA_OK) return result;
        previous = event->start;
        result = fill_positions(document, item, tab, position_count, event);
        if (result != KPA_OK) return result;
        /* The bound kpa_tab_first_after relies on: an event this far back
         * has already ended by the time this one starts. */
        if (tab->event_count >= KPA_TAB_MAX_OVERLAP &&
            tab->events[tab->event_count - KPA_TAB_MAX_OVERLAP].end >
                event->start)
            return KPA_CORRUPT;
        tab->event_count++;
    }
    return KPA_OK;
}

kpa_result kpa_tab_load(const kpa_project *project, kpa_tab *tab)
{
    kpa_arena arena;
    kpa_result result;
    char *bytes = NULL;
    size_t size = 0u;

    if (tab == NULL) return KPA_INVALID_ARGUMENT;
    memset(tab, 0, sizeof *tab);
    if (project == NULL || project->directory_fd < 0)
        return KPA_INVALID_ARGUMENT;
    if (!project->has_tab) return KPA_NOT_FOUND;
    result = read_document(project, project->tab_path, KPA_MAX_DOCUMENT_BYTES,
                           &bytes, &size);
    if (result != KPA_OK) return result;
    result = arena_init(&arena, size, KPA_TAB_NODE_CAP);
    if (result == KPA_OK) {
        result = result_from_json(kpa_json_parse(&arena.document, bytes,
                                                 size));
        if (result == KPA_OK) result = parse_tab(&arena.document, tab);
        arena_free(&arena);
    }
    free(bytes);
    if (result != KPA_OK) kpa_tab_free(tab);
    return result;
}

void kpa_tab_free(kpa_tab *tab)
{
    if (tab == NULL) return;
    free(tab->events);
    free(tab->positions);
    memset(tab, 0, sizeof *tab);
}

/* -------------------------------------------------------------- queries */

/* Number of leading items whose start is at or before `seconds`.  Load
 * verified the array is sorted by start, so this predicate is monotone. */
static uint32_t upper_bound_by_start(const void *items, size_t stride,
                                     uint32_t count, double seconds)
{
    const char *base = items;
    uint32_t low = 0u;
    uint32_t high = count;

    while (low < high) {
        const uint32_t probe = low + (high - low) / 2u;
        double start;

        memcpy(&start, base + (size_t)probe * stride, sizeof start);
        if (start <= seconds) low = probe + 1u;
        else high = probe;
    }
    return low;
}

/*
 * The active item is the last one that has started and has not ended, which
 * is what the browser player shows and the only answer that stays a binary
 * search when spans overlap: real captions do overlap, and the newest line is
 * the one being sung.  The span is half open, so a position exactly on an end
 * belongs to whatever comes next rather than to two items at once.
 */
static int32_t active_index(const void *items, size_t stride, uint32_t count,
                            double seconds)
{
    uint32_t index;
    double end;

    if (count == 0u) return -1;
    index = upper_bound_by_start(items, stride, count, seconds);
    if (index == 0u) return -1;
    index--;
    memcpy(&end, (const char *)items + (size_t)index * stride + sizeof(double),
           sizeof end);
    if (!(seconds < end)) return -1;
    return (int32_t)index;
}

int32_t kpa_lyrics_cue_at(const kpa_lyrics *lyrics, double seconds)
{
    if (lyrics == NULL || lyrics->cues == NULL) return -1;
    return active_index(lyrics->cues, sizeof *lyrics->cues,
                        lyrics->cue_count, seconds);
}

int32_t kpa_lyrics_word_at(const kpa_lyrics *lyrics, int32_t cue_index,
                           double seconds)
{
    const kpa_cue *cue;
    int32_t found;

    if (lyrics == NULL || lyrics->cues == NULL || lyrics->words == NULL ||
        cue_index < 0 || (uint32_t)cue_index >= lyrics->cue_count) return -1;
    cue = &lyrics->cues[cue_index];
    if (cue->word_count == 0u) return -1;
    found = active_index(&lyrics->words[cue->first_word],
                         sizeof *lyrics->words, cue->word_count, seconds);
    if (found < 0) return -1;
    /* Indices into kpa_lyrics.words, so a caller can hold one word without
     * carrying its cue around with it. */
    return (int32_t)(cue->first_word + (uint32_t)found);
}

uint32_t kpa_tab_first_after(const kpa_tab *tab, double seconds)
{
    uint32_t after;
    uint32_t index;

    if (tab == NULL || tab->events == NULL || tab->event_count == 0u)
        return 0u;
    after = upper_bound_by_start(tab->events, sizeof *tab->events,
                                 tab->event_count, seconds);
    /*
     * Everything from `after` on starts after `seconds` and so ends after it.
     * Earlier events can still be sounding, but load verified that an event
     * KPA_TAB_MAX_OVERLAP positions back has already ended by the time this
     * one starts, so the correction is a constant and not a scan of the song.
     */
    index = (after > KPA_TAB_MAX_OVERLAP) ? after - KPA_TAB_MAX_OVERLAP : 0u;
    for (; index < after; index++)
        if (tab->events[index].end > seconds) return index;
    return after;
}

/* -------------------------------------------------------------- listing */

typedef struct kpa_list_entry {
    char id[KPA_ID_CAPACITY];
    int64_t seconds;
    int64_t nanoseconds;
} kpa_list_entry;

/* pipeline.py:list_projects orders by directory mtime, newest first; the id
 * breaks ties so the same store always lists in the same order. */
static int compare_entries(const void *left, const void *right)
{
    const kpa_list_entry *first = left;
    const kpa_list_entry *second = right;

    if (first->seconds != second->seconds)
        return (first->seconds > second->seconds) ? -1 : 1;
    if (first->nanoseconds != second->nanoseconds)
        return (first->nanoseconds > second->nanoseconds) ? -1 : 1;
    return strcmp(first->id, second->id);
}

static kpa_result collect_entries(const char *projects,
                                  kpa_list_entry *entries, uint32_t capacity,
                                  uint32_t *out_count)
{
    DIR *stream = opendir(projects);
    const struct dirent *entry;
    uint32_t count = 0u;
    int directory_fd;

    *out_count = 0u;
    if (stream == NULL) return result_from_open_errno(errno);
    directory_fd = dirfd(stream);
    if (directory_fd < 0) {
        (void)closedir(stream);
        return KPA_IO;
    }
    while (count < capacity && (entry = readdir(stream)) != NULL) {
        struct stat status;

        if (!kpa_project_id_valid(entry->d_name)) continue;
        if (fstatat(directory_fd, entry->d_name, &status,
                    AT_SYMLINK_NOFOLLOW) != 0) continue;
        /* A symlink here would let one store's listing name another's. */
        if (!S_ISDIR(status.st_mode)) continue;
        (void)path_assign(entries[count].id, sizeof entries[count].id,
                          entry->d_name);
        entries[count].seconds = (int64_t)status.st_mtim.tv_sec;
        entries[count].nanoseconds = (int64_t)status.st_mtim.tv_nsec;
        count++;
    }
    (void)closedir(stream);
    *out_count = count;
    return KPA_OK;
}

kpa_result kpa_project_list(kpa_project_summary *out, uint32_t capacity,
                            uint32_t *out_count)
{
    char projects[KPA_ROOT_CAPACITY];
    kpa_list_entry *entries;
    kpa_project *project;
    uint32_t found = 0u;
    uint32_t index;
    kpa_result result;

    if (out == NULL || capacity == 0u || out_count == NULL)
        return KPA_INVALID_ARGUMENT;
    *out_count = 0u;
    result = kpa_projects_directory(projects, sizeof projects);
    if (result != KPA_OK) return result;
    entries = calloc(KPA_MAX_LIST_ENTRIES, sizeof *entries);
    project = malloc(sizeof *project);
    if (entries == NULL || project == NULL) {
        free(entries);
        free(project);
        return KPA_NO_MEMORY;
    }
    result = collect_entries(projects, entries, KPA_MAX_LIST_ENTRIES, &found);
    if (result != KPA_OK) {
        free(entries);
        free(project);
        return result;
    }
    if (found > 1u) qsort(entries, found, sizeof *entries, compare_entries);
    for (index = 0u; index < found && *out_count < capacity; index++) {
        kpa_project_summary *summary = &out[*out_count];

        /* A project that cannot be read is left out rather than allowed to
         * fail the whole listing, which is what list_projects does. */
        if (kpa_project_open(project, entries[index].id) != KPA_OK) continue;
        memset(summary, 0, sizeof *summary);
        (void)path_assign(summary->id, sizeof summary->id, project->id);
        (void)path_assign(summary->title, sizeof summary->title,
                          project->title);
        (void)path_assign(summary->artist, sizeof summary->artist,
                          project->artist);
        summary->duration = project->duration;
        summary->track_count = project->track_count;
        /* cli.py:command_list calls a project ready when its export stage is
         * done; the same word has to mean the same thing on both surfaces. */
        summary->ready = project->stages[KPA_STAGE_EXPORT] == KPA_STAGE_DONE;
        summary->has_lyrics = project->has_lyrics;
        summary->has_tab = project->has_tab;
        (*out_count)++;
        kpa_project_close(project);
    }
    free(entries);
    free(project);
    return KPA_OK;
}
