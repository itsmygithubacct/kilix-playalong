#ifndef KILIX_PLAYALONG_KPA_AUDIO_H
#define KILIX_PLAYALONG_KPA_AUDIO_H

/*
 * File-backed streaming multitrack playback with one clock.
 *
 * This is deliberately application-internal.  The shared `kilix-audio-session`
 * library remains a candidate that needs Kilix Amp as a migrated second
 * consumer before it can exist, so extracting one here would create a second
 * audio engine rather than remove one.  The contract below is written to that
 * candidate's behavioural rules so an extraction later is a move, not a
 * redesign:
 *
 *   - callers pass pre-opened descriptors, never peer-selected paths;
 *   - every track is normalised into one interleaved float32 output format;
 *   - the real-time callback allocates nothing, opens nothing and takes no
 *     unbounded lock - it pops a single-producer ring and returns;
 *   - seek flushes every decoder and the output queue as one transaction;
 *   - the audible-frame estimate subtracts queued device frames, so the lyric
 *     and tab cursors follow heard audio and not decoded-ahead audio;
 *   - an unavailable pitch-preserving engine reports rate control unavailable
 *     and never silently changes musical pitch; and
 *   - device failure pauses the clock and returns a structured recoverable
 *     error instead of advancing a fictional position.
 *
 * It owns no lyrics, tabs, projects, models or UI state.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define KPA_AUDIO_MAX_TRACKS 16u

typedef enum kpa_audio_result {
    KPA_AUDIO_OK = 0,
    KPA_AUDIO_INVALID_ARGUMENT = 1,
    KPA_AUDIO_NO_MEMORY = 2,
    KPA_AUDIO_DECODE = 3,          /* libsndfile refused the file */
    KPA_AUDIO_DEVICE = 4,          /* device open or restart failed */
    KPA_AUDIO_TOO_MANY_TRACKS = 5,
    KPA_AUDIO_MISMATCH = 6,        /* sample rate differs from the session */
    KPA_AUDIO_RATE_UNAVAILABLE = 7,/* no qualified pitch-preserving engine */
    KPA_AUDIO_LOST = 8             /* device disappeared; recoverable */
} kpa_audio_result;

const char *kpa_audio_result_name(kpa_audio_result result);

typedef uint32_t kpa_track_id;

typedef struct kpa_audio_options {
    uint32_t output_rate;        /* 0 selects the first track's rate */
    uint16_t output_channels;    /* 0 selects 2 */
    uint32_t target_latency_ms;  /* 0 selects 40 */
    /* Offline sessions never open a device.  Every mixing decision follows the
     * same path as the live session, which is what makes the deterministic
     * test meaningful rather than a test of a parallel implementation. */
    bool offline;
} kpa_audio_options;

typedef struct kpa_audio_snapshot {
    /* Position in source frames of the audio the listener is hearing now. */
    uint64_t audible_frame;
    uint64_t duration_frames;
    uint32_t output_rate;
    double rate;
    bool playing;
    bool underrun;      /* the decoder could not keep up since the last read */
    bool device_lost;
} kpa_audio_snapshot;

typedef struct kpa_audio_session kpa_audio_session;

void kpa_audio_options_init(kpa_audio_options *options);

kpa_audio_result kpa_audio_create(kpa_audio_session **out,
                                  const kpa_audio_options *options);
void kpa_audio_destroy(kpa_audio_session *session);

/*
 * Adds a track from a borrowed descriptor.  The session dups it; the caller
 * may close its own copy on return.  All tracks must share one sample rate:
 * resampling belongs to the pipeline that produced the stems, not to a
 * practice player that would then hide a stem it had silently retuned.
 */
kpa_audio_result kpa_audio_add_track(kpa_audio_session *session,
                                     int borrowed_fd, kpa_track_id *out_track);

kpa_audio_result kpa_audio_set_gain(kpa_audio_session *session,
                                    kpa_track_id track, float gain);
kpa_audio_result kpa_audio_set_muted(kpa_audio_session *session,
                                     kpa_track_id track, bool muted);
kpa_audio_result kpa_audio_set_soloed(kpa_audio_session *session,
                                      kpa_track_id track, bool soloed);

kpa_audio_result kpa_audio_play(kpa_audio_session *session);
kpa_audio_result kpa_audio_pause(kpa_audio_session *session);
/* One transaction across every decoder and the output queue. */
kpa_audio_result kpa_audio_seek(kpa_audio_session *session,
                                uint64_t source_frame);

/*
 * Loop over [start, end) in source frames; end == 0 clears the loop.  The
 * wrap happens in the decoder as a seek, so the loop point is sample exact
 * rather than a UI poll that lands a few tens of milliseconds late.
 */
kpa_audio_result kpa_audio_set_loop(kpa_audio_session *session,
                                    uint64_t start_frame, uint64_t end_frame);

/*
 * Practice rate.  Returns KPA_AUDIO_RATE_UNAVAILABLE when this build has no
 * qualified pitch-preserving engine, and in that case does not change the
 * rate: a guitar part transposed by a semitone is worse than one that plays
 * at full speed.
 */
kpa_audio_result kpa_audio_set_rate(kpa_audio_session *session, double rate);
bool kpa_audio_rate_available(const kpa_audio_session *session);

void kpa_audio_snapshot_get(kpa_audio_session *session,
                            kpa_audio_snapshot *out);

/*
 * Deterministic offline render through the same gain/mix/stretch graph the
 * device path uses.  Writes interleaved float32 and returns frames produced.
 * Only valid on a session created with options->offline.
 */
kpa_audio_result kpa_audio_render(kpa_audio_session *session, float *out,
                                  size_t frame_capacity, size_t *out_frames);

#ifdef __cplusplus
}
#endif

#endif
