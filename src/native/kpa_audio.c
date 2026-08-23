/*
 * Streaming multitrack playback with one clock.
 *
 * Layout of the engine, top to bottom: the lock-free output ring the device
 * callback pops, the per-track decode and gain-ramped mix, the WSOLA
 * time-stretch that owns practice rate, the producer step that glues those
 * together, and finally the public entry points.
 *
 * Two threads touch a live session.  The decoder thread owns every SNDFILE,
 * the mix buffers, the stretch state and the ring's write side.  The device
 * callback owns the ring's read side and nothing else.  Control state lives
 * behind `lock` and is copied into a kpa_control at the top of each producer
 * step, so the callback never waits on the application and the application
 * never waits on a decode longer than one block.
 */

#include "kilix_playalong/kpa_audio.h"

#include <errno.h>
#include <math.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define SDL_MAIN_HANDLED
#include <SDL2/SDL.h>
#include <sndfile.h>

/*
 * The callback reads these atomics from a real-time thread.  A lock-backed
 * atomic there would be exactly the unbounded lock the contract forbids, so
 * refuse to build rather than ship a callback that can block.
 */
_Static_assert(ATOMIC_LLONG_LOCK_FREE == 2,
               "64-bit atomics must be lock-free for the audio callback");
_Static_assert(ATOMIC_BOOL_LOCK_FREE == 2,
               "atomic_bool must be lock-free for the audio callback");

#define KPA_BLOCK_FRAMES 1024u   /* decode/mix granularity */
#define KPA_MAX_CHANNELS 8u      /* output channels we are willing to mix */
#define KPA_MAX_SRC_CHANNELS 64u /* per-track channel sanity bound */
#define KPA_RAMP_MS 5u           /* gain ramp length; a mute must not click */
#define KPA_MIN_RATE 0.5
#define KPA_MAX_RATE 2.0

/*
 * The WSOLA engine below was qualified by measuring the fundamental of a
 * rendered sine at 0.75x, 1.0x and 1.25x (tests/native/test_audio.c,
 * test_rate_preserves_pitch).  If that measurement ever stops passing this
 * constant becomes false and kpa_audio_set_rate reports
 * KPA_AUDIO_RATE_UNAVAILABLE instead of transposing the song.
 */
#define KPA_RATE_ENGINE_QUALIFIED true

/* ------------------------------------------------------------------ ring */

/*
 * Single-producer / single-consumer ring of interleaved output frames.
 *
 * Positions are monotonic 64-bit frame counters; the buffer index is the
 * counter masked by a power-of-two capacity, so a wrap is arithmetic rather
 * than a branch the callback has to get right.
 *
 * Seek is a flush the consumer performs on itself.  The producer publishes
 * its current write position as `flush_mark` and then bumps `flush_epoch`.
 * The consumer notices the epoch change and jumps its read position to the
 * mark, which is exactly the boundary between pre-seek and post-seek frames.
 * The producer can never have overwritten that region: it only writes while
 * write_pos - read_pos < capacity and read_pos <= flush_mark, so
 * write_pos - flush_mark < capacity always holds.  That is what makes the
 * callback unable to observe a mix of pre-seek and post-seek audio without
 * the callback taking a lock.
 */
typedef struct kpa_ring {
    float *data;
    uint32_t capacity_frames;
    uint32_t mask;
    uint16_t channels;

    uint64_t write_pos;             /* producer-owned */
    uint64_t read_pos;              /* consumer-owned */

    _Atomic uint64_t published_write;
    _Atomic uint64_t published_read;
    _Atomic uint64_t flush_mark;
    _Atomic uint64_t flush_epoch;
    uint64_t consumer_epoch;        /* consumer-owned */
} kpa_ring;

static uint32_t round_up_pow2(uint32_t value)
{
    uint32_t result = 1u;

    while (result < value && result < 0x40000000u) result <<= 1;
    return result;
}

static void ring_reset(kpa_ring *ring)
{
    ring->write_pos = 0u;
    ring->read_pos = 0u;
    ring->consumer_epoch = 0u;
    atomic_store_explicit(&ring->published_write, 0u, memory_order_relaxed);
    atomic_store_explicit(&ring->published_read, 0u, memory_order_relaxed);
    atomic_store_explicit(&ring->flush_mark, 0u, memory_order_relaxed);
    atomic_store_explicit(&ring->flush_epoch, 0u, memory_order_relaxed);
}

static uint32_t ring_space(const kpa_ring *ring)
{
    const uint64_t read_pos =
        atomic_load_explicit(&ring->published_read, memory_order_acquire);
    const uint64_t used = ring->write_pos - read_pos;

    if (used >= (uint64_t)ring->capacity_frames) return 0u;
    return ring->capacity_frames - (uint32_t)used;
}

/* Producer side.  Returns false when the caller must retry later. */
static bool ring_push(kpa_ring *ring, const float *frames, uint32_t count)
{
    uint32_t offset;
    uint32_t first;

    if (count == 0u) return true;
    if (ring_space(ring) < count) return false;

    offset = (uint32_t)(ring->write_pos & ring->mask);
    first = ring->capacity_frames - offset;
    if (first > count) first = count;
    (void)memcpy(ring->data + (size_t)offset * ring->channels, frames,
                 (size_t)first * ring->channels * sizeof *frames);
    if (first < count)
        (void)memcpy(ring->data,
                     frames + (size_t)first * ring->channels,
                     (size_t)(count - first) * ring->channels *
                         sizeof *frames);
    ring->write_pos += count;
    atomic_store_explicit(&ring->published_write, ring->write_pos,
                          memory_order_release);
    return true;
}

static void ring_flush(kpa_ring *ring)
{
    atomic_store_explicit(&ring->flush_mark, ring->write_pos,
                          memory_order_release);
    (void)atomic_fetch_add_explicit(&ring->flush_epoch, 1u,
                                    memory_order_release);
}

/*
 * Consumer side.  Adopts any pending flush first, then copies whole frames
 * into `dst` as bytes so the device buffer is never reinterpreted as float*
 * (SDL hands out a malloc'd buffer, but a cast would still be an alignment
 * assumption this code does not need to make).  At most two memcpys run.
 */
static uint32_t ring_pop(kpa_ring *ring, unsigned char *dst, uint32_t count,
                         bool *flushed)
{
    const size_t frame_bytes = (size_t)ring->channels * sizeof(float);
    uint64_t write_pos;
    uint64_t available;
    uint32_t taken;
    uint32_t offset;
    uint32_t first;
    const uint64_t epoch =
        atomic_load_explicit(&ring->flush_epoch, memory_order_acquire);

    if (epoch != ring->consumer_epoch) {
        ring->consumer_epoch = epoch;
        ring->read_pos =
            atomic_load_explicit(&ring->flush_mark, memory_order_acquire);
        atomic_store_explicit(&ring->published_read, ring->read_pos,
                              memory_order_release);
        *flushed = true;
    }

    write_pos = atomic_load_explicit(&ring->published_write,
                                     memory_order_acquire);
    if (write_pos <= ring->read_pos || count == 0u) return 0u;
    available = write_pos - ring->read_pos;
    taken = available < (uint64_t)count ? (uint32_t)available : count;

    offset = (uint32_t)(ring->read_pos & ring->mask);
    first = ring->capacity_frames - offset;
    if (first > taken) first = taken;
    (void)memcpy(dst, ring->data + (size_t)offset * ring->channels,
                 (size_t)first * frame_bytes);
    if (first < taken)
        (void)memcpy(dst + (size_t)first * frame_bytes, ring->data,
                     (size_t)(taken - first) * frame_bytes);

    ring->read_pos += taken;
    atomic_store_explicit(&ring->published_read, ring->read_pos,
                          memory_order_release);
    return taken;
}

/* ---------------------------------------------------------------- tracks */

