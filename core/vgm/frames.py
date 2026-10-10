"""The log cut into V-int frames: per frame, every channel's state and the writes made to it.

The driver runs once per V-int and writes its registers in one burst; the burst starts at the
same place in every frame (the log's phase), jittered a few samples by the DAC writes the Z80
interleaves, and lasts up to ~330 samples.  A frame's window starts a quarter frame before the
phase, so a whole burst lands in one frame:

    samples  ...|<-------------- 735 (NTSC) -------------->|...
                    ^ phase          burst                    next window
                |q|=========.......                        |
                 quarter frame early

The phase is the commonest burst start (mod the frame), found from the writes themselves; the
first frame is the one holding the log's first write.  This table is what the lift reads.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass

from ..chips import OPERATOR_SLOT_OFFSETS, split_operator_register
from .chipstate import FM_CHANNELS, NOISE_CHANNEL, PSG_TONE_CHANNELS, Change, ChangeKind, ChipState
from .reader import VGM_SAMPLE_RATE, VgmLog, VgmOp, VgmWrite

NTSC_RATE = 60
_BURST_GAP = 200                # samples: writes closer than this belong to one burst
_PHASE_WINDOW = 16              # samples either side of a phase that count towards it
_FRAME_LEAD_DIVISOR = 4         # a frame's window opens a quarter frame before the phase
_DAC_PAUSE_DIVISOR = 4          # DAC bytes resuming after a quarter frame's silence: a sample started
_DAC_REG = 0x2A
_VOICE_FIRST = 0x30             # DT/MUL .. SSG-EG: the operator registers
_VOICE_LAST = 0x9F
_REGISTER_STRIDE = 0x10         # from one operator register's base to the next
_REG_MODE = 0x27


@dataclass(frozen=True, slots=True)
class FmFrame:
    """One FM channel at the end of a frame, and what was written to it during the frame."""

    fnum: int
    block: int
    slots: int                  # the operator slots keyed on (0 = key-off)
    keys: tuple[int, ...]       # key writes this frame, in order: slot masks, 0 = off
    frequency_writes: int
    operators: bytes            # registers 0x30..0x9F, 4 slots each (28 bytes): the voice
    feedback_algorithm: int     # register B0
    carrier_tls: tuple[int, ...]
    pan: int                    # register B4: L R AMS PMS

    @property
    def keyed(self) -> bool:
        return self.slots != 0

    def operator(self, register: int) -> int:
        """Operator register `register` (channel 0's number: 0x30-0x9F) as the frame ends."""
        parts = split_operator_register(register)
        if parts is None:
            raise ValueError(f"register ${register:02X}: no operator register")
        base, slot, _ = parts
        group = (base - _VOICE_FIRST) // _REGISTER_STRIDE
        return self.operators[group * len(OPERATOR_SLOT_OFFSETS) + OPERATOR_SLOT_OFFSETS.index(slot)]


@dataclass(frozen=True, slots=True)
class PsgFrame:
    """One SN76489 channel at the end of a frame (the noise channel has no period)."""

    period: int
    attenuation: int
    attenuations: tuple[int, ...]   # written this frame, in order
    period_writes: int
    noise: int | None               # the noise channel's register; None on a tone channel


@dataclass(frozen=True, slots=True)
class DacStart:
    """A sample the Z80 started: at a seek, or where its bytes resume after a pause (a ripper
    seeks only where the bank does not already hold the sample next)."""

    offset: int                     # the PCM bank byte it starts on
    sample: int                     # where in the log (the Z80 starts a sample late)


@dataclass(frozen=True, slots=True)
class DacFrame:
    starts: tuple[DacStart, ...]    # the samples started this frame
    writes: int                     # DAC bytes written this frame
    since_start: int                # DAC bytes written since the last start, at the frame's end
    gaps: tuple[tuple[int, int], ...]   # (samples between consecutive DAC bytes, how often) this frame


@dataclass(frozen=True, slots=True)
class Frame:
    index: int
    sample: int                     # where the frame's window opens
    fm: tuple[FmFrame, ...]         # FM1..FM6
    psg: tuple[PsgFrame, ...]       # PSG1..PSG3, noise
    dac: DacFrame
    dac_enabled: bool
    lfo: int                        # register 0x22
    mode: int                       # register 0x27 (bit 6: FM3 special mode)

    @property
    def active(self) -> bool:
        """Anything written this frame besides the DAC's byte stream."""
        return (any(f.keys or f.frequency_writes for f in self.fm)
                or any(p.attenuations or p.period_writes for p in self.psg) or bool(self.dac.starts))


@dataclass(frozen=True)
class FrameLog:
    frames: list[Frame]
    frame_samples: int              # 735 NTSC, 882 PAL
    origin: int                     # sample where frame 0's window opens (<= 0)
    phase: int                      # where a burst starts within a frame's 735 samples
    loop_sample: int | None         # where the log loops back to
    pcm: bytes = b""                # the log's PCM bank: what DacStart.offset indexes
    end_sample: int = 0             # where the log ends (and a looping one jumps back)

    def frame_of(self, sample: int) -> int:
        return (sample - self.origin) // self.frame_samples

    def burst_frame(self, sample: int) -> int:
        """The frame whose driver burst `sample` follows: what the Z80 does a little after the
        68k asked (a DAC sample started) belongs to the frame that asked."""
        return self.frame_of(sample - self.frame_samples // _FRAME_LEAD_DIVISOR)

    @property
    def loop_frame(self) -> int | None:
        return None if self.loop_sample is None else self.frame_of(self.loop_sample)

    def seconds(self, frame: int) -> float:
        return (self.origin + frame * self.frame_samples) / VGM_SAMPLE_RATE


def frame_log(log: VgmLog, state: ChipState | None = None) -> FrameLog:
    """`log` cut into frames (`state`: the chips to replay into, with the log's clocks by default)."""
    state = state or ChipState.for_log(log)
    frame_samples = VGM_SAMPLE_RATE // (log.header.rate or NTSC_RATE)
    phase = _burst_phase(log.writes, frame_samples)
    lead = phase - frame_samples // _FRAME_LEAD_DIVISOR
    origin = -((frame_samples - lead % frame_samples) % frame_samples)

    builder = _FrameBuilder(state, origin, frame_samples)
    return FrameLog(list(builder.run(log)), frame_samples, origin, phase, log.loop_sample, log.pcm, log.end_sample)


def _is_dac_byte(w: VgmWrite) -> bool:
    return w.op is VgmOp.FM and w.port == 0 and w.reg == _DAC_REG


def _burst_phase(writes: list[VgmWrite], frame_samples: int) -> int:
    """The commonest burst start within a frame, counting starts within _PHASE_WINDOW of it."""
    starts: list[int] = []
    last = None
    for w in writes:
        if _is_dac_byte(w) or w.op is VgmOp.PCM_SEEK:
            continue
        if last is None or w.sample - last > _BURST_GAP:
            starts.append(w.sample % frame_samples)
        last = w.sample
    if not starts:
        return 0

    # A circular histogram, smoothed: jitter spreads one phase over a few samples
    hist = Counter(starts)
    def weight(p: int) -> int:
        return sum(hist.get((p + d) % frame_samples, 0) for d in range(-_PHASE_WINDOW, _PHASE_WINDOW + 1))
    return max(range(frame_samples), key=lambda p: (weight(p), hist.get(p, 0)))


class _FrameBuilder:
    """Replays the log, closing a Frame each time a write falls past the current one."""

    def __init__(self, state: ChipState, origin: int, frame_samples: int) -> None:
        self._state = state
        self._origin = origin
        self._frame_samples = frame_samples
        self._index = 0
        self._reset_events()
        self._since_start = 0
        self._last_dac: int | None = None
        self._pcm_at = 0                # the bank byte the next DAC write plays

    def _reset_events(self) -> None:
        self._keys: list[list[int]] = [[] for _ in range(FM_CHANNELS)]
        self._freq_writes = [0] * FM_CHANNELS
        self._atts: list[list[int]] = [[] for _ in range(PSG_TONE_CHANNELS + 1)]
        self._period_writes = [0] * PSG_TONE_CHANNELS
        self._starts: list[DacStart] = []
        self._dac_writes = 0
        self._gaps: Counter[int] = Counter()

    def run(self, log: VgmLog) -> Iterator[Frame]:
        writes = log.writes
        for i, w in enumerate(writes):
            # Close every frame before this write's
            index = (w.sample - self._origin) // self._frame_samples
            while self._index < index:
                yield self._close()

            # The DAC's byte stream, most of a log's writes, is only counted: no state reads 0x2A
            if w.reg == _DAC_REG and w.port == 0 and w.op is VgmOp.FM:
                self._dac_write(w.sample)
                continue
            change = self._state.apply(w, writes[i + 1] if i + 1 < len(writes) else None)
            if change is not None:
                self._record(change)

        # Frames to the log's end
        last = (max(log.end_sample - 1, 0) - self._origin) // self._frame_samples
        while self._index <= last:
            yield self._close()

    def _record(self, change: Change) -> None:
        state, kind, ch = self._state, change.kind, change.channel
        if kind is ChangeKind.FM_KEY:
            self._keys[ch].append(state.fm_slots(ch))
        elif kind is ChangeKind.FM_FREQUENCY:
            self._freq_writes[ch] += 1
        elif kind is ChangeKind.PSG_VOLUME:
            self._atts[ch].append(state.psg_attenuation(ch))
        elif kind is ChangeKind.PSG_TONE:
            self._period_writes[ch] += 1
        elif kind is ChangeKind.PCM_SEEK:
            self._pcm_at = change.value
            self._start(change.sample)

    def _start(self, sample: int) -> None:
        """A new sample: the silence before it is no gap of the byte stream."""
        self._starts.append(DacStart(self._pcm_at, sample))
        self._since_start = 0
        self._last_dac = None

    def _dac_write(self, sample: int) -> None:
        # Bytes resuming after a pause, no seek: the bank holds the next sample where the last ended
        if self._last_dac is not None and sample - self._last_dac > self._frame_samples // _DAC_PAUSE_DIVISOR:
            self._start(sample)

        self._dac_writes += 1
        self._since_start += 1
        self._pcm_at += 1
        if self._last_dac is not None:
            self._gaps[sample - self._last_dac] += 1
        self._last_dac = sample

    def _close(self) -> Frame:
        s = self._state
        fm = tuple(FmFrame(*s.fm_fnum_block(ch), s.fm_slots(ch), tuple(self._keys[ch]), self._freq_writes[ch],
                           s.fm_operator_regs(ch, _VOICE_FIRST, _VOICE_LAST), s.fm_feedback_algorithm(ch),
                           s.fm_carrier_tls(ch), s.fm_pan(ch))
                   for ch in range(FM_CHANNELS))
        psg = tuple(PsgFrame(s.psg_period(ch) if ch < PSG_TONE_CHANNELS else 0, s.psg_attenuation(ch),
                             tuple(self._atts[ch]), self._period_writes[ch] if ch < PSG_TONE_CHANNELS else 0,
                             s.noise if ch == NOISE_CHANNEL else None)
                    for ch in range(PSG_TONE_CHANNELS + 1))
        dac = DacFrame(tuple(self._starts), self._dac_writes, self._since_start, tuple(sorted(self._gaps.items())))
        frame = Frame(self._index, self._origin + self._index * self._frame_samples, fm, psg, dac,
                      s.dac_enabled, s.lfo, s.fm_global(_REG_MODE))
        self._index += 1
        self._reset_events()
        return frame

