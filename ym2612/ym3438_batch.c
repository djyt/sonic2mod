/* ym3438_batch.c -- compiled into ym3438.dll alongside ym3438.c
 *
 * Provides OPN2_RenderBatch: runs the emulator for n_samples audio samples in
 * a tight C loop instead of calling OPN2_Clock once per clock from Python.
 *
 * Identical arithmetic to wrapper.py::_render_samples_legacy():
 *   for each sample: call OPN2_Clock 24 times, accumulate L and R, subtract the mode's DC.
 *
 * The DC is the caller's: in YM2612 mode every clock carries a sign-only bias of +-3 so
 * silence sums to 72; in YM3438 mode the non-output clocks are 0 and silence sums to 0.
 *
 * Also provides the two per-sample loops the note renderer used to run in Python:
 *   OPN2_RenderBatchMono  -- the same render folded to mono, (L + R) // 2
 *   PCM_BoxDownsample     -- the box-filter downsampler of renderer.py::_resample_py
 * Both reproduce the Python arithmetic exactly (floor division), so a MOD rendered
 * through them is byte-identical to one rendered through the Python loops.
 */

#include "ym3438.h"
#include <stdint.h>

/*
 * OPN2_RenderBatch
 *
 * chip      : pointer to ym3438_t (opaque to caller)
 * n_samples : number of audio samples to render
 * buf_l     : caller-allocated int32_t[n_samples] — left channel output
 * buf_r     : caller-allocated int32_t[n_samples] — right channel output
 *
 * dc        : what 24 clocks of silence sum to in the current chip mode (72 / 0)
 *
 * buf_l[i] and buf_r[i] are centred on 0 (DC removed).
 */
void OPN2_RenderBatch(void *chip, int n_samples, int32_t *buf_l, int32_t *buf_r, int32_t dc)
{
    int16_t out[2];
    const int32_t DC = dc;
    int s, c;

    for (s = 0; s < n_samples; s++) {
        int32_t l = 0, r = 0;
        for (c = 0; c < 24; c++) {
            OPN2_Clock(chip, out);
            l += out[0];
            r += out[1];
        }
        buf_l[s] = l - DC;
        buf_r[s] = r - DC;
    }
}

/*
 * floor_div: Python's `//` for a possibly negative numerator and a positive divisor.
 * C's `/` truncates toward zero; the renderer's arithmetic is defined in terms of the
 * Python operator, so the two helpers below reproduce it exactly.
 */
static int32_t floor_div(int64_t num, int64_t den)
{
    int64_t q = num / den;
    if ((num % den != 0) && ((num < 0) != (den < 0)))
        q -= 1;
    return (int32_t)q;
}

/*
 * OPN2_RenderBatchMono
 *
 * Same as OPN2_RenderBatch but folds each stereo pair to mono on the spot:
 * buf[i] = (L + R) // 2, the arithmetic of core.pcm.to_mono, so the caller
 * never has to build a Python list of tuples.
 */
void OPN2_RenderBatchMono(void *chip, int n_samples, int32_t *buf, int32_t dc)
{
    int16_t out[2];
    const int32_t DC = dc;
    int s, c;

    for (s = 0; s < n_samples; s++) {
        int32_t l = 0, r = 0;
        for (c = 0; c < 24; c++) {
            OPN2_Clock(chip, out);
            l += out[0];
            r += out[1];
        }
        buf[s] = floor_div((int64_t)(l - DC) + (int64_t)(r - DC), 2);
    }
}

/*
 * PCM_BoxDownsample
 *
 * The box-filter (averaging) downsampler of ym2612/renderer.py::_resample_py, in C,
 * reproducing its arithmetic exactly:
 *
 *     ratio = from_rate / to_rate                       (double)
 *     for i in range(out_len):
 *         start = int(i * ratio)
 *         end   = min(in_len, int((i + 1) * ratio) + 1)
 *         out[i] = sum(in[start:end]) // (end - start)   (0 for an empty window)
 *
 * out_len is computed by the caller (Python's round() of in_len * to_rate / from_rate)
 * so its half-to-even rounding is not re-implemented here.
 */
void PCM_BoxDownsample(const int32_t *in, int in_len, int32_t *out, int out_len,
                       int from_rate, int to_rate)
{
    const double ratio = (double)from_rate / (double)to_rate;
    int i, j;

    for (i = 0; i < out_len; i++) {
        int start = (int)(i * ratio);
        int end   = (int)((i + 1) * ratio) + 1;
        int64_t sum = 0;
        if (end > in_len)
            end = in_len;
        if (start >= end) {
            out[i] = 0;
            continue;
        }
        for (j = start; j < end; j++)
            sum += in[j];
        out[i] = floor_div(sum, end - start);
    }
}