typedef struct kpa_track {
    SNDFILE *file;
    int fd;                  /* our dup; sf_open_fd was told not to own it */
    uint64_t frames;
    uint32_t rate;
    uint16_t channels;
    uint64_t at_frame;       /* decoder position; UINT64_MAX == unknown */
    float *scratch;          /* KPA_BLOCK_FRAMES * channels */

    float gain;              /* control plane, guarded by session->lock */
    bool muted;
    bool soloed;

    float applied;           /* decoder-owned ramp state */
} kpa_track;

/* Snapshot of the control plane taken once per producer step. */
typedef struct kpa_control {
    float gain[KPA_AUDIO_MAX_TRACKS];
    bool audible[KPA_AUDIO_MAX_TRACKS];
    double rate;
    uint64_t loop_start;
    uint64_t loop_end;
    bool playing;
} kpa_control;

/* ----------------------------------------------------------------- wsola */

/*
 * Waveform-similarity overlap-add.  Analysis frames are taken on the ideal
 * grid `ideal += hop * rate`, each allowed to slide by up to `delta` frames
 * to the position whose leading `hop` samples best match the natural
 * continuation of the previously chosen frame.  Overlap-add with a periodic
 * Hann window at hop = window/2 sums to unity, so nothing is resampled and
 * nothing is transposed: only the rate at which the analysis pointer walks
 * the input changes.
 *
 * The chosen slide never feeds back into `ideal`, otherwise the tolerance
 * would accumulate and the stretch ratio would drift away from `rate`.
 */
typedef struct kpa_wsola {
    float *pcm;          /* capacity * channels, interleaved mixed audio */
    float *mono;         /* capacity, channel sum used for the search */
    uint64_t *src;       /* capacity, source frame behind each pcm frame */
    float *window;       /* window frames */
    float *tail;         /* hop * channels, pending overlap-add half */
    uint32_t capacity;
    uint32_t fill;
    uint64_t base;       /* stream index of pcm[0] */
    uint64_t stream;     /* next stream index the mixer will produce */
    uint64_t stream_min; /* oldest index this stretch generation may look at */
    uint64_t end;        /* stream index where real audio stopped */
    uint64_t end_src;    /* source frame behind that stream index */
    uint32_t window_frames;
    uint32_t hop;
    uint32_t delta;
    uint64_t prev;
    double ideal;
    bool primed;
    bool ended;
    bool tail_flushed;
} kpa_wsola;

/* ---------------------------------------------------------------- session */

struct kpa_audio_session {
    uint32_t rate_hz;
    uint16_t channels;
    uint32_t ramp_frames;
    bool offline;
    uint32_t latency_ms;
    uint32_t latency_frames;

    kpa_track tracks[KPA_AUDIO_MAX_TRACKS];
    uint32_t track_count;
    uint64_t duration;

    pthread_mutex_t lock;
    pthread_cond_t wake;      /* decoder waits here */
    pthread_cond_t acked;     /* callers wait here for a seek to land */
    bool lock_ready;
    bool thread_ready;
    pthread_t thread;

    /* control plane, guarded by lock */
    bool playing;
    bool stopping;
    double rate;
    uint64_t loop_start;
    uint64_t loop_end;
    uint64_t seek_target;
    uint64_t seek_serial;
    uint64_t seek_done;
    bool seek_pending;

    /* decoder-owned */
    uint64_t position;
    uint64_t last_src_end;    /* source frame behind the newest pushed block */
    bool stretch_active;
    float *stage;             /* max(KPA_BLOCK_FRAMES, hop) * channels */
    uint32_t stage_frames;
    kpa_wsola wsola;

    kpa_ring ring;

    /*
     * Producer-published mapping from output frames to source frames, read by
     * kpa_audio_snapshot_get through a seqlock.  Only the producer writes it.
     */
    _Atomic uint64_t head_seq;
    _Atomic uint64_t head_out;
    _Atomic uint64_t head_src;

    /* callback-published */
    _Atomic uint64_t callback_ns;
    _Atomic uint32_t callback_frames;
    _Atomic bool underrun;
    _Atomic bool stream_ended;
    _Atomic bool device_lost;

    uint64_t frozen_audible;
    bool frozen_valid;
    uint64_t hold_audible;    /* the clock stops while the device is paused */
    bool hold_valid;

    SDL_AudioDeviceID device;
    uint32_t device_frames;
    bool subsystem;

    size_t bytes;             /* session-scoped allocation counter */
};

/* Test-only hooks.  Deliberately absent from the public header: the test
 * declares them itself so the shipped contract stays exactly kpa_audio.h. */
void kpa_audio_debug_render_device(kpa_audio_session *session, float *out,
                                   size_t frames);
void kpa_audio_debug_force_device_lost(kpa_audio_session *session);
bool kpa_audio_debug_produce(kpa_audio_session *session);
size_t kpa_audio_debug_bytes(const kpa_audio_session *session);
uint32_t kpa_audio_debug_ramp_frames(const kpa_audio_session *session);
uint32_t kpa_audio_debug_ring_fill(kpa_audio_session *session);

const char *kpa_audio_result_name(kpa_audio_result result)
{
    switch (result) {
    case KPA_AUDIO_OK: return "ok";
    case KPA_AUDIO_INVALID_ARGUMENT: return "invalid argument";
    case KPA_AUDIO_NO_MEMORY: return "out of memory";
    case KPA_AUDIO_DECODE: return "decode failed";
    case KPA_AUDIO_DEVICE: return "device failed";
    case KPA_AUDIO_TOO_MANY_TRACKS: return "too many tracks";
    case KPA_AUDIO_MISMATCH: return "sample rate mismatch";
    case KPA_AUDIO_RATE_UNAVAILABLE: return "rate control unavailable";
    case KPA_AUDIO_LOST: return "device lost";
    default: break;
    }
    return "unknown";
}

void kpa_audio_options_init(kpa_audio_options *options)
{
    if (options == NULL) return;
    options->output_rate = 0u;
    options->output_channels = 0u;
    options->target_latency_ms = 0u;
    options->offline = false;
}

/* Every heap block the session owns goes through here so the test can assert
 * a streaming memory budget without a process-wide counter. */
static void *session_alloc(kpa_audio_session *session, size_t count,
                           size_t size)
{
    void *block;

    if (count != 0u && size > SIZE_MAX / count) return NULL;
    block = calloc(count, size);
    if (block != NULL) session->bytes += count * size;
    return block;
}

static uint64_t monotonic_ns(void)
{
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return 0u;
    return (uint64_t)now.tv_sec * UINT64_C(1000000000) +
           (uint64_t)now.tv_nsec;
}

/* ------------------------------------------------------------ decode/mix */

/*
 * Fills `track->scratch` with `frames` source frames starting at `from`,
 * zero-padding anything past the end of the file.  `frames` is bounded by
 * KPA_BLOCK_FRAMES by every caller, and every length that came out of
 * libsndfile was range-checked in kpa_audio_add_track before it was stored,
 * so the products below cannot overflow the scratch allocation.
 */
static void track_read(kpa_track *track, uint64_t from, uint32_t frames)
{
    const size_t samples = (size_t)frames * track->channels;
    sf_count_t got = 0;
    uint64_t want;

    if (from >= track->frames) {
        (void)memset(track->scratch, 0, samples * sizeof *track->scratch);
        track->at_frame = from + frames;
        return;
    }
    if (track->at_frame != from) {
        if (sf_seek(track->file, (sf_count_t)from, SEEK_SET) < 0) {
            (void)memset(track->scratch, 0, samples * sizeof *track->scratch);
            track->at_frame = UINT64_MAX;
            return;
        }
        track->at_frame = from;
    }

    want = track->frames - from;
    if (want > (uint64_t)frames) want = frames;
    if (want > 0u)
        got = sf_readf_float(track->file, track->scratch,
                             (sf_count_t)want);
    if (got < 0) got = 0;
    if ((size_t)got < samples / track->channels)
        (void)memset(track->scratch + (size_t)got * track->channels, 0,
                     (samples - (size_t)got * track->channels) *
                         sizeof *track->scratch);

    /* A short read that is not the end of the file means libsndfile lost its
     * place; force a seek next block rather than sliding the whole stem. */
    if ((uint64_t)got < want) track->at_frame = UINT64_MAX;
    else track->at_frame = from + (uint64_t)got;
}

