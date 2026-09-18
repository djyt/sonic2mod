"""ctypes wrapper around Nuked-OPN2 (ym3438).

Usage::

    opn2 = OPN2()                        # builds DLL, resets chip
    opn2.write_reg(0xB0, 0x07)           # channel 0: alg=7, fb=0
    opn2.key_on(0)                       # key-on all operators, channel 0
    samples = opn2.render_samples(n)     # list of (left, right) int16 tuples
    opn2.key_off(0)
    tail = opn2.render_samples(m)

Register write protocol
-----------------------
YM2612 has two banks (bank 0 = channels 0–2, bank 1 = channels 3–5).
``write_reg(addr, data, bank=0)`` issues:
  1. OPN2_Write(chip, bank*2,   addr)   — address latch
     <24 pipeline-flush clocks>
  2. OPN2_Write(chip, bank*2+1, data)   — data write
     <24 pipeline-flush clocks>

The 24-cycle flush ensures writes are processed before the next
register access; this satisfies the chip's timing requirements for
offline/batch use.

Native sample rate
------------------
The YM2612 outputs one audio sample every 24 internal clocks.
  native_rate = clock_rate / 6 / 24
              = 7,670,454 / 6 / 24 ≈ 53,267 Hz

``render_samples(n)`` clocks the chip 24 times per sample and
collects one (L, R) pair per batch, yielding exactly ``n`` samples.
"""

import array
import ctypes
import functools

from .build import get_lib_path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CLOCK_RATE  = 7_670_454          # Mega Drive NTSC master clock (Hz)
_CLOCKS_PER_SAMPLE = 24           # Internal clocks per audio sample
_NATIVE_RATE = _CLOCK_RATE // 6 // _CLOCKS_PER_SAMPLE  # ≈ 53,267 Hz

# ym3438_t is a large struct (~2–3 KB). We allocate a conservatively sized
# opaque buffer and let the C code use it directly via pointer cast.
_STRUCT_BUFFER_SIZE = 8192        # bytes — safely larger than sizeof(ym3438_t)

# YM2612 chip-type flag (from ym3438.h)
_YM3438_MODE_YM2612 = 0x01

# Key-on register address
_REG_KEY_ON = 0x28

# Pipeline flush clocks between address and data writes
_FLUSH_CLOCKS = 24


# ---------------------------------------------------------------------------
# ctypes DLL interface
# ---------------------------------------------------------------------------

def _load_lib() -> ctypes.CDLL:
    lib = ctypes.CDLL(str(get_lib_path()))

    # void OPN2_Reset(ym3438_t *chip)
    lib.OPN2_Reset.restype  = None
    lib.OPN2_Reset.argtypes = [ctypes.c_void_p]

    # void OPN2_SetChipType(uint32_t type)
    lib.OPN2_SetChipType.restype  = None
    lib.OPN2_SetChipType.argtypes = [ctypes.c_uint32]

    # void OPN2_Clock(ym3438_t *chip, int16_t *buffer)
    lib.OPN2_Clock.restype  = None
    lib.OPN2_Clock.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    # void OPN2_Write(ym3438_t *chip, uint32_t port, uint8_t data)
    lib.OPN2_Write.restype  = None
    lib.OPN2_Write.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint8]

    # void OPN2_RenderBatch(void *chip, int n_samples, int32_t *buf_l, int32_t *buf_r, int32_t dc)
    lib.OPN2_RenderBatch.restype  = None
    lib.OPN2_RenderBatch.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                      ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32]

    # void OPN2_RenderBatchMono(void *chip, int n_samples, int32_t *buf, int32_t dc)
    lib.OPN2_RenderBatchMono.restype  = None
    lib.OPN2_RenderBatchMono.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                          ctypes.c_int32]

    # void PCM_BoxDownsample(const int32_t *in, int in_len, int32_t *out, int out_len,
    #                        int from_rate, int to_rate)
    lib.PCM_BoxDownsample.restype  = None
    lib.PCM_BoxDownsample.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int]

    return lib


@functools.cache
def _shared_lib() -> ctypes.CDLL:
    """The loaded DLL, for helpers that need no chip instance (box_downsample)."""
    return _load_lib()


