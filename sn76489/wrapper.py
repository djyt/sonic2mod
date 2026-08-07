"""ctypes wrapper around the VGMPlay SN76489 emulator.

Usage::

    sn = SN76489(clock_rate=3_546_895, sample_rate=44100)
    sn.write_tone_freq(0, 253)   # channel 0, N=253
    sn.write_volume(0, 0)        # max volume
    samples = sn.render_samples(44100)   # 1 second — list of (L, R) int32 tuples
    sn.shutdown()

Register protocol
-----------------
Tone frequency (ch 0-2), 10-bit divider N:
  Byte 1 (latch):  1 CC 0 NNNN    (low 4 bits)
  Byte 2 (data):   0 0 NNNNNN     (high 6 bits, i.e. N >> 4)

Volume (any ch):
  Latch:  1 CC 1 VVVV    (0 = max, 15 = silent)

Noise (ch 3):
  Latch:  0xE0 | (fb << 2) | rate
    fb  : 0 = periodic, 1 = white
    rate: 0/1/2 = N/512, N/1024, N/2048; 3 = follow tone ch 2

Mega Drive config: FB_SEGAVDP=0x0009, SRW_SEGAVDP=16, boost_noise=1.
"""

import ctypes
from ctypes import POINTER, c_int32, cast

from .build import get_lib_path

# ---------------------------------------------------------------------------
# Sega VDP chip constants (from sn76489.h)
# ---------------------------------------------------------------------------
_FB_SEGAVDP  = 0x0009
_SRW_SEGAVDP = 16
_BOOST_NOISE = 1


# ---------------------------------------------------------------------------
# ctypes DLL interface
# ---------------------------------------------------------------------------

def _load_lib() -> ctypes.CDLL:
    lib = ctypes.CDLL(str(get_lib_path()))

    # SN76489_Context* SN76489_Init(int PSGClockValue, int SamplingRate)
    lib.SN76489_Init.restype  = ctypes.c_void_p
    lib.SN76489_Init.argtypes = [ctypes.c_int, ctypes.c_int]

    # void SN76489_Reset(SN76489_Context* chip)
    lib.SN76489_Reset.restype  = None
    lib.SN76489_Reset.argtypes = [ctypes.c_void_p]

    # void SN76489_Shutdown(SN76489_Context* chip)
    lib.SN76489_Shutdown.restype  = None
    lib.SN76489_Shutdown.argtypes = [ctypes.c_void_p]

    # void SN76489_Config(SN76489_Context* chip, int feedback, int sr_width, int boost_noise)
    lib.SN76489_Config.restype  = None
    lib.SN76489_Config.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]

    # void SN76489_Write(SN76489_Context* chip, int data)
    lib.SN76489_Write.restype  = None
    lib.SN76489_Write.argtypes = [ctypes.c_void_p, ctypes.c_int]

    # void SN76489_Update(SN76489_Context* chip, INT32 **buffer, int length)
    lib.SN76489_Update.restype  = None
    lib.SN76489_Update.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]

    # void SN76489_SetMute(SN76489_Context* chip, int val)
    lib.SN76489_SetMute.restype  = None
    lib.SN76489_SetMute.argtypes = [ctypes.c_void_p, ctypes.c_int]

    return lib


# ---------------------------------------------------------------------------
# SN76489 class
# ---------------------------------------------------------------------------

class SN76489:
    """Python interface to the VGMPlay SN76489 emulator."""

    def __init__(self, clock_rate: int = 3_546_895, sample_rate: int = 44100):
        """Initialise the chip.

        Args:
            clock_rate:  SN76489 clock frequency (Hz). NTSC MD = 3,579,545.
            sample_rate: Output sample rate (Hz). Set to target_rate per instrument
                         to avoid the need for resampling.
        """
        self._lib = _load_lib()
        self._clock_rate = clock_rate
        self._sample_rate = sample_rate
        self._chip = self._lib.SN76489_Init(clock_rate, sample_rate)
        if not self._chip:
            raise RuntimeError("SN76489_Init returned NULL")
        self.reset()

    def reset(self) -> None:
        """Reset chip state and re-apply the Sega VDP configuration.

        SN76489_Init has its SN76489_Reset call commented out in the C source, so this
        must run at least once after construction — otherwise Registers[], ToneFreqVals[]
        and IntermediatePos[] hold garbage malloc memory, which causes out-of-bounds
        PSGVolumeValues reads.

        Also lets a single instance be reused across many renders without the malloc/free
        churn of constructing a new chip each time.  Note the output sample rate is baked
        in at SN76489_Init and cannot be changed here.
        """
        self._lib.SN76489_Reset(self._chip)
        self._lib.SN76489_Config(self._chip, _FB_SEGAVDP, _SRW_SEGAVDP, _BOOST_NOISE)
        # Enable all channels
        self._lib.SN76489_SetMute(self._chip, 0x0F)

    # ------------------------------------------------------------------
    # Low-level write
    # ------------------------------------------------------------------

    def write(self, data: int) -> None:
        """Send a raw 8-bit command byte to the chip."""
        self._lib.SN76489_Write(self._chip, data & 0xFF)

    # ------------------------------------------------------------------
    # Convenience writes
    # ------------------------------------------------------------------

    def write_tone_freq(self, ch: int, n: int) -> None:
        """Set tone channel ch (0-2) frequency divider N (1-1023).

        Sends the 2-byte latch+data protocol for the 10-bit divider.
        """
        n = max(1, min(1023, n))
        # Latch byte: 1 CC 0 NNNN  (low 4 bits of N)
        latch = 0x80 | ((ch & 0x03) << 5) | (n & 0x0F)
        # Data byte:  0 0 NNNNNN   (high 6 bits of N = bits 9:4)
        data  = (n >> 4) & 0x3F
        self.write(latch)
        self.write(data)

    def write_volume(self, ch: int, vol: int) -> None:
        """Set volume for channel ch (0-3). vol: 0=max, 15=silent."""
        vol = max(0, min(15, vol))
        # Latch: 1 CC 1 VVVV
        latch = 0x90 | ((ch & 0x03) << 5) | (vol & 0x0F)
        self.write(latch)

    def write_noise(self, white: bool, rate: int) -> None:
        """Configure noise channel (ch 3).

        Args:
            white: True = white noise, False = periodic noise.
            rate:  0/1/2 = N/512, N/1024, N/2048 dividers; 3 = follow tone ch 2.
        """
        fb   = 1 if white else 0
        rate = max(0, min(3, rate))
        self.write(0xE0 | (fb << 2) | rate)

    # ------------------------------------------------------------------
    # Audio rendering
    # ------------------------------------------------------------------

    def render_samples(self, n: int) -> list:
        """Render n samples.  Returns list of (L, R) int32 tuples.

        Uses SN76489_Update with stereo INT32 buffers.
        Max amplitude is ~±4096 per channel (from PSGVolumeValues table).
        """
        buf_l = (c_int32 * n)()
        buf_r = (c_int32 * n)()
        buf_ptrs = (POINTER(c_int32) * 2)(
            cast(buf_l, POINTER(c_int32)),
            cast(buf_r, POINTER(c_int32)),
        )
        self._lib.SN76489_Update(self._chip, buf_ptrs, n)
        return [(buf_l[i], buf_r[i]) for i in range(n)]

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Free the chip context.  Do not use the object after calling this."""
        if self._chip:
            self._lib.SN76489_Shutdown(self._chip)
            self._chip = None

    def __del__(self):
        self.shutdown()