static void tracks_seek(kpa_audio_session *session, uint64_t frame)
{
    for (uint32_t i = 0u; i < session->track_count; ++i) {
        kpa_track *track = &session->tracks[i];

        if (frame >= track->frames) {
            track->at_frame = frame;
            continue;
        }
        if (sf_seek(track->file, (sf_count_t)frame, SEEK_SET) < 0)
            track->at_frame = UINT64_MAX;
        else
            track->at_frame = frame;
    }
    session->position = frame;
}

/*
 * Mixes at most `want` frames starting at session->position into `out`,
 * truncating at the loop end or at the end of the longest stem so a loop
 * point lands on an exact frame rather than wherever a block happened to
 * end.  Returns the number of frames produced; 0 means the stream is over.
 */
static uint32_t decoder_mix(kpa_audio_session *session,
                            const kpa_control *control, float *out,
                            uint64_t *src_out, uint32_t want)
{
    const uint16_t channels = session->channels;
    const bool looping = control->loop_end > control->loop_start;
    const float step = 1.0f / (float)session->ramp_frames;
    uint64_t limit;
    uint64_t available;
    uint32_t frames;
    uint64_t start;

    if (looping && session->position >= control->loop_end)
        tracks_seek(session, control->loop_start);

    limit = session->duration;
    if (looping && session->position < control->loop_end)
        limit = control->loop_end;
    if (session->position >= limit) return 0u;

    available = limit - session->position;
    frames = available < (uint64_t)want ? (uint32_t)available : want;
    if (frames > KPA_BLOCK_FRAMES) frames = KPA_BLOCK_FRAMES;
    start = session->position;

    (void)memset(out, 0, (size_t)frames * channels * sizeof *out);
    for (uint32_t t = 0u; t < session->track_count; ++t) {
        kpa_track *track = &session->tracks[t];
        const uint16_t src_channels = track->channels;
        const float target = control->audible[t] ? control->gain[t] : 0.0f;
        float applied = track->applied;

        track_read(track, start, frames);
        if (applied == target && applied == 0.0f) continue;

        if (applied == target) {
            for (uint32_t f = 0u; f < frames; ++f) {
                const float *in = track->scratch + (size_t)f * src_channels;
                float *dst = out + (size_t)f * channels;

                for (uint16_t c = 0u; c < channels; ++c)
                    dst[c] += in[c % src_channels] * applied;
            }
        } else {
            for (uint32_t f = 0u; f < frames; ++f) {
                const float *in = track->scratch + (size_t)f * src_channels;
                float *dst = out + (size_t)f * channels;
                const float delta = target - applied;

                if (delta > step) applied += step;
                else if (delta < -step) applied -= step;
                else applied = target;
                for (uint16_t c = 0u; c < channels; ++c)
                    dst[c] += in[c % src_channels] * applied;
            }
            track->applied = applied;
        }
    }

    if (src_out != NULL)
        for (uint32_t f = 0u; f < frames; ++f) src_out[f] = start + f;
    session->position = start + frames;
    return frames;
}

/* ----------------------------------------------------------- time stretch */

static void wsola_reset(kpa_wsola *stretch, uint16_t channels)
{
    stretch->base = stretch->stream;
    stretch->stream_min = stretch->stream;
    stretch->fill = 0u;
    stretch->prev = stretch->stream;
    stretch->ideal = (double)stretch->stream;
    stretch->end = UINT64_MAX;
    stretch->end_src = 0u;
    stretch->primed = false;
    stretch->ended = false;
    stretch->tail_flushed = false;
    (void)memset(stretch->tail, 0,
                 (size_t)stretch->hop * channels * sizeof *stretch->tail);
}

/*
 * Guarantees pcm covers [lo, hi).  Drops everything before `lo` first so the
 * resident window walks forward instead of growing with the song, then pulls
 * mixed blocks until the range is resident, zero-filling past the end of the
 * material.  Returns false only when the range cannot fit, which the caller
 * has already made impossible by construction.
 */
static bool wsola_ensure(kpa_audio_session *session,
                         const kpa_control *control, uint64_t lo, uint64_t hi)
{
    kpa_wsola *stretch = &session->wsola;
    const uint16_t channels = session->channels;

    if (hi - lo > (uint64_t)stretch->capacity) return false;
    if (lo > stretch->base) {
        const uint32_t drop = (uint32_t)(lo - stretch->base);

        if (drop >= stretch->fill) {
            stretch->fill = 0u;
        } else {
            (void)memmove(stretch->pcm,
                          stretch->pcm + (size_t)drop * channels,
                          (size_t)(stretch->fill - drop) * channels *
                              sizeof *stretch->pcm);
            (void)memmove(stretch->mono, stretch->mono + drop,
                          (size_t)(stretch->fill - drop) *
                              sizeof *stretch->mono);
            (void)memmove(stretch->src, stretch->src + drop,
                          (size_t)(stretch->fill - drop) *
                              sizeof *stretch->src);
            stretch->fill -= drop;
        }
        stretch->base = lo;
    }

    while (stretch->base + stretch->fill < hi) {
        const uint32_t room = stretch->capacity - stretch->fill;
        uint32_t want = room < KPA_BLOCK_FRAMES ? room : KPA_BLOCK_FRAMES;
        uint32_t produced = 0u;
        float *pcm = stretch->pcm + (size_t)stretch->fill * channels;
        uint64_t *src = stretch->src + stretch->fill;

        if (room == 0u) return false;
        if (!stretch->ended) {
            produced = decoder_mix(session, control, pcm, src, want);
            if (produced == 0u) {
                stretch->ended = true;
                stretch->end = stretch->base + stretch->fill;
                stretch->end_src = session->position;
            }
        }
        if (produced == 0u) {
            const uint64_t last = stretch->end_src;

            produced = want;
            (void)memset(pcm, 0,
                         (size_t)produced * channels * sizeof *pcm);
            for (uint32_t f = 0u; f < produced; ++f) src[f] = last;
        }
        for (uint32_t f = 0u; f < produced; ++f) {
            const float *frame = pcm + (size_t)f * channels;
            float sum = 0.0f;

            for (uint16_t c = 0u; c < channels; ++c) sum += frame[c];
            stretch->mono[stretch->fill + f] = sum;
        }
        stretch->fill += produced;
        stretch->stream += produced;
    }
    return true;
}

/* Normalised cross-correlation of one candidate against the template. */
static float wsola_score(const kpa_wsola *stretch, const float *tmpl,
                         uint64_t candidate)
{
    const uint32_t n = stretch->hop;
    const float *mono = stretch->mono + (size_t)(candidate - stretch->base);
    float corr = 0.0f;
    float energy = 0.0f;

    for (uint32_t i = 0u; i < n; ++i) {
        corr += mono[i] * tmpl[i];
        energy += mono[i] * mono[i];
    }
    return corr / sqrtf(energy + 1e-9f);
}

static uint64_t wsola_scan(const kpa_wsola *stretch, const float *tmpl,
                           uint64_t lo, uint64_t hi, uint64_t stride,
                           uint64_t fallback)
{
    uint64_t best = fallback;
    float best_score = -HUGE_VALF;

    for (uint64_t p = lo; p <= hi; p += stride) {
        const float score = wsola_score(stretch, tmpl, p);

        if (score > best_score) {
            best_score = score;
            best = p;
        }
    }
    return best;
}

/*
 * Finds the analysis frame that continues the previous one most smoothly.
 * The score runs on the channel sum so both channels take the same shift and
 * the stereo image stays put.  A strided coarse pass followed by a fine pass
 * around its winner costs about an eighth of an exhaustive scan; on periodic
 * material every period-aligned lobe scores alike, so the coarse pass cannot
 * land in a worse lobe than the exhaustive one would have chosen.
 */