def box_downsample(mono: array.array, from_rate: int, to_rate: int) -> array.array:
    """Box-filter (averaging) downsample of a mono int32 array, in C.

    Same arithmetic as ym2612.renderer._resample_py (see PCM_BoxDownsample in
    ym3438_batch.c); only useful for downsampling (to_rate < from_rate).
    """
    assert mono.itemsize == 4 and mono.typecode == 'i', "box_downsample wants array('i')"
    out_len = round(len(mono) * to_rate / from_rate)
    out = array.array('i', bytes(out_len * 4))
    if out_len:
        in_buf, _ = mono.buffer_info()
        out_buf, _ = out.buffer_info()
        _shared_lib().PCM_BoxDownsample(in_buf, len(mono), out_buf, out_len, from_rate, to_rate)
    return out


# ---------------------------------------------------------------------------
# OPN2 class
# ---------------------------------------------------------------------------

class OPN2:
    """Python interface to the Nuked-OPN2 YM2612 emulator."""

    NATIVE_RATE = _NATIVE_RATE  # ≈ 53,267 Hz

    def __init__(self, mode: str = "ym2612"):
        """Load the compiled DLL and reset the chip.

        Args:
            mode: "ym2612" (Mega Drive VA2, default) or "ym3438" (YM3438 accurate).
                  Remembered on the instance: ``reset()`` with no argument keeps it.
        """
        if mode not in ("ym2612", "ym3438"):
            raise ValueError(f"OPN2 mode must be 'ym2612' or 'ym3438' (got {mode!r})")
        self.mode = mode
        self._lib = _load_lib()

        # Opaque storage for ym3438_t — 8 KB, well above the actual struct size
        self._buf = (ctypes.c_uint8 * _STRUCT_BUFFER_SIZE)()
        self._chip = ctypes.cast(self._buf, ctypes.c_void_p)

        # Stereo int16 output buffer passed to OPN2_Clock
        self._out = (ctypes.c_int16 * 2)()
        self._out_ptr = ctypes.cast(self._out, ctypes.c_void_p)

        # Pipeline-flush capture buffer; None = discard (default, see _flush)
        self._capture: list | None = None

        self.reset(mode)

    # ------------------------------------------------------------------
    # Core control
    # ------------------------------------------------------------------

    def reset(self, mode: str | None = None) -> None:
        """Reset the chip; ``mode`` switches chip type, None keeps the instance's."""
        if mode is not None:
            if mode not in ("ym2612", "ym3438"):
                raise ValueError(f"OPN2 mode must be 'ym2612' or 'ym3438' (got {mode!r})")
            self.mode = mode
        chip_type = _YM3438_MODE_YM2612 if self.mode == "ym2612" else 0
        self._lib.OPN2_SetChipType(ctypes.c_uint32(chip_type))
        self._lib.OPN2_Reset(self._chip)

    @property
    def _dc(self) -> int:
        """What 24 clocks of silence sum to: the batch helpers subtract it to zero-centre.

        In YM2612 mode every clock carries a sign-only bias of ±3 (see render_samples), so
        silence sums to 72; in YM3438 mode the non-output clocks are 0.
        """
        return _CLOCKS_PER_SAMPLE * 3 if self.mode == "ym2612" else 0

    def _clock_n(self, n: int) -> None:
        """Clock the chip n times, discarding output."""
        for _ in range(n):
            self._lib.OPN2_Clock(self._chip, self._out_ptr)

    # ------------------------------------------------------------------
    # Pipeline-flush capture
    # ------------------------------------------------------------------
    #
    # Each write_reg() advances the chip by two pipeline-flush periods of
    # _FLUSH_CLOCKS clocks each.  Since _FLUSH_CLOCKS == _CLOCKS_PER_SAMPLE,
    # that is exactly two audio samples' worth of chip time per register write.
    #
    # For one-shot sample rendering the audio produced during those flushes is
    # irrelevant and is discarded.  A continuous-timeline renderer cannot afford
    # that: 30 writes for a voice program would silently drop ~60 samples and
    # desynchronise the YM2612 against the SN76489.  Capture mode retains those
    # samples so the caller can prepend them to the frame it renders next.

    def begin_capture(self) -> None:
        """Retain pipeline-flush audio from subsequent write_reg() calls."""
        self._capture = []

    def end_capture(self) -> None:
        """Stop retaining pipeline-flush audio (back to discarding)."""
        self._capture = None

    def take_capture(self) -> list:
        """Return and clear the captured (left, right) pairs since the last call."""
        captured = self._capture or []
        if self._capture is not None:
            self._capture = []
        return captured

    def _flush(self) -> None:
        """Advance the chip by one pipeline-flush period, capturing if enabled."""
        if self._capture is None:
            self._clock_n(_FLUSH_CLOCKS)
        else:
            # render_samples(1) clocks _CLOCKS_PER_SAMPLE times — identical chip
            # time to _clock_n(_FLUSH_CLOCKS) — but keeps the audio, with the
            # same DC correction the batch renderer applies.
            self._capture.extend(self.render_samples(1))

    def write_reg(self, addr: int, data: int, bank: int = 0) -> None:
        """Write a YM2612 register with pipeline-flush clocking.

        Args:
            addr: Register address (0x00–0xFF within the bank).
            data: 8-bit value to write.
            bank: 0 for channels 0–2, 1 for channels 3–5.
        """
        port_addr = bank * 2
        port_data = bank * 2 + 1
        self._lib.OPN2_Write(self._chip, port_addr, ctypes.c_uint8(addr))
        self._flush()
        self._lib.OPN2_Write(self._chip, port_data, ctypes.c_uint8(data))
        self._flush()

    # ------------------------------------------------------------------
    # Key on / off
    # ------------------------------------------------------------------

    def key_on(self, channel: int, operators: int = 0xF) -> None:
        """Trigger a key-on for the given channel.

        Args:
            channel: YM2612 channel 0–5.
            operators: Bit mask of operators to enable (bits 3–0 = OP4 OP3 OP2 OP1).
                       Default 0xF = all four operators on.
        """
        ch_bits = channel if channel < 3 else (channel + 1)  # 0-2 → 0-2, 3-5 → 4-6
        data = ((operators & 0xF) << 4) | (ch_bits & 0x07)
        self.write_reg(_REG_KEY_ON, data, bank=0)

    def key_off(self, channel: int) -> None:
        """Release all operators on the given channel."""
        ch_bits = channel if channel < 3 else (channel + 1)
        self.write_reg(_REG_KEY_ON, ch_bits & 0x07, bank=0)

    # ------------------------------------------------------------------
    # Audio rendering
    # ------------------------------------------------------------------

    def render_samples(self, n_samples: int) -> list:
        """Clock the chip and collect n_samples stereo pairs.

        Delegates to the C helper OPN2_RenderBatch which runs the
        ``n_samples × 24`` clock loop entirely in C, writing results into
        pre-allocated int32 buffers.  In YM2612 mode the chip time-
        multiplexes six channels across the 24-clock period: each channel's
        audio appears at the four output-enable clocks where
        ``(cycles & 3) == 3``; all other clocks carry a sign-only DC bias
        of ±3.  Summing all 24 values mixes the six channels together and
        is the standard way to use Nuked-OPN2 at the native sample rate.

        A DC offset of ``_CLOCKS_PER_SAMPLE × 3 = 72`` is subtracted to
        zero-centre the waveform (silence produces 0 after this removal); in
        YM3438 mode there is no bias and nothing is subtracted (``_dc``).

        Returns:
            List of (left, right) tuples centred on 0.
        """
        buf_l = (ctypes.c_int32 * n_samples)()
        buf_r = (ctypes.c_int32 * n_samples)()
        self._lib.OPN2_RenderBatch(self._chip, n_samples, buf_l, buf_r, self._dc)
        return list(zip(buf_l, buf_r, strict=True))

    def render_mono(self, n_samples: int) -> array.array:
        """Clock the chip for n_samples and return them folded to mono, ``(L + R) // 2``.

        The fold happens in C (OPN2_RenderBatchMono) and the result comes back as an
        ``array('i')`` rather than a list of tuples: the note renderer only ever wants
        mono, and building 400 000 tuples per sample used to cost a fifth of the
        emulation time itself.  Same chip time and same values as
        ``core.pcm.to_mono(render_samples(n))``.
        """
        out = array.array('i', bytes(n_samples * 4))
        if n_samples:
            buf, _ = out.buffer_info()
            self._lib.OPN2_RenderBatchMono(self._chip, n_samples, buf, self._dc)
        return out

    def _render_samples_legacy(self, n_samples: int) -> list:
        """Original Python-loop implementation — kept for reference only.

        Replaced by render_samples() which calls OPN2_RenderBatch in C.
        """
        dc = self._dc

        out = []
        lib     = self._lib
        chip    = self._chip
        buf_ptr = self._out_ptr
        buf     = self._out
        clock   = lib.OPN2_Clock

        for _ in range(n_samples):
            l_sum = 0
            r_sum = 0
            for _ in range(_CLOCKS_PER_SAMPLE):
                clock(chip, buf_ptr)
                l_sum += buf[0]
                r_sum += buf[1]
            out.append((l_sum - dc, r_sum - dc))

        return out
