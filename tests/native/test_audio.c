/*
 * Tests for the streaming multitrack audio session.
 *
 * Every fixture in here is synthesised at run time: six aligned stems, each a
 * single sine at a distinct frequency, written as float32 WAV so a decoded
 * sample equals the sample that was written and the expected mix can be
 * computed exactly rather than approximately.  Nothing on disk survives the
 * run and no recorded material is involved at any point.
 */

#include "kilix_playalong/kpa_audio.h"

#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define SDL_MAIN_HANDLED
#include <SDL2/SDL.h>
#include <sndfile.h>

/* Hooks the implementation exports for tests only; they are deliberately not
 * part of include/kilix_playalong/kpa_audio.h. */
void kpa_audio_debug_render_device(kpa_audio_session *session, float *out,
                                   size_t frames);
void kpa_audio_debug_force_device_lost(kpa_audio_session *session);
bool kpa_audio_debug_produce(kpa_audio_session *session);
size_t kpa_audio_debug_bytes(const kpa_audio_session *session);
uint32_t kpa_audio_debug_ramp_frames(const kpa_audio_session *session);
uint32_t kpa_audio_debug_ring_fill(kpa_audio_session *session);

#define CHECK(condition)                                                      \
    do {                                                                      \
        if (!(condition)) {                                                   \
            (void)fprintf(stderr, "%s:%d: check failed: %s\n", __FILE__,      \
                          __LINE__, #condition);                              \
            return false;                                                     \
        }                                                                     \
    } while (false)

#define STEM_COUNT 6u
#define FIXTURE_RATE 48000u
#define FIXTURE_FRAMES 240000u   /* five seconds */
#define STEM_AMPLITUDE 0.1

#define TAU 6.283185307179586

typedef struct fixture {
    char directory[128];
    char stem[STEM_COUNT][192];
    double frequency[STEM_COUNT];
    uint32_t rate;
    uint64_t frames;
} fixture;

static const double stem_hz[STEM_COUNT] = {
    220.0, 330.0, 440.0, 550.0, 660.0, 880.0
};

/* The single definition of a stem's content; the expected-mix reference and
 * the fixture writer both come through here, so they cannot drift. */
static float tone_sample(double hz, uint32_t rate, uint64_t frame,
                         double amplitude)
{
    return (float)(amplitude *
                   sin(TAU * hz * (double)frame / (double)rate));
}

static bool write_tone_wav(const char *path, uint32_t rate, uint64_t frames,
                           double hz, double amplitude, int format)
{
    SF_INFO info;
    SNDFILE *file;
    float block[4096];
    uint64_t written = 0u;
    bool ok = true;

    (void)memset(&info, 0, sizeof info);
    info.samplerate = (int)rate;
    info.channels = 1;
    info.format = SF_FORMAT_WAV | format;
    file = sf_open(path, SFM_WRITE, &info);
    if (file == NULL) return false;
    while (written < frames && ok) {
        uint64_t chunk = frames - written;
        uint64_t i;

        if (chunk > (uint64_t)(sizeof block / sizeof block[0]))
            chunk = sizeof block / sizeof block[0];
        for (i = 0u; i < chunk; ++i)
            block[i] = tone_sample(hz, rate, written + i, amplitude);
        if (sf_writef_float(file, block, (sf_count_t)chunk) !=
            (sf_count_t)chunk)
            ok = false;
        written += chunk;
    }
    (void)sf_close(file);
    return ok;
}

static bool fixture_build(fixture *fx)
{
    (void)snprintf(fx->directory, sizeof fx->directory,
                   "/tmp/kpa-audio-test-XXXXXX");
    if (mkdtemp(fx->directory) == NULL) return false;
    fx->rate = FIXTURE_RATE;
    fx->frames = FIXTURE_FRAMES;
    for (uint32_t i = 0u; i < STEM_COUNT; ++i) {
        fx->frequency[i] = stem_hz[i];
        (void)snprintf(fx->stem[i], sizeof fx->stem[i], "%s/stem%u.wav",
                       fx->directory, i);
        if (!write_tone_wav(fx->stem[i], fx->rate, fx->frames,
                            fx->frequency[i], STEM_AMPLITUDE,
                            SF_FORMAT_FLOAT))
            return false;
    }
    return true;
}

static void fixture_remove(const fixture *fx)
{
    for (uint32_t i = 0u; i < STEM_COUNT; ++i) (void)unlink(fx->stem[i]);
    (void)rmdir(fx->directory);
}

/* Mirrors decoder_mix exactly: tracks accumulated in add order, each source
 * sample multiplied by that track's gain, mono fanned to every channel. */
static float expected_mix(const fixture *fx, const float *gains,
                          uint64_t frame)
{
    float acc = 0.0f;

    if (frame >= fx->frames) return 0.0f;
    for (uint32_t i = 0u; i < STEM_COUNT; ++i)
        acc += tone_sample(fx->frequency[i], fx->rate, frame,
                           STEM_AMPLITUDE) * gains[i];
    return acc;
}

static kpa_audio_result open_stems(kpa_audio_session *session,
                                   const fixture *fx, uint32_t count)
{
    kpa_audio_result result = KPA_AUDIO_OK;

    for (uint32_t i = 0u; i < count && result == KPA_AUDIO_OK; ++i) {
        kpa_track_id id = 0u;
        const int fd = open(fx->stem[i], O_RDONLY);

        if (fd < 0) return KPA_AUDIO_DECODE;
        result = kpa_audio_add_track(session, fd, &id);
        (void)close(fd);
        if (result == KPA_AUDIO_OK && id != i) result = KPA_AUDIO_DECODE;
    }
    return result;
}

static kpa_audio_session *make_offline(const fixture *fx, uint32_t stems)
{
    kpa_audio_options options;
    kpa_audio_session *session = NULL;

    kpa_audio_options_init(&options);
    options.offline = true;
    if (kpa_audio_create(&session, &options) != KPA_AUDIO_OK) return NULL;
    if (open_stems(session, fx, stems) != KPA_AUDIO_OK) {
        kpa_audio_destroy(session);
        return NULL;
    }
    return session;
}

/* ------------------------------------------------------------------ tests */

static bool test_create_destroy(void)
{
    kpa_audio_options options;
    kpa_audio_session *session = NULL;
    kpa_audio_snapshot snapshot;

    kpa_audio_options_init(&options);
    CHECK(options.output_rate == 0u);
    CHECK(options.output_channels == 0u);
    CHECK(options.target_latency_ms == 0u);
    CHECK(!options.offline);

    CHECK(kpa_audio_create(NULL, &options) == KPA_AUDIO_INVALID_ARGUMENT);
    options.output_channels = 99u;
    CHECK(kpa_audio_create(&session, &options) == KPA_AUDIO_INVALID_ARGUMENT);
    CHECK(session == NULL);

    kpa_audio_options_init(&options);
    options.offline = true;
    CHECK(kpa_audio_create(&session, &options) == KPA_AUDIO_OK);
    CHECK(session != NULL);
    /* No tracks: nothing to play, nothing to render, no device to lose. */
    CHECK(kpa_audio_play(session) == KPA_AUDIO_INVALID_ARGUMENT);
    kpa_audio_snapshot_get(session, &snapshot);
    CHECK(snapshot.audible_frame == 0u);
    CHECK(snapshot.duration_frames == 0u);
    CHECK(!snapshot.device_lost);
    kpa_audio_destroy(session);
    kpa_audio_destroy(NULL);

    CHECK(strcmp(kpa_audio_result_name(KPA_AUDIO_OK), "ok") == 0);
    CHECK(strcmp(kpa_audio_result_name(KPA_AUDIO_MISMATCH),
                 "sample rate mismatch") == 0);
    CHECK(strcmp(kpa_audio_result_name(KPA_AUDIO_RATE_UNAVAILABLE),
                 "rate control unavailable") == 0);
    CHECK(strcmp(kpa_audio_result_name(KPA_AUDIO_LOST), "device lost") == 0);
    return true;
}

static bool test_track_limits(const fixture *fx)
{
    kpa_audio_options options;
    kpa_audio_session *session = NULL;
    kpa_track_id id = 0u;
    char path[192];
    int fd;

    kpa_audio_options_init(&options);
    options.offline = true;
    CHECK(kpa_audio_create(&session, &options) == KPA_AUDIO_OK);
    CHECK(kpa_audio_add_track(session, -1, &id) ==
          KPA_AUDIO_INVALID_ARGUMENT);
    CHECK(kpa_audio_add_track(session, 0, NULL) ==
          KPA_AUDIO_INVALID_ARGUMENT);
    CHECK(open_stems(session, fx, STEM_COUNT) == KPA_AUDIO_OK);

    /* A stem at another rate is a mismatch, never a silent resample. */
    (void)snprintf(path, sizeof path, "%s/odd.wav", fx->directory);
    CHECK(write_tone_wav(path, 44100u, 4410u, 440.0, STEM_AMPLITUDE,
                         SF_FORMAT_FLOAT));
    fd = open(path, O_RDONLY);
    CHECK(fd >= 0);
    CHECK(kpa_audio_add_track(session, fd, &id) == KPA_AUDIO_MISMATCH);
    CHECK(close(fd) == 0);

    /* Something that is not audio at all is a decode failure, not a crash. */
    {
        char junk[192];
        FILE *stream;

        (void)snprintf(junk, sizeof junk, "%s/junk.bin", fx->directory);
        stream = fopen(junk, "wb");
        CHECK(stream != NULL);
        CHECK(fwrite("not a wav file at all", 1u, 21u, stream) == 21u);
        CHECK(fclose(stream) == 0);
        fd = open(junk, O_RDONLY);
        CHECK(fd >= 0);
        CHECK(kpa_audio_add_track(session, fd, &id) == KPA_AUDIO_DECODE);
        CHECK(close(fd) == 0);
        (void)unlink(junk);
    }
    kpa_audio_destroy(session);

    /* Seventeen tracks into a sixteen track session. */
    CHECK(kpa_audio_create(&session, &options) == KPA_AUDIO_OK);
    for (uint32_t i = 0u; i < KPA_AUDIO_MAX_TRACKS + 1u; ++i) {
        kpa_audio_result result;

        (void)snprintf(path, sizeof path, "%s/many%u.wav", fx->directory, i);
        CHECK(write_tone_wav(path, FIXTURE_RATE, 4800u,
                             110.0 + 10.0 * (double)i, STEM_AMPLITUDE,
                             SF_FORMAT_FLOAT));
        fd = open(path, O_RDONLY);
        CHECK(fd >= 0);
        result = kpa_audio_add_track(session, fd, &id);
        CHECK(close(fd) == 0);
        (void)unlink(path);
        if (i < KPA_AUDIO_MAX_TRACKS) {
            CHECK(result == KPA_AUDIO_OK);
            CHECK(id == i);
        } else {
            CHECK(result == KPA_AUDIO_TOO_MANY_TRACKS);
        }
    }
    kpa_audio_destroy(session);
    (void)unlink(path);
    (void)snprintf(path, sizeof path, "%s/odd.wav", fx->directory);
    (void)unlink(path);

    /* An explicit output rate that disagrees with the stems is a mismatch as
     * well: the session will not resample a stem to reach the device. */
    kpa_audio_options_init(&options);
    options.offline = true;
    options.output_rate = 96000u;
    CHECK(kpa_audio_create(&session, &options) == KPA_AUDIO_OK);
    fd = open(fx->stem[0], O_RDONLY);
    CHECK(fd >= 0);
    CHECK(kpa_audio_add_track(session, fd, &id) == KPA_AUDIO_MISMATCH);
    CHECK(close(fd) == 0);
    kpa_audio_destroy(session);

    /* A mono output session fans each mono stem to its single channel. */
    kpa_audio_options_init(&options);
    options.offline = true;
    options.output_channels = 1u;
    CHECK(kpa_audio_create(&session, &options) == KPA_AUDIO_OK);
    CHECK(open_stems(session, fx, STEM_COUNT) == KPA_AUDIO_OK);
    {
        float mono[1024];
        float gains[STEM_COUNT];
        size_t got = 0u;

        for (uint32_t i = 0u; i < STEM_COUNT; ++i) gains[i] = 1.0f;
        CHECK(kpa_audio_render(session, mono, 1024u, &got) == KPA_AUDIO_OK);
        CHECK(got == 1024u);
        for (size_t f = 0u; f < 1024u; ++f)
            CHECK(mono[f] == expected_mix(fx, gains, f));
    }
    kpa_audio_destroy(session);
    return true;
}

static bool test_render_determinism_and_mix(const fixture *fx)
{
    const size_t frames = 120000u;
    const size_t samples = frames * 2u;
    float gains[STEM_COUNT];
    float *first = malloc(samples * sizeof *first);
    float *second = malloc(samples * sizeof *second);
    kpa_audio_session *session;
    size_t got = 0u;
    bool ok = true;

    for (uint32_t i = 0u; i < STEM_COUNT; ++i) gains[i] = 1.0f;
    if (first == NULL || second == NULL) ok = false;

    for (uint32_t pass = 0u; pass < 2u && ok; ++pass) {
        float *target = pass == 0u ? first : second;

        session = make_offline(fx, STEM_COUNT);
        if (session == NULL) { ok = false; break; }
        if (kpa_audio_render(session, target, frames, &got) != KPA_AUDIO_OK ||
            got != frames)
            ok = false;
        kpa_audio_destroy(session);
    }
    if (ok && memcmp(first, second, samples * sizeof *first) != 0) {
        (void)fprintf(stderr, "offline render is not deterministic\n");
        ok = false;
    }
    /* Bit-exact against the analytic mix: a stem dropped, doubled or shifted
     * by one frame would not survive this. */
    for (size_t f = 0u; ok && f < frames; ++f) {
        const float want = expected_mix(fx, gains, f);

        if (first[f * 2u] != want || first[f * 2u + 1u] != want) {
            (void)fprintf(stderr,
                          "mix mismatch at frame %zu: %.9g vs %.9g\n", f,
                          (double)first[f * 2u], (double)want);
            ok = false;
        }
    }
    free(first);
    free(second);
    CHECK(ok);
    return true;
}

static bool test_gain_mute_solo(const fixture *fx)
{
    const size_t settle = 16384u;   /* well past the gain ramp */
    const size_t window = 8192u;
    float *buffer = malloc((settle + window) * 2u * sizeof *buffer);
    float gains[STEM_COUNT];
    kpa_audio_session *session = NULL;
    size_t got = 0u;
    bool ok = buffer != NULL;

    /* gain, mute and solo resolved together: track 1 muted, track 2 soloed,
     * so everything except track 2 must be silent regardless of its gain. */
    if (ok) {
        session = make_offline(fx, STEM_COUNT);
        ok = session != NULL;
    }
    if (ok) {
        ok = kpa_audio_set_gain(session, 0u, 0.5f) == KPA_AUDIO_OK &&
             kpa_audio_set_gain(session, 3u, 0.25f) == KPA_AUDIO_OK &&
             kpa_audio_set_muted(session, 1u, true) == KPA_AUDIO_OK &&
             kpa_audio_set_gain(session, 9u, 1.0f) ==
                 KPA_AUDIO_INVALID_ARGUMENT &&
             kpa_audio_set_gain(session, 0u, -1.0f) ==
                 KPA_AUDIO_INVALID_ARGUMENT;
    }
    if (ok)
        ok = kpa_audio_render(session, buffer, settle + window, &got) ==
                 KPA_AUDIO_OK &&
             got == settle + window;
    if (ok) {
        gains[0] = 0.5f;
        gains[1] = 0.0f;
        gains[2] = 1.0f;
        gains[3] = 0.25f;
        gains[4] = 1.0f;
        gains[5] = 1.0f;
        for (size_t f = settle; ok && f < settle + window; ++f) {
            const float want = expected_mix(fx, gains, f);

            if (buffer[f * 2u] != want) {
                (void)fprintf(stderr, "gain/mute mix mismatch at %zu\n", f);
                ok = false;
            }
        }
    }
    if (ok) ok = kpa_audio_set_soloed(session, 2u, true) == KPA_AUDIO_OK;
    if (ok)
        ok = kpa_audio_render(session, buffer, settle + window, &got) ==
                 KPA_AUDIO_OK &&
             got == settle + window;
    if (ok) {
        for (uint32_t i = 0u; i < STEM_COUNT; ++i) gains[i] = 0.0f;
        gains[2] = 1.0f;
        for (size_t f = settle; ok && f < settle + window; ++f) {
            const uint64_t frame = (uint64_t)(settle + window + f);
            const float want = expected_mix(fx, gains, frame);

            if (buffer[f * 2u] != want) {
                (void)fprintf(stderr, "solo mix mismatch at %zu\n", f);
                ok = false;
            }
        }
    }
    kpa_audio_destroy(session);
    free(buffer);
    CHECK(ok);
    return true;
}

static bool test_click_free(const fixture *fx)
{
    const size_t settle = 16384u;
    const size_t window = 32768u;
    float *buffer = malloc(window * 2u * sizeof *buffer);
    kpa_audio_session *session = make_offline(fx, STEM_COUNT);
    size_t got = 0u;
    double steady = 0.0;
    double bound = 0.0;
    double worst = 0.0;
    double unramped = 0.0;
    uint32_t ramp = 0u;
    bool ok = buffer != NULL && session != NULL;

    if (ok) {
        ramp = kpa_audio_debug_ramp_frames(session);
        ok = ramp >= 200u && ramp <= 300u; /* 5 ms at 48 kHz */
    }
    /* Steady state first: the largest step this material takes on its own. */
    if (ok)
        ok = kpa_audio_render(session, buffer, settle, &got) ==
                 KPA_AUDIO_OK && got == settle;
    if (ok) {
        for (size_t f = 1u; f < settle; ++f) {
            const double delta =
                fabs((double)buffer[f * 2u] - (double)buffer[(f - 1u) * 2u]);

            if (delta > steady) steady = delta;
        }
        /* One ramp increment applied to the muted stem's peak is all a
         * click-free mute may add to that. */
        bound = (steady + STEM_AMPLITUDE / (double)ramp) * 1.05;
        ok = kpa_audio_set_muted(session, 0u, true) == KPA_AUDIO_OK;
    }
    if (ok)
        ok = kpa_audio_render(session, buffer, window, &got) ==
                 KPA_AUDIO_OK && got == window;
    if (ok) {
        for (size_t f = 1u; f < window; ++f) {
            const double delta =
                fabs((double)buffer[f * 2u] - (double)buffer[(f - 1u) * 2u]);

            if (delta > worst) worst = delta;
        }
        /* What an unramped mute would have cost: the muted stem vanishes in
         * one sample, so the discontinuity is its instantaneous value. */
        for (size_t f = 0u; f < ramp * 2u; ++f) {
            const double value = fabs((double)tone_sample(
                fx->frequency[0], fx->rate, (uint64_t)(settle + f),
                STEM_AMPLITUDE));

            if (value > unramped) unramped = value;
        }
        if (worst > bound) {
            (void)fprintf(stderr,
                          "mute clicked: worst step %.6f > bound %.6f\n",
                          worst, bound);
            ok = false;
        }
        /* The test can tell the difference: a step-mute would break it. */
        if (unramped <= bound) ok = false;
        if (ok) {
            float gains[STEM_COUNT];

            for (uint32_t i = 0u; i < STEM_COUNT; ++i) gains[i] = 1.0f;
            gains[0] = 0.0f;
            for (size_t f = window - 4096u; ok && f < window; ++f) {
                const float want =
                    expected_mix(fx, gains, (uint64_t)(settle + f));

                if (buffer[f * 2u] != want) ok = false;
            }
        }
    }
    (void)fprintf(stdout,
                  "  click-free: ramp %u frames, steady step %.6f, worst step"
                  " at mute %.6f, bound %.6f, unramped would be %.6f\n",
                  ramp, steady, worst, bound, unramped);
    kpa_audio_destroy(session);
    free(buffer);
    CHECK(ok);
    return true;
}

static bool test_seek_alignment(const fixture *fx)
{
    const size_t total = 100000u;
    const size_t chunk = 3000u;
    static const uint64_t targets[] = {
        0u, 1u, 999u, 1024u, 1025u, 40960u, 12345u, 512u, 99000u, 7u
    };
    float *reference = malloc(total * 2u * sizeof *reference);
    float *actual = malloc(chunk * 2u * sizeof *actual);
    kpa_audio_session *session = NULL;
    size_t got = 0u;
    bool ok = reference != NULL && actual != NULL;

    if (ok) {
        session = make_offline(fx, STEM_COUNT);
        ok = session != NULL;
    }
    if (ok)
        ok = kpa_audio_render(session, reference, total, &got) ==
                 KPA_AUDIO_OK && got == total;
    if (ok) kpa_audio_destroy(session);

    /* Seeking repeatedly inside one render must land sample exactly, and the
     * frames after the seek must never be a blend of the two positions. */
    if (ok) {
        session = make_offline(fx, STEM_COUNT);
        ok = session != NULL;
    }
    for (size_t t = 0u; ok && t < sizeof targets / sizeof targets[0]; ++t) {
        const uint64_t at = targets[t];

        ok = kpa_audio_seek(session, at) == KPA_AUDIO_OK &&
             kpa_audio_render(session, actual, chunk, &got) == KPA_AUDIO_OK &&
             got == chunk;
        for (size_t f = 0u; ok && f < chunk; ++f) {
            const size_t ref = (size_t)at + f;

            if (ref >= total) break;
            if (actual[f * 2u] != reference[ref * 2u] ||
                actual[f * 2u + 1u] != reference[ref * 2u + 1u]) {
                (void)fprintf(stderr,
                              "seek to %llu misaligned at offset %zu\n",
                              (unsigned long long)at, f);
                ok = false;
            }
        }
        /* Mid-stream seek: no flush residue from the previous position. */
        if (ok) {
            ok = kpa_audio_render(session, actual, 777u, &got) ==
                     KPA_AUDIO_OK && got == 777u;
            for (size_t f = 0u; ok && f < 777u; ++f) {
                const size_t ref = (size_t)at + chunk + f;

                if (ref >= total) break;
                if (actual[f * 2u] != reference[ref * 2u]) ok = false;
            }
        }
    }
    /*
     * Audio already queued for the device when the seek arrives must be
     * discarded, not played.  Fill the ring without consuming any of it, seek,
     * and require the very next frame the consumer sees to be the target.
     */
    if (ok) {
        const uint64_t target = 55555u;

        ok = kpa_audio_seek(session, 20000u) == KPA_AUDIO_OK;
        for (uint32_t i = 0u; ok && i < 4u; ++i)
            ok = kpa_audio_debug_produce(session);
        ok = ok && kpa_audio_debug_ring_fill(session) >= 4096u &&
             kpa_audio_seek(session, target) == KPA_AUDIO_OK &&
             kpa_audio_render(session, actual, chunk, &got) ==
                 KPA_AUDIO_OK && got == chunk;
        for (size_t f = 0u; ok && f < chunk; ++f)
            if (actual[f * 2u] != reference[((size_t)target + f) * 2u] ||
                actual[f * 2u + 1u] !=
                    reference[((size_t)target + f) * 2u + 1u]) {
                (void)fprintf(stderr,
                              "stale pre-seek audio survived at %zu\n", f);
                ok = false;
            }
    }
    if (session != NULL) kpa_audio_destroy(session);
    free(reference);
    free(actual);
    CHECK(ok);
    return true;
}

static bool test_loop_wrap(const fixture *fx)
{
    const size_t total = 100000u;
    const uint64_t loop_start = 20000u;
    const uint64_t loop_end = 60000u;
    const size_t lead = 100u;
    const size_t after = 4000u;
    float *reference = malloc(total * 2u * sizeof *reference);
    float *actual = malloc((lead + after) * 2u * sizeof *actual);
    kpa_audio_session *session = NULL;
    size_t got = 0u;
    bool ok = reference != NULL && actual != NULL;

    if (ok) {
        session = make_offline(fx, STEM_COUNT);
        ok = session != NULL;
    }
    if (ok)
        ok = kpa_audio_render(session, reference, total, &got) ==
                 KPA_AUDIO_OK && got == total;
    if (ok) kpa_audio_destroy(session);

    if (ok) {
        session = make_offline(fx, STEM_COUNT);
        ok = session != NULL;
    }
    if (ok)
        ok = kpa_audio_set_loop(session, 100u, 50u) ==
                 KPA_AUDIO_INVALID_ARGUMENT &&
             kpa_audio_set_loop(session, loop_start, loop_end) ==
                 KPA_AUDIO_OK &&
             kpa_audio_seek(session, loop_end - lead) == KPA_AUDIO_OK &&
             kpa_audio_render(session, actual, lead + after, &got) ==
                 KPA_AUDIO_OK &&
             got == lead + after;
    for (size_t f = 0u; ok && f < lead + after; ++f) {
        const size_t ref = f < lead ? (size_t)loop_end - lead + f
                                    : (size_t)loop_start + (f - lead);

        if (actual[f * 2u] != reference[ref * 2u] ||
            actual[f * 2u + 1u] != reference[ref * 2u + 1u]) {
            (void)fprintf(stderr, "loop wrap not sample exact at %zu\n", f);
            ok = false;
        }
    }
    /* Clearing the loop lets the stream run past the loop end again. */
    if (ok)
        ok = kpa_audio_set_loop(session, 0u, 0u) == KPA_AUDIO_OK &&
             kpa_audio_seek(session, loop_end - 10u) == KPA_AUDIO_OK &&
             kpa_audio_render(session, actual, 200u, &got) == KPA_AUDIO_OK &&
             got == 200u;
    for (size_t f = 0u; ok && f < 200u; ++f) {
        const size_t ref = (size_t)loop_end - 10u + f;

        if (actual[f * 2u] != reference[ref * 2u]) ok = false;
    }
    if (session != NULL) kpa_audio_destroy(session);
    free(reference);
    free(actual);
    CHECK(ok);
    return true;
}

/* --------------------------------------------------------- pitch measure */

static double goertzel_magnitude(const float *mono, size_t frames,
                                 uint32_t rate, double hz)
{
    const double omega = TAU * hz / (double)rate;
    const double coeff = 2.0 * cos(omega);
    double s1 = 0.0;
    double s2 = 0.0;

    for (size_t i = 0u; i < frames; ++i) {
        const double s0 = (double)mono[i] + coeff * s1 - s2;

        s2 = s1;
        s1 = s0;
    }
    return sqrt(s1 * s1 + s2 * s2 - coeff * s1 * s2) / (double)frames;
}

/* Normalised autocorrelation with parabolic peak refinement; good to well
 * under one percent for a steady tone. */
static double estimate_hz(const float *mono, size_t frames, uint32_t rate)
{
    const size_t lag_min = rate / 1200u;
    const size_t lag_max = rate / 120u;
    double *correlation;
    double best = -1.0e30;
    size_t best_lag = lag_min;
    double denominator;
    double shift;

    if (frames <= lag_max * 2u) return -1.0;
    correlation = calloc(lag_max + 2u, sizeof *correlation);
    if (correlation == NULL) return -1.0;
    for (size_t lag = lag_min; lag <= lag_max; ++lag) {
        double sum = 0.0;
        double left = 0.0;
        double right = 0.0;

        for (size_t i = 0u; i + lag < frames; ++i) {
            const double a = (double)mono[i];
            const double b = (double)mono[i + lag];

            sum += a * b;
            left += a * a;
            right += b * b;
        }
        correlation[lag] = sum / (sqrt(left * right) + 1e-12);
        if (correlation[lag] > best) {
            best = correlation[lag];
            best_lag = lag;
        }
    }
    if (best_lag <= lag_min || best_lag >= lag_max) {
        free(correlation);
        return -1.0;
    }
    denominator = correlation[best_lag - 1u] - 2.0 * correlation[best_lag] +
                  correlation[best_lag + 1u];
    shift = denominator == 0.0
                ? 0.0
                : 0.5 * (correlation[best_lag - 1u] -
                         correlation[best_lag + 1u]) / denominator;
    free(correlation);
    if (shift < -1.0 || shift > 1.0) shift = 0.0;
    return (double)rate / ((double)best_lag + shift);
}

static bool test_rate_preserves_pitch(const fixture *fx)
{
    static const double rates[] = {0.75, 1.0, 1.25};
    const size_t capacity = 360000u;
    float *buffer = malloc(capacity * 2u * sizeof *buffer);
    float *mono = malloc(capacity * sizeof *mono);
    kpa_audio_options options;
    kpa_audio_session *session = NULL;
    bool ok = buffer != NULL && mono != NULL;

    if (ok) {
        kpa_audio_options_init(&options);
        options.offline = true;
        ok = kpa_audio_create(&session, &options) == KPA_AUDIO_OK;
    }
    if (ok) {
        /* Only stem 2 (440 Hz) so the fundamental is unambiguous. */
        kpa_track_id id = 0u;
        const int fd = open(fx->stem[2], O_RDONLY);

        ok = fd >= 0 &&
             kpa_audio_add_track(session, fd, &id) == KPA_AUDIO_OK;
        if (fd >= 0) (void)close(fd);
    }
    if (ok) {
        ok = kpa_audio_rate_available(session);
        if (!ok)
            (void)fprintf(stderr,
                          "no pitch preserving engine: rate stays 1.0\n");
    }
    if (ok)
        ok = kpa_audio_set_rate(session, 4.0) ==
                 KPA_AUDIO_INVALID_ARGUMENT &&
             kpa_audio_set_rate(session, 0.1) == KPA_AUDIO_INVALID_ARGUMENT;

    for (size_t r = 0u; ok && r < sizeof rates / sizeof rates[0]; ++r) {
        const double rate = rates[r];
        const size_t want = (size_t)(160000.0 / rate);
        size_t got = 0u;
        double measured;
        double at_440;
        double at_330;
        double at_550;
        kpa_audio_snapshot snapshot;

        ok = kpa_audio_set_rate(session, rate) == KPA_AUDIO_OK &&
             kpa_audio_seek(session, 0u) == KPA_AUDIO_OK;
        if (ok)
            ok = kpa_audio_render(session, buffer, want, &got) ==
                     KPA_AUDIO_OK && got == want;
        if (!ok) break;
        kpa_audio_snapshot_get(session, &snapshot);
        if (snapshot.rate != rate) { ok = false; break; }

        /* Skip the first tenth of a second: the analysis pointer has to walk
         * out of its priming frame before the stretch is in steady state. */
        for (size_t i = 0u; i < got - 8000u; ++i)
            mono[i] = buffer[(i + 8000u) * 2u];
        measured = estimate_hz(mono, got - 8000u, fx->rate);
        at_440 = goertzel_magnitude(mono, got - 8000u, fx->rate, 440.0);
        at_330 = goertzel_magnitude(mono, got - 8000u, fx->rate, 330.0);
        at_550 = goertzel_magnitude(mono, got - 8000u, fx->rate, 550.0);
        (void)fprintf(stdout,
                      "  rate %.2fx: fundamental %.3f Hz (%.3f%% off), "
                      "|440|=%.5f |330|=%.5f |550|=%.5f\n",
                      rate, measured, 100.0 * (measured - 440.0) / 440.0,
                      at_440, at_330, at_550);
        if (measured < 0.0 || fabs(measured - 440.0) / 440.0 > 0.01) {
            (void)fprintf(stderr, "rate %.2f transposes the material\n", rate);
            ok = false;
        }
        /* A resample-based rate control would move the energy to 330 or 550
         * when the rate moves; assert it stays where it started. */
        if (ok && (at_440 < at_330 * 20.0 || at_440 < at_550 * 20.0))
            ok = false;
    }

    /* Total stretched length tracks the requested rate. */
    if (ok) {
        size_t got = 0u;

        ok = kpa_audio_set_rate(session, 0.75) == KPA_AUDIO_OK &&
             kpa_audio_seek(session, 0u) == KPA_AUDIO_OK &&
             kpa_audio_render(session, buffer, capacity, &got) ==
                 KPA_AUDIO_OK;
        if (ok) {
            const double expected = (double)FIXTURE_FRAMES / 0.75;
            const double error = fabs((double)got - expected) / expected;

            (void)fprintf(stdout,
                          "  rate 0.75x length: %zu frames, expected %.0f "
                          "(%.3f%% off)\n", got, expected, 100.0 * error);
            ok = error < 0.02;
        }
    }
    kpa_audio_destroy(session);
    free(buffer);
    free(mono);
    CHECK(ok);
    return true;
}

static bool test_audible_frame(const fixture *fx)
{
    kpa_audio_session *session = make_offline(fx, STEM_COUNT);
    float *buffer = malloc(8192u * 2u * sizeof *buffer);
    kpa_audio_snapshot snapshot;
    size_t got = 0u;
    bool ok = session != NULL && buffer != NULL;

    /* Decode four blocks ahead.  Nothing has been popped, so nothing has
     * been heard: an implementation that reported the decoder position here
     * would fire every lyric cue a ring's worth of audio early. */
    for (uint32_t i = 0u; ok && i < 4u; ++i)
        ok = kpa_audio_debug_produce(session);
    if (ok) ok = kpa_audio_debug_ring_fill(session) == 4096u;
    if (ok) {
        kpa_audio_snapshot_get(session, &snapshot);
        ok = snapshot.audible_frame == 0u &&
             snapshot.duration_frames == fx->frames &&
             snapshot.output_rate == fx->rate && snapshot.rate == 1.0 &&
             !snapshot.device_lost;
    }
    if (ok) {
        kpa_audio_debug_render_device(session, buffer, 1000u);
        kpa_audio_snapshot_get(session, &snapshot);
        ok = snapshot.audible_frame == 1000u;
    }
    if (ok) {
        kpa_audio_debug_render_device(session, buffer, 3096u);
        kpa_audio_snapshot_get(session, &snapshot);
        ok = snapshot.audible_frame == 4096u;
    }
    /* Decoding further ahead must not move the cursor. */
    if (ok) ok = kpa_audio_debug_produce(session);
    if (ok) {
        kpa_audio_snapshot_get(session, &snapshot);
        ok = snapshot.audible_frame == 4096u;
    }
    if (ok)
        ok = kpa_audio_render(session, buffer, 2048u, &got) == KPA_AUDIO_OK &&
             got == 2048u;
    if (ok) {
        kpa_audio_snapshot_get(session, &snapshot);
        ok = snapshot.audible_frame == 6144u;
    }
    /* A seek republishes the mapping, so the cursor follows immediately. */
    if (ok) ok = kpa_audio_seek(session, 30000u) == KPA_AUDIO_OK;
    if (ok) {
        kpa_audio_snapshot_get(session, &snapshot);
        ok = snapshot.audible_frame == 30000u;
    }
    if (ok)
        ok = kpa_audio_render(session, buffer, 512u, &got) == KPA_AUDIO_OK &&
             got == 512u;
    if (ok) {
        kpa_audio_snapshot_get(session, &snapshot);
        ok = snapshot.audible_frame == 30512u;
    }
    kpa_audio_destroy(session);
    free(buffer);
    CHECK(ok);
    return true;
}

static bool test_underrun(const fixture *fx)
{
    const size_t frames = 512u;
    kpa_audio_session *session = make_offline(fx, STEM_COUNT);
    float *buffer = calloc(frames * 2u, sizeof *buffer);
    kpa_audio_snapshot snapshot;
    float gains[STEM_COUNT];
    bool ok = session != NULL && buffer != NULL;

    for (uint32_t i = 0u; i < STEM_COUNT; ++i) gains[i] = 1.0f;
    for (size_t i = 0u; ok && i < frames * 2u; ++i) buffer[i] = 1.0f;

    /* Nothing decoded yet: the callback writes silence and says so. */
    if (ok) {
        kpa_audio_debug_render_device(session, buffer, frames);
        for (size_t i = 0u; ok && i < frames * 2u; ++i)
            if (buffer[i] != 0.0f) ok = false;
    }
    if (ok) {
        kpa_audio_snapshot_get(session, &snapshot);
        ok = snapshot.underrun && snapshot.audible_frame == 0u;
    }
    /* The flag is edge triggered: reading it clears it. */
    if (ok) {
        kpa_audio_snapshot_get(session, &snapshot);
        ok = !snapshot.underrun;
    }
    /* And the silence was not counted as audio: the listener still has not
     * heard source frame 0, so it must be the next thing delivered. */
    if (ok) ok = kpa_audio_debug_produce(session);
    if (ok) {
        kpa_audio_debug_render_device(session, buffer, frames);
        for (size_t f = 0u; ok && f < frames; ++f) {
            const float want = expected_mix(fx, gains, f);

            if (buffer[f * 2u] != want || buffer[f * 2u + 1u] != want)
                ok = false;
        }
    }
    if (ok) {
        kpa_audio_snapshot_get(session, &snapshot);
        ok = !snapshot.underrun && snapshot.audible_frame == frames;
    }
    kpa_audio_destroy(session);
    free(buffer);
    CHECK(ok);
    return true;
}

static bool test_device_lost_offline(const fixture *fx)
{
    kpa_audio_session *session = make_offline(fx, 2u);
    float *buffer = malloc(4096u * 2u * sizeof *buffer);
    kpa_audio_snapshot snapshot;
    kpa_track_id id = 0u;
    uint64_t frozen;
    size_t got = 0u;
    bool ok = session != NULL && buffer != NULL;

    if (ok)
        ok = kpa_audio_render(session, buffer, 4096u, &got) == KPA_AUDIO_OK &&
             got == 4096u;
    if (ok) {
        kpa_audio_snapshot_get(session, &snapshot);
        ok = !snapshot.device_lost && snapshot.audible_frame == 4096u;
    }
    if (ok) {
        kpa_audio_debug_force_device_lost(session);
        kpa_audio_snapshot_get(session, &snapshot);
        frozen = snapshot.audible_frame;
        ok = snapshot.device_lost && frozen == 4096u;
    }
    if (ok)
        ok = kpa_audio_play(session) == KPA_AUDIO_LOST &&
             kpa_audio_pause(session) == KPA_AUDIO_LOST &&
             kpa_audio_seek(session, 0u) == KPA_AUDIO_LOST &&
             kpa_audio_set_gain(session, 0u, 0.5f) == KPA_AUDIO_LOST &&
             kpa_audio_set_muted(session, 0u, true) == KPA_AUDIO_LOST &&
             kpa_audio_set_soloed(session, 0u, true) == KPA_AUDIO_LOST &&
             kpa_audio_set_loop(session, 0u, 100u) == KPA_AUDIO_LOST &&
             kpa_audio_set_rate(session, 0.9) == KPA_AUDIO_LOST &&
             kpa_audio_add_track(session, 0, &id) == KPA_AUDIO_LOST &&
             kpa_audio_render(session, buffer, 512u, &got) == KPA_AUDIO_LOST;
    /* The position must not drift after the loss. */
    if (ok) {
        kpa_audio_snapshot_get(session, &snapshot);
        ok = snapshot.audible_frame == 4096u && snapshot.device_lost;
    }
    kpa_audio_destroy(session);
    free(buffer);
    CHECK(ok);
    return true;
}

static void nap_ms(long milliseconds)
{
    struct timespec request;

    request.tv_sec = milliseconds / 1000L;
    request.tv_nsec = (milliseconds % 1000L) * 1000000L;
    while (nanosleep(&request, &request) != 0 && errno == EINTR) continue;
}

static bool test_live_device(const fixture *fx)
{
    kpa_audio_options options;
    kpa_audio_session *session = NULL;
    kpa_audio_snapshot snapshot;
    float scratch[8];
    size_t got = 0u;
    uint64_t running;
    uint64_t paused_at;
    uint64_t frozen;
    bool ok;

    kpa_audio_options_init(&options);
    options.target_latency_ms = 20u;
    ok = kpa_audio_create(&session, &options) == KPA_AUDIO_OK &&
         open_stems(session, fx, 3u) == KPA_AUDIO_OK &&
         /* render() is an offline-only entry point. */
         kpa_audio_render(session, scratch, 4u, &got) ==
             KPA_AUDIO_INVALID_ARGUMENT &&
         kpa_audio_play(session) == KPA_AUDIO_OK;
    if (ok) {
        nap_ms(250);
        kpa_audio_snapshot_get(session, &snapshot);
        running = snapshot.audible_frame;
        ok = snapshot.playing && !snapshot.device_lost && running > 0u &&
             running < fx->frames;
        if (!ok)
            (void)fprintf(stderr, "live clock did not advance: %llu\n",
                          (unsigned long long)running);
    }
    if (ok) {
        /* Pause stops the clock rather than letting it coast. */
        ok = kpa_audio_pause(session) == KPA_AUDIO_OK;
        kpa_audio_snapshot_get(session, &snapshot);
        paused_at = snapshot.audible_frame;
        nap_ms(150);
        kpa_audio_snapshot_get(session, &snapshot);
        ok = ok && snapshot.audible_frame == paused_at && !snapshot.playing &&
             paused_at >= running;
    }
    if (ok) {
        ok = kpa_audio_play(session) == KPA_AUDIO_OK;
        nap_ms(150);
        kpa_audio_snapshot_get(session, &snapshot);
        ok = ok && snapshot.audible_frame > paused_at;
    }
    /* The seek transaction across the decoder thread and the live callback:
     * every seek must land on its target within the queue depth, never on a
     * position the previous generation was playing. */
    if (ok) {
        static const uint64_t targets[] = {
            96000u, 12000u, 200000u, 48000u, 1u, 150000u, 60000u, 3333u
        };

        for (size_t t = 0u; ok && t < sizeof targets / sizeof targets[0];
             ++t) {
            const uint64_t at = targets[t];

            ok = kpa_audio_seek(session, at) == KPA_AUDIO_OK;
            if (!ok) break;
            kpa_audio_snapshot_get(session, &snapshot);
            /* Only the device buffer, which no flush can reach, may still
             * separate the reported cursor from the seek target. */
            if (snapshot.audible_frame + 2048u < at ||
                snapshot.audible_frame > at) {
                (void)fprintf(stderr,
                              "live seek to %llu reported %llu\n",
                              (unsigned long long)at,
                              (unsigned long long)snapshot.audible_frame);
                ok = false;
                break;
            }
            nap_ms(20);
            kpa_audio_snapshot_get(session, &snapshot);
            if (snapshot.audible_frame + 2048u < at ||
                snapshot.audible_frame > at + fx->rate / 4u) {
                (void)fprintf(stderr,
                              "live playback drifted after seek to %llu:"
                              " %llu\n", (unsigned long long)at,
                              (unsigned long long)snapshot.audible_frame);
                ok = false;
            }
        }
    }
    if (ok) {
        kpa_audio_debug_force_device_lost(session);
        kpa_audio_snapshot_get(session, &snapshot);
        frozen = snapshot.audible_frame;
        ok = snapshot.device_lost;
        nap_ms(150);
        kpa_audio_snapshot_get(session, &snapshot);
        ok = ok && snapshot.device_lost &&
             snapshot.audible_frame == frozen &&
             kpa_audio_pause(session) == KPA_AUDIO_LOST;
        (void)fprintf(stdout,
                      "  live dummy device: clock froze at frame %llu\n",
                      (unsigned long long)frozen);
    }
    kpa_audio_destroy(session);
    CHECK(ok);
    return true;
}

/* ------------------------------------------------------- memory budget */

static long read_status_kb(const char *field)
{
    FILE *stream = fopen("/proc/self/status", "r");
    char line[256];
    long value = -1;

    if (stream == NULL) return -1;
    while (fgets(line, (int)sizeof line, stream) != NULL) {
        if (strncmp(line, field, strlen(field)) == 0) {
            if (sscanf(line + strlen(field), " %ld", &value) != 1)
                value = -1;
            break;
        }
    }
    (void)fclose(stream);
    return value;
}

static bool test_long_fixture_memory(void)
{
    char directory[128];
    char path[STEM_COUNT][192];
    char shortpath[STEM_COUNT][192];
    const uint32_t rate = 8000u;
    const uint64_t frames = 30u * 60u * 8000u;   /* thirty minutes */
    const size_t chunk = 16384u;
    kpa_audio_options options;
    kpa_audio_session *session = NULL;
    kpa_audio_session *control = NULL;
    float *buffer = malloc(chunk * 2u * sizeof *buffer);
    uint64_t rendered = 0u;
    size_t long_bytes = 0u;
    size_t short_bytes = 0u;
    long rss_before;
    long rss_after;
    long peak;
    bool ok = buffer != NULL;

    (void)snprintf(directory, sizeof directory, "/tmp/kpa-audio-long-XXXXXX");
    if (ok) ok = mkdtemp(directory) != NULL;
    for (uint32_t i = 0u; ok && i < STEM_COUNT; ++i) {
        (void)snprintf(path[i], sizeof path[i], "%s/long%u.wav", directory, i);
        (void)snprintf(shortpath[i], sizeof shortpath[i], "%s/brief%u.wav",
                       directory, i);
        ok = write_tone_wav(path[i], rate, frames, stem_hz[i] / 4.0,
                            STEM_AMPLITUDE, SF_FORMAT_PCM_16) &&
             write_tone_wav(shortpath[i], rate, rate * 10u,
                            stem_hz[i] / 4.0, STEM_AMPLITUDE,
                            SF_FORMAT_PCM_16);
    }

    kpa_audio_options_init(&options);
    options.offline = true;
    if (ok) ok = kpa_audio_create(&session, &options) == KPA_AUDIO_OK;
    if (ok) ok = kpa_audio_create(&control, &options) == KPA_AUDIO_OK;
    for (uint32_t i = 0u; ok && i < STEM_COUNT; ++i) {
        kpa_track_id id = 0u;
        int fd = open(path[i], O_RDONLY);

        ok = fd >= 0 && kpa_audio_add_track(session, fd, &id) == KPA_AUDIO_OK;
        if (fd >= 0) (void)close(fd);
        if (!ok) break;
        fd = open(shortpath[i], O_RDONLY);
        ok = fd >= 0 && kpa_audio_add_track(control, fd, &id) == KPA_AUDIO_OK;
        if (fd >= 0) (void)close(fd);
    }

    if (ok) {
        long_bytes = kpa_audio_debug_bytes(session);
        short_bytes = kpa_audio_debug_bytes(control);
        rss_before = read_status_kb("VmRSS:");
        for (;;) {
            size_t got = 0u;

            if (kpa_audio_render(session, buffer, chunk, &got) !=
                KPA_AUDIO_OK) {
                ok = false;
                break;
            }
            if (got == 0u) break;
            rendered += got;
        }
        rss_after = read_status_kb("VmRSS:");
        peak = read_status_kb("VmHWM:");
        (void)fprintf(stdout,
                      "  30 min x %u stems: rendered %llu frames, session "
                      "heap %zu bytes (10 s session: %zu), RSS %ld -> %ld kB,"
                      " peak %ld kB\n",
                      STEM_COUNT, (unsigned long long)rendered, long_bytes,
                      short_bytes, rss_before, rss_after, peak);
        /* The engine's own allocation depends on rate and channel count only,
         * so a thirty minute session must cost exactly what a ten second one
         * costs, and the process must not grow while streaming it. */
        if (rendered != frames) ok = false;
        if (long_bytes != short_bytes) ok = false;
        if (long_bytes > 4u * 1024u * 1024u) ok = false;
        if (rss_before > 0 && rss_after > 0 &&
            rss_after - rss_before > 65536L)
            ok = false;
    }

    kpa_audio_destroy(session);
    kpa_audio_destroy(control);
    for (uint32_t i = 0u; i < STEM_COUNT; ++i) {
        (void)unlink(path[i]);
        (void)unlink(shortpath[i]);
    }
    (void)rmdir(directory);
    free(buffer);
    CHECK(ok);
    return true;
}

int main(void)
{
    fixture fx;
    bool ok;

    (void)setenv("SDL_AUDIODRIVER", "dummy", 1);
    SDL_SetMainReady();
    (void)memset(&fx, 0, sizeof fx);
    if (!fixture_build(&fx)) {
        (void)fprintf(stderr, "could not build the synthetic fixture\n");
        fixture_remove(&fx);
        return EXIT_FAILURE;
    }
    ok = test_create_destroy() && test_track_limits(&fx) &&
         test_render_determinism_and_mix(&fx) && test_gain_mute_solo(&fx) &&
         test_click_free(&fx) && test_seek_alignment(&fx) &&
         test_loop_wrap(&fx) && test_audible_frame(&fx) &&
         test_underrun(&fx) && test_device_lost_offline(&fx) &&
         test_rate_preserves_pitch(&fx) && test_live_device(&fx) &&
         test_long_fixture_memory();
    fixture_remove(&fx);
    SDL_Quit();
    if (!ok) return EXIT_FAILURE;
    (void)puts("ok: kpa-audio");
    return 0;
}