static uint64_t wsola_best_start(const kpa_wsola *stretch, uint64_t lo,
                                 uint64_t hi, uint64_t nominal)
{
    const float *tmpl =
        stretch->mono + (size_t)(stretch->prev + stretch->hop - stretch->base);
    uint64_t coarse = wsola_scan(stretch, tmpl, lo, hi, 4u, nominal);
    const uint64_t fine_lo = coarse > lo + 3u ? coarse - 3u : lo;
    const uint64_t fine_hi = coarse + 3u < hi ? coarse + 3u : hi;

    return wsola_scan(stretch, tmpl, fine_lo, fine_hi, 1u, coarse);
}

/*
 * Produces one synthesis hop of stretched audio.  Returns false when the
 * material is exhausted.  `*src_end` is the source frame the end of this
 * output block corresponds to, which is what the audible-frame estimate
 * walks backwards from.
 */
static bool wsola_step(kpa_audio_session *session, const kpa_control *control,
                       float *out, uint32_t *out_frames, uint64_t *src_end)
{
    kpa_wsola *stretch = &session->wsola;
    const uint16_t channels = session->channels;
    const uint32_t hop = stretch->hop;
    const uint32_t width = stretch->window_frames;
    uint64_t start;
    size_t at;

    if (!stretch->primed) {
        if (!wsola_ensure(session, control, stretch->prev,
                          stretch->prev + width))
            return false;
        if (stretch->ended && stretch->end <= stretch->prev) return false;
        start = stretch->prev;
        at = (size_t)(start - stretch->base);
        /* No fade-in: the missing previous tail is the same audio weighted
         * by the complement of the window, so the first hop is the source. */
        (void)memcpy(out, stretch->pcm + at * channels,
                     (size_t)hop * channels * sizeof *out);
        for (uint32_t f = 0u; f < hop; ++f)
            for (uint16_t c = 0u; c < channels; ++c)
                stretch->tail[(size_t)f * channels + c] =
                    stretch->pcm[(at + hop + f) * channels + c] *
                    stretch->window[hop + f];
        stretch->primed = true;
        stretch->ideal = (double)start + control->rate * (double)hop;
        *out_frames = hop;
        *src_end = stretch->src[at + hop - 1u] + 1u;
        return true;
    }

    {
        const uint64_t nominal = (uint64_t)llround(stretch->ideal);
        const uint64_t search_lo =
            nominal > stretch->stream_min + stretch->delta
                ? nominal - stretch->delta
                : stretch->stream_min;
        const uint64_t search_hi = nominal + stretch->delta;
        const uint64_t lo = stretch->prev + hop < search_lo
                                ? stretch->prev + hop
                                : search_lo;
        uint64_t hi = search_hi + width;

        if (stretch->prev + width > hi) hi = stretch->prev + width;
        if (!wsola_ensure(session, control, lo, hi)) return false;
        if (stretch->ended && nominal >= stretch->end) {
            if (stretch->tail_flushed) return false;
            stretch->tail_flushed = true;
            (void)memcpy(out, stretch->tail,
                         (size_t)hop * channels * sizeof *out);
            *out_frames = hop;
            *src_end = stretch->end_src;
            return true;
        }
        start = wsola_best_start(stretch, search_lo, search_hi, nominal);
    }

    at = (size_t)(start - stretch->base);
    for (uint32_t f = 0u; f < hop; ++f)
        for (uint16_t c = 0u; c < channels; ++c) {
            const size_t o = (size_t)f * channels + c;

            out[o] = stretch->tail[o] +
                     stretch->pcm[(at + f) * channels + c] *
                         stretch->window[f];
            stretch->tail[o] = stretch->pcm[(at + hop + f) * channels + c] *
                               stretch->window[hop + f];
        }
    stretch->prev = start;
    stretch->ideal += control->rate * (double)hop;
    *out_frames = hop;
    *src_end = stretch->src[at + hop - 1u] + 1u;
    return true;
}

/* -------------------------------------------------------------- producer */

typedef enum kpa_step {
    KPA_STEP_PRODUCED = 0,
    KPA_STEP_FULL = 1,
    KPA_STEP_IDLE = 2,
    KPA_STEP_ENDED = 3
} kpa_step;

/*
 * Publishes the (output frame, source frame) pair at the ring's write head as
 * a seqlock.  Only the producer writes it and only kpa_audio_snapshot_get
 * reads it, so the real-time callback is not involved at all.
 */
static void publish_head(kpa_audio_session *session, uint64_t out_frames,
                         uint64_t source_frame)
{
    const uint64_t seq =
        atomic_load_explicit(&session->head_seq, memory_order_relaxed);

    atomic_store_explicit(&session->head_seq, seq + 1u, memory_order_seq_cst);
    atomic_store_explicit(&session->head_src, source_frame,
                          memory_order_seq_cst);
    atomic_store_explicit(&session->head_out, out_frames,
                          memory_order_seq_cst);
    atomic_store_explicit(&session->head_seq, seq + 2u, memory_order_seq_cst);
}

/*
 * The seek transaction.  Every decoder moves, the stretch state is discarded,
 * the output ring is flushed and the frame mapping is republished, all before
 * any further audio is pushed.  The callback either sees the whole thing or
 * none of it, because the flush is a single epoch bump it adopts atomically.
 */
static void perform_seek(kpa_audio_session *session, uint64_t frame)
{
    if (frame > session->duration) frame = session->duration;
    if (session->stage == NULL) {
        session->position = frame;
        return;
    }
    tracks_seek(session, frame);
    wsola_reset(&session->wsola, session->channels);
    session->last_src_end = frame;
    ring_flush(&session->ring);
    publish_head(session, session->ring.write_pos, frame);
    atomic_store_explicit(&session->stream_ended, false,
                          memory_order_release);
}

static void control_snapshot(kpa_audio_session *session, kpa_control *control)
{
    bool any_solo = false;

    for (uint32_t i = 0u; i < session->track_count; ++i)
        if (session->tracks[i].soloed) any_solo = true;
    for (uint32_t i = 0u; i < session->track_count; ++i) {
        const kpa_track *track = &session->tracks[i];

        control->gain[i] = track->gain;
        control->audible[i] =
            !track->muted && (!any_solo || track->soloed);
    }
    control->rate = session->rate;
    control->loop_start = session->loop_start;
    control->loop_end = session->loop_end;
    control->playing = session->playing;
}

static kpa_step producer_step(kpa_audio_session *session, bool force)
{
    kpa_control control;
    uint32_t frames = 0u;
    uint64_t source_end = 0u;
    bool stretching;

    (void)pthread_mutex_lock(&session->lock);
    if (session->seek_pending) {
        const uint64_t target = session->seek_target;
        const uint64_t serial = session->seek_serial;

        session->seek_pending = false;
        (void)pthread_mutex_unlock(&session->lock);
        perform_seek(session, target);
        (void)pthread_mutex_lock(&session->lock);
        session->seek_done = serial;
        (void)pthread_cond_broadcast(&session->acked);
    }
    control_snapshot(session, &control);
    (void)pthread_mutex_unlock(&session->lock);

    if (!force && !control.playing) return KPA_STEP_IDLE;
    if (session->track_count == 0u) return KPA_STEP_ENDED;

    stretching = KPA_RATE_ENGINE_QUALIFIED && control.rate != 1.0;
    if (stretching != session->stretch_active) {
        if (stretching) {
            wsola_reset(&session->wsola, session->channels);
        } else {
            /* The stretch reads ahead of what it has emitted; rewind the
             * decoders to the last frame actually pushed so leaving practice
             * rate does not skip forward by the lookahead. */
            tracks_seek(session, session->last_src_end);
        }
        session->stretch_active = stretching;
    }

    if (stretching) {
        if (ring_space(&session->ring) < session->wsola.hop)
            return KPA_STEP_FULL;
        if (!wsola_step(session, &control, session->stage, &frames,
                        &source_end)) {
            atomic_store_explicit(&session->stream_ended, true,
                                  memory_order_release);
            return KPA_STEP_ENDED;
        }
    } else {
        if (ring_space(&session->ring) < KPA_BLOCK_FRAMES)
            return KPA_STEP_FULL;
        frames = decoder_mix(session, &control, session->stage, NULL,
                             KPA_BLOCK_FRAMES);
        if (frames == 0u) {
            atomic_store_explicit(&session->stream_ended, true,
                                  memory_order_release);
            return KPA_STEP_ENDED;
        }
        source_end = session->position;
    }

    if (!ring_push(&session->ring, session->stage, frames))
        return KPA_STEP_FULL;
    session->last_src_end = source_end;
    publish_head(session, session->ring.write_pos, source_end);
    return KPA_STEP_PRODUCED;
}

