/* ym3438_batch.c -- compiled into ym3438.dll alongside ym3438.c
 *
 * Provides OPN2_RenderBatch: runs the emulator for n_samples audio samples in
 * a tight C loop instead of calling OPN2_Clock once per clock from Python.
 *
 * Identical arithmetic to wrapper.py::render_samples():
 *   for each sample: call OPN2_Clock 24 times, accumulate L+R, subtract DC=72.
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
 * buf_l[i] and buf_r[i] are centred on 0 (DC removed).
 */
void OPN2_RenderBatch(void *chip, int n_samples, int32_t *buf_l, int32_t *buf_r)
{
    int16_t out[2];
    const int32_t DC = 72;  /* _CLOCKS_PER_SAMPLE * 3 -- YM2612-mode silence level */
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