/* --------------------------------------------------------- device output */

/*
 * The real-time path.  Every call it makes, in order: atomic_load_explicit,
 * atomic_store_explicit (all on lock-free 64-bit/bool atomics, asserted at
 * the top of this file), memcpy, memset and clock_gettime(CLOCK_MONOTONIC)
 * which is a vDSO read on the platforms this ships to.  No malloc, no free,
 * no file I/O, no mutex, no condition variable, no SDL call, and no loop
 * whose bound is not the fixed frame count SDL asked for: ring_pop performs
 * at most two memcpys and the tail fill at most one memset.
 */
static void audio_fill(kpa_audio_session *session, unsigned char *stream,
                       size_t frames)
{
    const size_t frame_bytes = (size_t)session->channels * sizeof(float);
    bool flushed = false;
    uint32_t popped;

    if (frames > 0xffffffffu) frames = 0xffffffffu;
    popped = ring_pop(&session->ring, stream, (uint32_t)frames, &flushed);
    if ((size_t)popped < frames) {
        (void)memset(stream + (size_t)popped * frame_bytes, 0,
                     (frames - (size_t)popped) * frame_bytes);
        if (!flushed &&
            !atomic_load_explicit(&session->stream_ended,
                                  memory_order_acquire))
            atomic_store_explicit(&session->underrun, true,
                                  memory_order_relaxed);
    }
    atomic_store_explicit(&session->callback_frames, popped,
                          memory_order_relaxed);
    atomic_store_explicit(&session->callback_ns, monotonic_ns(),
                          memory_order_release);
}

static void SDLCALL audio_callback(void *userdata, Uint8 *stream, int len)
{
    kpa_audio_session *session = (kpa_audio_session *)userdata;
    size_t frame_bytes;
    size_t frames;

    if (session == NULL || stream == NULL || len <= 0) return;
    frame_bytes = (size_t)session->channels * sizeof(float);
    frames = (size_t)len / frame_bytes;
    audio_fill(session, stream, frames);
    if ((size_t)len > frames * frame_bytes)
        (void)memset(stream + frames * frame_bytes, 0,
                     (size_t)len - frames * frame_bytes);
}

/*
 * Frames handed to the device that it has not played yet.  The callback
 * stamps a monotonic time and the count it actually filled; the elapsed time
 * since then converts to played frames, clamped to the buffer it filled.
 * Without this the cursor would lead the sound by the whole device buffer.
 */
static uint64_t device_pending_frames(kpa_audio_session *session)
{
    uint64_t stamp;
    uint64_t now;
    uint64_t elapsed;
    uint64_t played;
    const uint32_t filled =
        atomic_load_explicit(&session->callback_frames, memory_order_relaxed);

    if (session->device == 0u || filled == 0u) return 0u;
    stamp = atomic_load_explicit(&session->callback_ns, memory_order_acquire);
    if (stamp == 0u) return 0u;
    now = monotonic_ns();
    if (now <= stamp) return filled;
    elapsed = now - stamp;
    if (elapsed > UINT64_C(1000000000)) return 0u;
    played = elapsed * session->rate_hz / UINT64_C(1000000000);
    if (played >= (uint64_t)filled) return 0u;
    return (uint64_t)filled - played;
}

static void mark_device_lost(kpa_audio_session *session)
{
    atomic_store_explicit(&session->device_lost, true, memory_order_release);
}

/* ---------------------------------------------------------------- thread */

static void *decoder_main(void *argument)
{
    kpa_audio_session *session = (kpa_audio_session *)argument;

    for (;;) {
        kpa_step step;
        bool stopping;

        (void)pthread_mutex_lock(&session->lock);
        stopping = session->stopping;
        (void)pthread_mutex_unlock(&session->lock);
        if (stopping) break;

        if (session->device != 0u &&
            SDL_GetAudioDeviceStatus(session->device) == SDL_AUDIO_STOPPED)
            mark_device_lost(session);

        step = producer_step(session, false);
        if (step == KPA_STEP_PRODUCED) continue;

        (void)pthread_mutex_lock(&session->lock);
        if (!session->stopping && !session->seek_pending) {
            struct timespec deadline;

            if (clock_gettime(CLOCK_MONOTONIC, &deadline) == 0) {
                deadline.tv_nsec += 2000000L; /* 2 ms; also the stop latency */
                if (deadline.tv_nsec >= 1000000000L) {
                    deadline.tv_nsec -= 1000000000L;
                    deadline.tv_sec += 1;
                }
                (void)pthread_cond_timedwait(&session->wake, &session->lock,
                                             &deadline);
            }
        }
        (void)pthread_mutex_unlock(&session->lock);
    }
    return NULL;
}

/* ------------------------------------------------------------ public API */

kpa_audio_result kpa_audio_create(kpa_audio_session **out,
                                  const kpa_audio_options *options)
{
    kpa_audio_options defaults;
    kpa_audio_session *session;

    if (out == NULL) return KPA_AUDIO_INVALID_ARGUMENT;
    *out = NULL;
    if (options == NULL) {
        kpa_audio_options_init(&defaults);
        options = &defaults;
    }
    if (options->output_channels > KPA_MAX_CHANNELS)
        return KPA_AUDIO_INVALID_ARGUMENT;
    if (options->target_latency_ms > 2000u)
        return KPA_AUDIO_INVALID_ARGUMENT;

    session = calloc(1u, sizeof *session);
    if (session == NULL) return KPA_AUDIO_NO_MEMORY;
    session->bytes = sizeof *session;
    session->channels = options->output_channels != 0u
                            ? options->output_channels
                            : (uint16_t)2u;
    session->rate_hz = options->output_rate;
    session->offline = options->offline;
    session->latency_ms =
        options->target_latency_ms != 0u ? options->target_latency_ms : 40u;
    session->rate = 1.0;
    session->playing = false;

    if (pthread_mutex_init(&session->lock, NULL) != 0) {
        free(session);
        return KPA_AUDIO_NO_MEMORY;
    }
    {
        pthread_condattr_t attributes;
        bool ok = pthread_condattr_init(&attributes) == 0;

        if (ok) {
            (void)pthread_condattr_setclock(&attributes, CLOCK_MONOTONIC);
            ok = pthread_cond_init(&session->wake, &attributes) == 0;
            if (ok && pthread_cond_init(&session->acked, &attributes) != 0) {
                (void)pthread_cond_destroy(&session->wake);
                ok = false;
            }
            (void)pthread_condattr_destroy(&attributes);
        }
        if (!ok) {
            (void)pthread_mutex_destroy(&session->lock);
            free(session);
            return KPA_AUDIO_NO_MEMORY;
        }
    }
    session->lock_ready = true;
    ring_reset(&session->ring);
    *out = session;
    return KPA_AUDIO_OK;
}

static void session_release_buffers(kpa_audio_session *session)
{
    free(session->ring.data);
    free(session->stage);
    free(session->wsola.pcm);
    free(session->wsola.mono);
    free(session->wsola.src);
    free(session->wsola.window);
    free(session->wsola.tail);
    session->ring.data = NULL;
    session->stage = NULL;
    session->wsola.pcm = NULL;
    session->wsola.mono = NULL;
    session->wsola.src = NULL;
    session->wsola.window = NULL;
    session->wsola.tail = NULL;
}

/*
 * Allocates every streaming buffer once the output rate is known.  Sizes are
 * derived from the rate and the requested latency only, never from the length
 * of the material, which is what keeps a thirty-minute session the same size
 * as a thirty-second one.
 */
static kpa_audio_result session_prepare(kpa_audio_session *session)
{
    const uint16_t channels = session->channels;
    uint32_t latency_frames;
    uint32_t capacity;
    uint32_t width;
    uint32_t hop;
    uint32_t stage_frames;

    if (session->ring.data != NULL) return KPA_AUDIO_OK;

    session->ramp_frames =
        (uint32_t)((uint64_t)session->rate_hz * KPA_RAMP_MS / 1000u);
    if (session->ramp_frames == 0u) session->ramp_frames = 1u;

    latency_frames = (uint32_t)((uint64_t)session->rate_hz *
                                session->latency_ms / 1000u);
    if (latency_frames < 64u) latency_frames = 64u;
    if (latency_frames > 32768u) latency_frames = 32768u;
    session->latency_frames = round_up_pow2(latency_frames);

    /* ~21 ms analysis window; the search radius is one hop, which covers a
     * full period down to about 95 Hz at 48 kHz. */
    width = round_up_pow2((uint32_t)((uint64_t)session->rate_hz * 21u /
                                     1000u));
    if (width < 64u) width = 64u;
    if (width > 8192u) width = 8192u;
    hop = width / 2u;

    capacity = session->latency_frames * 4u;
    if (capacity < 8192u) capacity = 8192u;
    if (capacity < hop * 8u) capacity = hop * 8u;
    capacity = round_up_pow2(capacity);

    stage_frames = KPA_BLOCK_FRAMES > hop ? KPA_BLOCK_FRAMES : hop;

    session->ring.capacity_frames = capacity;
    session->ring.mask = capacity - 1u;
    session->ring.channels = channels;
    session->ring.data =
        session_alloc(session, (size_t)capacity * channels, sizeof(float));
    session->stage = session_alloc(session, (size_t)stage_frames * channels,
                                   sizeof(float));
    session->stage_frames = stage_frames;

    session->wsola.window_frames = width;
    session->wsola.hop = hop;
    session->wsola.delta = hop;
    session->wsola.capacity = width * 6u;
    session->wsola.pcm = session_alloc(
        session, (size_t)session->wsola.capacity * channels, sizeof(float));
    session->wsola.mono = session_alloc(session, session->wsola.capacity,
                                        sizeof(float));
    session->wsola.src = session_alloc(session, session->wsola.capacity,
                                       sizeof(uint64_t));
    session->wsola.window = session_alloc(session, width, sizeof(float));
    session->wsola.tail =
        session_alloc(session, (size_t)hop * channels, sizeof(float));

    if (session->ring.data == NULL || session->stage == NULL ||
        session->wsola.pcm == NULL || session->wsola.mono == NULL ||
        session->wsola.src == NULL || session->wsola.window == NULL ||
        session->wsola.tail == NULL) {
        session_release_buffers(session);
        return KPA_AUDIO_NO_MEMORY;
    }

    /* Periodic Hann: w[i] + w[i + width/2] == 1, so overlap-add at this hop
     * reconstructs unity gain and no amplitude modulation is introduced. */
    for (uint32_t i = 0u; i < width; ++i)
        session->wsola.window[i] = (float)(0.5 -
            0.5 * cos(6.283185307179586 * (double)i / (double)width));
    wsola_reset(&session->wsola, channels);
    return KPA_AUDIO_OK;
}

static void session_stop_thread(kpa_audio_session *session)
{
    if (!session->thread_ready) return;
    (void)pthread_mutex_lock(&session->lock);
    session->stopping = true;
    (void)pthread_cond_broadcast(&session->wake);
    (void)pthread_cond_broadcast(&session->acked);
    (void)pthread_mutex_unlock(&session->lock);
    (void)pthread_join(session->thread, NULL);
    session->thread_ready = false;
}

void kpa_audio_destroy(kpa_audio_session *session)
{
    if (session == NULL) return;
    session_stop_thread(session);
    if (session->device != 0u) {
        SDL_CloseAudioDevice(session->device);
        session->device = 0u;
    }
    if (session->subsystem) {
        SDL_QuitSubSystem(SDL_INIT_AUDIO);
        session->subsystem = false;
    }
    for (uint32_t i = 0u; i < session->track_count; ++i) {
        kpa_track *track = &session->tracks[i];

        if (track->file != NULL) (void)sf_close(track->file);
        if (track->fd >= 0) (void)close(track->fd);
        free(track->scratch);
    }
    session_release_buffers(session);
    if (session->lock_ready) {
        (void)pthread_cond_destroy(&session->wake);
        (void)pthread_cond_destroy(&session->acked);
        (void)pthread_mutex_destroy(&session->lock);
    }
    free(session);
}

kpa_audio_result kpa_audio_add_track(kpa_audio_session *session,
                                     int borrowed_fd, kpa_track_id *out_track)
{
    SF_INFO info;
    SNDFILE *file;
    kpa_track *track;
    kpa_audio_result result;
    int fd;

    if (session == NULL || out_track == NULL || borrowed_fd < 0)
        return KPA_AUDIO_INVALID_ARGUMENT;
    if (atomic_load_explicit(&session->device_lost, memory_order_acquire))
        return KPA_AUDIO_LOST;
    if (session->track_count >= KPA_AUDIO_MAX_TRACKS)
        return KPA_AUDIO_TOO_MANY_TRACKS;
    /* The decoder thread reads tracks[] without the lock, so the stem set is
     * fixed once playback has started; build the session, then play it. */
    if (session->thread_ready) return KPA_AUDIO_INVALID_ARGUMENT;

    /* dup() shares the file offset with the caller's descriptor, so the
     * caller must be done reading from its copy; the contract already says it
     * may close it on return.  SF_FALSE keeps the close on our side. */
    fd = dup(borrowed_fd);
    if (fd < 0) return KPA_AUDIO_DECODE;
    (void)memset(&info, 0, sizeof info);
    file = sf_open_fd(fd, SFM_READ, &info, SF_FALSE);
    if (file == NULL) {
        (void)close(fd);
        return KPA_AUDIO_DECODE;
    }
    if (info.frames < 0 || info.samplerate <= 0 || info.channels <= 0 ||
        (uint32_t)info.channels > KPA_MAX_SRC_CHANNELS) {
        (void)sf_close(file);
        (void)close(fd);
        return KPA_AUDIO_DECODE;
    }
    if (session->rate_hz == 0u) session->rate_hz = (uint32_t)info.samplerate;
    if (session->rate_hz != (uint32_t)info.samplerate) {
        (void)sf_close(file);
        (void)close(fd);
        return KPA_AUDIO_MISMATCH;
    }

    result = session_prepare(session);
    if (result != KPA_AUDIO_OK) {
        (void)sf_close(file);
        (void)close(fd);
        return result;
    }

    track = &session->tracks[session->track_count];
    track->scratch = session_alloc(
        session, (size_t)KPA_BLOCK_FRAMES * (uint32_t)info.channels,
        sizeof(float));
    if (track->scratch == NULL) {
        (void)memset(track, 0, sizeof *track);
        (void)sf_close(file);
        (void)close(fd);
        return KPA_AUDIO_NO_MEMORY;
    }
    track->file = file;
    track->fd = fd;
    track->frames = (uint64_t)info.frames;
    track->rate = (uint32_t)info.samplerate;
    track->channels = (uint16_t)info.channels;
    track->at_frame = UINT64_MAX;
    track->gain = 1.0f;
    track->applied = 1.0f;
    track->muted = false;
    track->soloed = false;

    if (track->frames > session->duration) session->duration = track->frames;
    *out_track = session->track_count;
    session->track_count += 1u;
    return KPA_AUDIO_OK;
}

static kpa_audio_result control_guard(kpa_audio_session *session,
                                      kpa_track_id track)
{
    if (session == NULL) return KPA_AUDIO_INVALID_ARGUMENT;
    if (atomic_load_explicit(&session->device_lost, memory_order_acquire))
        return KPA_AUDIO_LOST;
    if (track >= session->track_count) return KPA_AUDIO_INVALID_ARGUMENT;
    return KPA_AUDIO_OK;
}

kpa_audio_result kpa_audio_set_gain(kpa_audio_session *session,
                                    kpa_track_id track, float gain)
{
    const kpa_audio_result guard = control_guard(session, track);

    if (guard != KPA_AUDIO_OK) return guard;
    if (!(gain >= 0.0f) || gain > 8.0f) return KPA_AUDIO_INVALID_ARGUMENT;
    (void)pthread_mutex_lock(&session->lock);
    session->tracks[track].gain = gain;
    (void)pthread_cond_broadcast(&session->wake);
    (void)pthread_mutex_unlock(&session->lock);
    return KPA_AUDIO_OK;
}

kpa_audio_result kpa_audio_set_muted(kpa_audio_session *session,
                                     kpa_track_id track, bool muted)
{
    const kpa_audio_result guard = control_guard(session, track);

    if (guard != KPA_AUDIO_OK) return guard;
    (void)pthread_mutex_lock(&session->lock);
    session->tracks[track].muted = muted;
    (void)pthread_cond_broadcast(&session->wake);
    (void)pthread_mutex_unlock(&session->lock);
    return KPA_AUDIO_OK;
}

kpa_audio_result kpa_audio_set_soloed(kpa_audio_session *session,
                                      kpa_track_id track, bool soloed)
{
    const kpa_audio_result guard = control_guard(session, track);

    if (guard != KPA_AUDIO_OK) return guard;
    (void)pthread_mutex_lock(&session->lock);
    session->tracks[track].soloed = soloed;
    (void)pthread_cond_broadcast(&session->wake);
    (void)pthread_mutex_unlock(&session->lock);
    return KPA_AUDIO_OK;
}

static uint64_t audible_estimate(kpa_audio_session *session,
                                 uint64_t *ring_fill_out)
{
    uint64_t head_out = 0u;
    uint64_t head_src = 0u;
    uint64_t read_pos;
    uint64_t ring_fill;
    uint64_t queued;
    uint64_t behind;
    uint64_t audible;
    int attempts = 0;

    for (;;) {
        const uint64_t before =
            atomic_load_explicit(&session->head_seq, memory_order_seq_cst);
        uint64_t after;

        head_src = atomic_load_explicit(&session->head_src,
                                        memory_order_seq_cst);
        head_out = atomic_load_explicit(&session->head_out,
                                        memory_order_seq_cst);
        after = atomic_load_explicit(&session->head_seq,
                                     memory_order_seq_cst);
        if (before == after && (before & 1u) == 0u) break;
        if (++attempts >= 8) break;
    }

    read_pos = atomic_load_explicit(&session->ring.published_read,
                                    memory_order_acquire);
    /*
     * Between a seek and the callback that adopts its flush, published_read
     * still points at pre-seek frames that will never be played.  Counting
     * them as queued would report the cursor a whole ring early for one
     * callback period - the exact "every cue fires before it is heard" bug.
     * The flush mark is where the consumer is about to jump to, so it is the
     * real read position whenever it is ahead.
     */
    {
        const uint64_t mark =
            atomic_load_explicit(&session->ring.flush_mark,
                                 memory_order_acquire);

        if (mark > read_pos) read_pos = mark;
    }
    ring_fill = head_out > read_pos ? head_out - read_pos : 0u;
    queued = ring_fill + device_pending_frames(session);
    behind = (uint64_t)((double)queued * session->rate + 0.5);
    audible = head_src > behind ? head_src - behind : 0u;
    if (audible > session->duration) audible = session->duration;
    if (ring_fill_out != NULL) *ring_fill_out = ring_fill;
    return audible;
}

/* Latches the heard position while the device is not consuming, so a pause
 * does not let the estimate creep forward as the device buffer ages out. */
static void latch_hold(kpa_audio_session *session)
{
    if (session->offline || session->hold_valid) return;
    session->hold_audible = audible_estimate(session, NULL);
    session->hold_valid = true;
}

static kpa_audio_result device_open(kpa_audio_session *session)
{
    SDL_AudioSpec desired;
    SDL_AudioSpec obtained;

    if (session->device != 0u) return KPA_AUDIO_OK;
    if (!session->subsystem) {
        SDL_SetMainReady();
        if (SDL_InitSubSystem(SDL_INIT_AUDIO) != 0) return KPA_AUDIO_DEVICE;
        session->subsystem = true;
    }
    (void)memset(&desired, 0, sizeof desired);
    (void)memset(&obtained, 0, sizeof obtained);
    desired.freq = (int)session->rate_hz;
    desired.format = AUDIO_F32SYS;
    desired.channels = (Uint8)session->channels;
    desired.samples = (Uint16)session->latency_frames;
    desired.callback = audio_callback;
    desired.userdata = session;
    /* Only the buffer size may move.  Letting SDL change the format or the
     * frequency would hand it a resample we promised not to do silently. */
    session->device = SDL_OpenAudioDevice(NULL, 0, &desired, &obtained,
                                          SDL_AUDIO_ALLOW_SAMPLES_CHANGE);
    if (session->device == 0u) return KPA_AUDIO_DEVICE;
    session->device_frames = obtained.samples;
    return KPA_AUDIO_OK;
}

kpa_audio_result kpa_audio_play(kpa_audio_session *session)
{
    kpa_audio_result result;

    if (session == NULL) return KPA_AUDIO_INVALID_ARGUMENT;
    if (atomic_load_explicit(&session->device_lost, memory_order_acquire))
        return KPA_AUDIO_LOST;
    if (session->track_count == 0u) return KPA_AUDIO_INVALID_ARGUMENT;

    if (!session->offline && !session->thread_ready) {
        result = device_open(session);
        if (result != KPA_AUDIO_OK) return result;
        /* Prefill on this thread, before any producer thread exists, so the
         * first callback is served real audio instead of a false underrun. */
        while (producer_step(session, true) == KPA_STEP_PRODUCED) continue;
        if (pthread_create(&session->thread, NULL, decoder_main, session) != 0)
            return KPA_AUDIO_DEVICE;
        session->thread_ready = true;
    }

    (void)pthread_mutex_lock(&session->lock);
    session->playing = true;
    session->hold_valid = false;
    (void)pthread_cond_broadcast(&session->wake);
    (void)pthread_mutex_unlock(&session->lock);
    if (session->device != 0u) SDL_PauseAudioDevice(session->device, 0);
    return KPA_AUDIO_OK;
}

kpa_audio_result kpa_audio_pause(kpa_audio_session *session)
{
    if (session == NULL) return KPA_AUDIO_INVALID_ARGUMENT;
    if (atomic_load_explicit(&session->device_lost, memory_order_acquire))
        return KPA_AUDIO_LOST;
    /* Pausing the device rather than draining the ring keeps the queued
     * frames, so resume continues on the sample it stopped on. */
    if (session->device != 0u) SDL_PauseAudioDevice(session->device, 1);
    (void)pthread_mutex_lock(&session->lock);
    session->playing = false;
    latch_hold(session);
    (void)pthread_mutex_unlock(&session->lock);
    return KPA_AUDIO_OK;
}

kpa_audio_result kpa_audio_seek(kpa_audio_session *session,
                                uint64_t source_frame)
{
    if (session == NULL) return KPA_AUDIO_INVALID_ARGUMENT;
    if (atomic_load_explicit(&session->device_lost, memory_order_acquire))
        return KPA_AUDIO_LOST;

    if (!session->thread_ready) {
        (void)pthread_mutex_lock(&session->lock);
        perform_seek(session, source_frame);
        session->hold_valid = false;
        (void)pthread_mutex_unlock(&session->lock);
        return KPA_AUDIO_OK;
    }

    (void)pthread_mutex_lock(&session->lock);
    session->hold_valid = false;
    session->seek_serial += 1u;
    session->seek_target = source_frame;
    session->seek_pending = true;
    {
        const uint64_t serial = session->seek_serial;

        (void)pthread_cond_broadcast(&session->wake);
        while (session->seek_done < serial && !session->stopping) {
            struct timespec deadline;

            if (clock_gettime(CLOCK_MONOTONIC, &deadline) != 0) break;
            deadline.tv_sec += 2;
            if (pthread_cond_timedwait(&session->acked, &session->lock,
                                       &deadline) == ETIMEDOUT)
                break;
        }
    }
    (void)pthread_mutex_unlock(&session->lock);
    return KPA_AUDIO_OK;
}

kpa_audio_result kpa_audio_set_loop(kpa_audio_session *session,
                                    uint64_t start_frame, uint64_t end_frame)
{
    if (session == NULL) return KPA_AUDIO_INVALID_ARGUMENT;
    if (atomic_load_explicit(&session->device_lost, memory_order_acquire))
        return KPA_AUDIO_LOST;
    if (end_frame != 0u) {
        if (end_frame <= start_frame) return KPA_AUDIO_INVALID_ARGUMENT;
        if (session->duration != 0u) {
            if (start_frame >= session->duration)
                return KPA_AUDIO_INVALID_ARGUMENT;
            if (end_frame > session->duration) end_frame = session->duration;
        }
    } else {
        start_frame = 0u;
    }
    (void)pthread_mutex_lock(&session->lock);
    session->loop_start = start_frame;
    session->loop_end = end_frame;
    (void)pthread_cond_broadcast(&session->wake);
    (void)pthread_mutex_unlock(&session->lock);
    return KPA_AUDIO_OK;
}

kpa_audio_result kpa_audio_set_rate(kpa_audio_session *session, double rate)
{
    if (session == NULL) return KPA_AUDIO_INVALID_ARGUMENT;
    if (atomic_load_explicit(&session->device_lost, memory_order_acquire))
        return KPA_AUDIO_LOST;
    if (!KPA_RATE_ENGINE_QUALIFIED) return KPA_AUDIO_RATE_UNAVAILABLE;
    if (!(rate >= KPA_MIN_RATE) || rate > KPA_MAX_RATE)
        return KPA_AUDIO_INVALID_ARGUMENT;
    (void)pthread_mutex_lock(&session->lock);
    session->rate = rate;
    (void)pthread_cond_broadcast(&session->wake);
    (void)pthread_mutex_unlock(&session->lock);
    return KPA_AUDIO_OK;
}

bool kpa_audio_rate_available(const kpa_audio_session *session)
{
    return session != NULL && KPA_RATE_ENGINE_QUALIFIED;
}

/*
 * audible_frame: the source frame the listener is hearing right now.
 *
 *     audible = head_src - (ring_fill + device_pending) * rate
 *
 * head_src is the source frame behind the newest output frame the decoder
 * pushed, published together with that output frame counter through a
 * seqlock.  ring_fill is what the callback has not popped yet and
 * device_pending is the part of the last callback buffer the device has not
 * played yet; both are output frames, and one output frame consumes `rate`
 * source frames, so a single multiply converts the queue depth back into the
 * source domain.  Subtracting only the ring would leave the cursor early by
 * the entire device buffer, which is exactly the bug that fires every lyric
 * and tab cue before it is heard.
 *
 * During an underrun the callback pops nothing, so ring_fill does not shrink
 * and the estimate correctly stops advancing: injected silence is never
 * counted as heard audio.
 */
void kpa_audio_snapshot_get(kpa_audio_session *session,
                            kpa_audio_snapshot *out)
{
    uint64_t ring_fill = 0u;
    uint64_t audible;
    bool lost;
    bool ended;

    if (out == NULL) return;
    (void)memset(out, 0, sizeof *out);
    if (session == NULL) return;

    (void)pthread_mutex_lock(&session->lock);
    out->duration_frames = session->duration;
    out->output_rate = session->rate_hz;
    out->rate = session->rate;
    lost = atomic_load_explicit(&session->device_lost, memory_order_acquire);
    ended = atomic_load_explicit(&session->stream_ended,
                                 memory_order_acquire);

    audible = audible_estimate(session, &ring_fill);
    if (!session->offline && !session->playing) {
        latch_hold(session);
        audible = session->hold_audible;
    }
    if (lost) {
        if (!session->frozen_valid) {
            session->frozen_audible = audible;
            session->frozen_valid = true;
        }
        audible = session->frozen_audible;
    }
    out->audible_frame = audible;
    out->underrun =
        atomic_exchange_explicit(&session->underrun, false,
                                 memory_order_acq_rel);
    out->playing = session->playing && !(ended && ring_fill == 0u);
    out->device_lost = lost;
    (void)pthread_mutex_unlock(&session->lock);
}

kpa_audio_result kpa_audio_render(kpa_audio_session *session, float *out,
                                  size_t frame_capacity, size_t *out_frames)
{
    const uint16_t channels = session == NULL ? 0u : session->channels;
    size_t done = 0u;

    if (session == NULL || out == NULL || out_frames == NULL)
        return KPA_AUDIO_INVALID_ARGUMENT;
    *out_frames = 0u;
    if (!session->offline) return KPA_AUDIO_INVALID_ARGUMENT;
    if (atomic_load_explicit(&session->device_lost, memory_order_acquire))
        return KPA_AUDIO_LOST;
    if (session->track_count == 0u || frame_capacity == 0u)
        return KPA_AUDIO_OK;

    while (done < frame_capacity) {
        const size_t remaining = frame_capacity - done;
        unsigned char *dst =
            (unsigned char *)(out + done * (size_t)channels);
        bool flushed = false;
        uint32_t want = remaining > 0xffffffffu ? 0xffffffffu
                                                : (uint32_t)remaining;
        uint32_t got = ring_pop(&session->ring, dst, want, &flushed);
        kpa_step step;

        done += got;
        if (done == frame_capacity) break;
        step = producer_step(session, true);
        if (step == KPA_STEP_ENDED) break;
        if (step == KPA_STEP_FULL && got == 0u) break;
    }
    *out_frames = done;
    return KPA_AUDIO_OK;
}

/* ------------------------------------------------------------ test hooks */

void kpa_audio_debug_render_device(kpa_audio_session *session, float *out,
                                   size_t frames)
{
    if (session == NULL || out == NULL || frames == 0u) return;
    audio_fill(session, (unsigned char *)out, frames);
}

void kpa_audio_debug_force_device_lost(kpa_audio_session *session)
{
    if (session == NULL) return;
    mark_device_lost(session);
}

bool kpa_audio_debug_produce(kpa_audio_session *session)
{
    if (session == NULL) return false;
    return producer_step(session, true) == KPA_STEP_PRODUCED;
}

size_t kpa_audio_debug_bytes(const kpa_audio_session *session)
{
    return session == NULL ? 0u : session->bytes;
}

uint32_t kpa_audio_debug_ramp_frames(const kpa_audio_session *session)
{
    return session == NULL ? 0u : session->ramp_frames;
}

uint32_t kpa_audio_debug_ring_fill(kpa_audio_session *session)
{
    uint64_t write_pos;
    uint64_t read_pos;

    if (session == NULL) return 0u;
    write_pos = atomic_load_explicit(&session->ring.published_write,
                                     memory_order_acquire);
    read_pos = atomic_load_explicit(&session->ring.published_read,
                                    memory_order_acquire);
    return write_pos > read_pos ? (uint32_t)(write_pos - read_pos) : 0u;
}
