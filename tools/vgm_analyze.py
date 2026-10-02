#!/usr/bin/env python3
"""YM2612 + SN76489 pitch/event analyzer for VGM/VGZ files.

Parses YM2612 register writes and SN76489 register writes, and outputs a
table of key-on events showing channel, frequency data, and nearest note
name.  Use this to verify synth_root values against the actual chip output
from a game recording, or to count PSG noise events.

Usage::

    python tools/vgm_analyze.py "reference/vgz/02 - Green Hill Zone.vgz"
    python tools/vgm_analyze.py file.vgz --chip fm --channel FM3 FM4 FM5
    python tools/vgm_analyze.py file.vgz --chip psg --channel NOISE
    python tools/vgm_analyze.py file.vgz --chip all --max-rows 0
    python tools/vgm_analyze.py file.vgz --max-rows 500 --clock 7670454

Output columns (FM):
    time_ms   — milliseconds from track start (VGM 44100 Hz sample clock)
    chan      — FM1..FM6
    fnum      — raw frequency number written to YM2612
    blk       — block (octave shift), 0–7
    freq_hz   — computed frequency using chip formula
    note      — nearest semitone name in standard pitch (e.g. G3, C#2)
    smps      — same note in SMPS-assembly convention (FM label is one octave
                lower than chip output, so A6 chip → A5 SMPS label)

Output columns (PSG tone):
    time_ms   — milliseconds from track start (a row = the channel becoming audible, or its
                period leaving the current note by more than --psg-mod-cents; smaller moves
                are smpsModSet vibrato steps and do not start a row)
    chan      — PSG1..PSG3
    period    — 10-bit SN76489 period register value
    —         — (blk column unused, shown as —)
    freq_hz   — computed frequency: clock / (32 * period)
    note      — nearest semitone name in standard pitch
    smps      — same note in SMPS-assembly convention (PSG label is one octave
                higher than chip output, so E4 chip → E5 SMPS label)

Output columns (PSG noise):
    time_ms   — milliseconds from track start
    chan      — NOISE
    noise_reg — 3-bit noise register (bit2=type, bits[1:0]=rate)
    —         — (blk column unused)
    freq_hz   — LFSR shift rate: clock/512, /1024, /2048 for rates 0–2, or
                clock/(32·N) when rate 3 follows tone ch2 (N=0 treated as 1,
                as the Sega VDP PSG does)
    note      — decoded noise type string (e.g. white/N/512, white/tone2 N=0)
    smps      — "—" (no pitch meaning for noise)

Output columns (DAC, --chip dac / all):
    time_ms   — milliseconds from track start
    chan      — DAC
    data      — PCM bank offset the stream seeks to (0xE0 command); a seek
                marks a sample start, so the offset identifies the sample
                (kick/snare/…).  Some loggers merge back-to-back restarts of
                the same sample, so counts can be lower than the SMPS data.
    note      — "seek <offset>"
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import math
import statistics
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.audio import db_to_gain
from core.smps import CARRIER_OFFSETS_BY_ALG, MD_FM_CLOCK, MD_PSG_CLOCK, fm_level_db, psg_level_db

# ---------------------------------------------------------------------------
# Frequency math
# ---------------------------------------------------------------------------

# VGM files use 44100 Hz as the sample clock for wait commands.
_VGM_SAMPLE_RATE = 44100

# The recording's chip clocks, Sonic 1 on an NTSC Mega Drive.  Override with --clock / --psg-clock.
DEFAULT_FM_CLOCK = MD_FM_CLOCK
DEFAULT_PSG_CLOCK = MD_PSG_CLOCK

_NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

def fnum_to_hz(fnum: int, block: int, clock: int) -> float:
    """Convert YM2612 fnum/block pair to frequency in Hz.

    Formula: freq = clock x fnum / (144 x 2^(21 - block))

    YM2612: f0 = Fnum x (fM/144) x 2^B / 2^21.  Cross-check with the Sonic 1 driver, whose table
    macro is MakeFMFrequency(f) = f x 2^21 / FM_Sample_Rate at block 0 (16.35 Hz = C0 -> $0284).
    A4 = 440 Hz -> fnum=1083, block=4 with clock=7670454.

    (Until 2026-09 this used 2^(20 - block) and reported every FM pitch one octave high; the
    synthesiser had the mirror-image error, so configs tuned from this tool sounded right.)
    """
    return clock * fnum / (144 * (1 << (21 - block)))


def _psg_period_to_hz(period: int, clock: int) -> float:
    """Convert SN76489 10-bit tone period to frequency in Hz.

    Formula: freq = clock / (32 * period)
    """
    if period <= 0:
        return 0.0
    return clock / (32 * period)


def _nearest_note(freq: float) -> str:
    """Return the nearest note name (e.g. 'G3', 'C#2') for a frequency in Hz."""
    if freq <= 0:
        return "---"
    midi = round(69 + 12 * math.log2(freq / 440.0))
    name = _NOTE_NAMES[midi % 12]
    octave = midi // 12 - 1
    return f"{name}{octave}"


def _smps_note(freq: float, chan_type: str) -> str:
    """Return the SMPS-convention note name for a chip output frequency.

    In Sonic 1 SMPS the note byte labels relate to standard pitch like this:
      FM  — label IS the standard pitch name (driver table: nC0 -> 16.35 Hz = C0), so the
            effective note (byte + pitch_offset) is simply the chip's note
            e.g. nA4 with pitch_offset 0 → chip plays A4 = 440 Hz
      PSG — label is one octave *higher* than what the chip outputs
            e.g. nE5 in the assembly → chip plays E4 (standard)
            Conversion: add 12 to MIDI number
      NOISE — no pitch; returns "—"

    Args:
        freq:      chip output frequency in Hz (> 0)
        chan_type: "fm", "psg", or "noise"
    """
    if freq <= 0 or chan_type == "noise":
        return "—"
    midi = round(69 + 12 * math.log2(freq / 440.0))
    if chan_type == "psg":
        midi += 12
    elif chan_type != "fm":
        return "—"
    if midi < 0 or midi > 127:
        return "—"
    name = _NOTE_NAMES[midi % 12]
    octave = midi // 12 - 1
    return f"{name}{octave}"


# ---------------------------------------------------------------------------
# VGM parser
# ---------------------------------------------------------------------------

# VGM commands (VGM spec 1.71)
_CMD_PSG       = 0x50
_CMD_FM        = (0x52, 0x53)   # port 0 (FM1-3), port 1 (FM4-6)
_CMD_WAIT      = 0x61        # 16-bit sample count follows
_CMD_END       = 0x66
_CMD_DATA      = 0x67        # data block: 0x66, type, 32-bit size, data
_CMD_PCM_SEEK  = 0xE0        # marks the start of a DAC sample (kick/snare/...)

# Commands that only wait: samples each one waits.  0x8n writes a DAC byte from the PCM bank first.
_WAITS = {
    0x62: 735,               # 1 NTSC frame
    0x63: 882,               # 1 PAL frame
    **{cmd: (cmd & 0x0F) + 1 for cmd in range(0x70, 0x80)},
    **{cmd: cmd & 0x0F for cmd in range(0x80, 0x90)},
}

# DAC stream control commands: parameter bytes
_DAC_STREAM_LENGTHS = {0x90: 4, 0x91: 4, 0x92: 5, 0x93: 10, 0x94: 1, 0x95: 4}

_PSG_SILENT = 0xF            # 4-bit attenuation
_NOISE = 3                   # the SN76489's noise channel
_KEY_REG = 0x28              # YM2612 key-on/off, always via port 0
_HEADER_CLOCK_FLAGS = 0x3FFF_FFFF


def _data_start(data: bytes) -> int:
    """Offset of the first command: version >= 1.50 gives it relative to 0x34."""
    version = struct.unpack_from('<I', data, 0x08)[0]
    if version < 0x150:
        return 0x40
    rel = struct.unpack_from('<I', data, 0x34)[0]
    return (0x34 + rel) if rel else 0x40


def _header_clock(data: bytes, offset: int, default: int) -> int:
    """A chip clock from the header (FM 0x2C, PSG 0x0C), its flag bits stripped; 0 = not given."""
    clock = struct.unpack_from('<I', data, offset)[0]
    return clock & _HEADER_CLOCK_FLAGS if clock else default


def _psg_data_byte_next(data: bytes, pos: int) -> bool:
    """The next command is a PSG write of a data byte (not a latch)."""
    return pos + 1 < len(data) and data[pos] == _CMD_PSG and not data[pos + 1] & 0x80


class _ChipLog:
    """YM2612 + SN76489 register state while a VGM log plays, and the key-on rows it yields.

    `samples` is the VGM sample clock, advanced by the caller.
    """

    def __init__(self, fm_clock: int, psg_clock: int, channel_filter: set[str] | None,
                 psg_mod_cents: float) -> None:
        self._fm_clock = fm_clock
        self._psg_clock = psg_clock
        self._filter = channel_filter
        self._psg_mod_cents = psg_mod_cents
        self.samples = 0
        self.rows: list[tuple] = []
        self.fm_amp: dict[str, list[float]] = {}    # chan_name -> linear amplitudes at key-on
        self.psg_amp: dict[str, list[float]] = {}   # "PSG1".."PSG3","NOISE" -> the same

        # FM per [bank][channel]: bank 0 = FM1-3, bank 1 = FM4-6
        self._fnum_lo = [[0, 0, 0], [0, 0, 0]]
        self._fnum_hi = [[0, 0, 0], [0, 0, 0]]
        # Key state, and the frequency the sounding note was keyed on with.  The Sonic 1 driver's
        # FMNoteOn writes key-on unconditionally; under smpsNoAttack only the key-OFF is skipped, so a
        # tie (`nA5, $10, smpsNoAttack, $3B`) logs a second key-on that the chip ignores.
        self._fm_keyed = [[False] * 3, [False] * 3]
        self._fm_keyed_hz = [[0.0] * 3, [0.0] * 3]
        # Amplitude: TL per slot and algorithm per channel
        self._fm_tl: list[list[dict[int, int]]] = [[{0x00: 0, 0x04: 0, 0x08: 0, 0x0C: 0} for _ in range(3)]
                                                   for _ in range(2)]
        self._fm_algo = [[0] * 3 for _ in range(2)]

        # PSG
        self._psg_vol = [_PSG_SILENT] * 4
        self._psg_freq = [0, 0, 0]          # 10-bit tone period
        self._psg_prev_freq = [0, 0, 0]     # period at the last key-on
        self._psg_noise = 0                 # 3-bit noise register
        self._latch_ch: int | None = None   # last latched channel (0-3)
        self._latch_type: int | None = None  # 0 = freq, 1 = vol

    def _now_ms(self) -> float:
        return self.samples * 1000.0 / _VGM_SAMPLE_RATE

    def _wanted(self, ch_name: str) -> bool:
        return not self._filter or ch_name in self._filter

    # ---- YM2612 ----

    def fm_write(self, bank: int, reg: int, val: int) -> None:
        """A register write to port `bank`."""
        if 0x40 <= reg <= 0x4E and (reg & 0x03) != 0x03:
            # TL: bits[3:2] = slot offset (0x00/0x04/0x08/0x0C), bits[1:0] = channel
            self._fm_tl[bank][reg & 0x03][reg & 0x0C] = val & 0x7F
        elif 0xB0 <= reg <= 0xB2:
            self._fm_algo[bank][reg - 0xB0] = val & 0x07         # feedback + algorithm
        elif 0xA0 <= reg <= 0xA2:
            self._fnum_lo[bank][reg - 0xA0] = val                # F-number low byte
        elif 0xA4 <= reg <= 0xA6:
            self._fnum_hi[bank][reg - 0xA4] = val                # block + F-number high bits
        elif reg == _KEY_REG and bank == 0:
            self._fm_key(val)

    def _fnum_block(self, bank: int, ch: int) -> tuple[int, int]:
        hi = self._fnum_hi[bank][ch]
        return ((hi & 0x7) << 8) | self._fnum_lo[bank][ch], (hi >> 3) & 0x7

    def _fm_key(self, val: int) -> None:
        """Key-on/off: a row where a key-on starts a note."""
        ch_raw = val & 0x07
        if ch_raw == 3:
            return   # unused slot
        bank, ch = (1, ch_raw - 4) if ch_raw >= 4 else (0, ch_raw)
        if not (val >> 4) & 0x0F:
            self._fm_keyed[bank][ch] = False
            return

        # A key-on while already keyed on re-attacks nothing.  It is still a note when the pitch moved
        # to another note (a legato slide); within psg_mod_cents of where the note was keyed on it is a
        # tie, or a tie with a new smpsDetune (Scrap Brain FM4 scoops every phrase start up by 36 cents).
        hz = fnum_to_hz(*self._fnum_block(bank, ch), self._fm_clock)
        was = self._fm_keyed_hz[bank][ch]
        if (self._fm_keyed[bank][ch] and hz > 0 and was > 0
                and abs(1200.0 * math.log2(hz / was)) <= max(self._psg_mod_cents, 1e-9)):
            return
        self._fm_keyed[bank][ch], self._fm_keyed_hz[bank][ch] = True, hz
        self._emit_fm_keyon(bank, ch)

    def _emit_fm_keyon(self, bank: int, ch: int) -> None:
        fnum, block = self._fnum_block(bank, ch)
        if fnum == 0:
            return
        ch_name = f"FM{bank * 3 + ch + 1}"
        freq = fnum_to_hz(fnum, block, self._fm_clock)
        if self._wanted(ch_name):
            self.rows.append((self._now_ms(), ch_name, fnum, block, freq, _nearest_note(freq), _smps_note(freq, "fm")))

        # Amplitude, whatever the filter: the carriers' linear levels summed
        tl = self._fm_tl[bank][ch]
        linear = sum(db_to_gain(fm_level_db(tl[s])) for s in sorted(CARRIER_OFFSETS_BY_ALG[self._fm_algo[bank][ch]]))
        self.fm_amp.setdefault(ch_name, []).append(linear)

    # ---- SN76489 ----

    def psg_write(self, b: int, data_byte_next: bool) -> None:
        """One byte written; `data_byte_next`: the next command writes a data byte."""
        if b & 0x80:
            self._psg_latch(b, data_byte_next)
            return
        ch = self._latch_ch
        if ch is None or self._latch_type != 0 or ch >= _NOISE:
            return

        # Data byte: high 6 bits of the tone period.  An audible channel's period moving is a note.
        self._psg_freq[ch] = (b & 0x3F) << 4 | (self._psg_freq[ch] & 0xF)
        if self._psg_new_note(ch):
            self._emit_psg_keyon(ch)

    def _psg_latch(self, b: int, data_byte_next: bool) -> None:
        """Latch byte: channel, type and the low nibble."""
        ch, typ, nib = (b >> 5) & 3, (b >> 4) & 1, b & 0xF
        self._latch_ch, self._latch_type = ch, typ

        # Volume: silent -> audible is a key-on
        if typ == 1:
            old = self._psg_vol[ch]
            self._psg_vol[ch] = nib
            if old == _PSG_SILENT and nib < _PSG_SILENT:
                self._emit_psg_keyon(ch)
            return

        if ch == _NOISE:
            self._psg_noise = nib & 0x7
            return

        # Tone period, low nibble.  The driver always follows the latch with the high-bits byte;
        # judging the half-written period would report a note that never sounds, so wait for it.
        self._psg_freq[ch] = (self._psg_freq[ch] & 0x3F0) | nib
        if not data_byte_next and self._psg_new_note(ch):
            self._emit_psg_keyon(ch)

    def _psg_new_note(self, ch: int) -> bool:
        """Audible, and the period has left the current note (see psg_mod_cents)."""
        new, old = self._psg_freq[ch], self._psg_prev_freq[ch]
        if self._psg_vol[ch] >= _PSG_SILENT or new == old:
            return False
        if new <= 0 or old <= 0 or self._psg_mod_cents <= 0:
            return True
        return abs(1200.0 * math.log2(new / old)) > self._psg_mod_cents

    def _emit_psg_keyon(self, ch: int) -> None:
        """A key-on: the volume going audible, or the period moving while audible (portamento /
        arpeggio without intervening silence)."""
        if ch == _NOISE:
            self._emit_noise_keyon()
            return
        ch_name = f"PSG{ch + 1}"
        period = self._psg_freq[ch]
        freq = _psg_period_to_hz(period, self._psg_clock)
        self._psg_prev_freq[ch] = period   # so the same period does not emit twice
        if self._wanted(ch_name):
            self.rows.append((self._now_ms(), ch_name, period, 0, freq, _nearest_note(freq), _smps_note(freq, "psg")))
        self.psg_amp.setdefault(ch_name, []).append(db_to_gain(psg_level_db(self._psg_vol[ch])))

    def _emit_noise_keyon(self) -> None:
        shift_hz, desc = self._noise_shift()
        if self._wanted("NOISE"):
            self.rows.append((self._now_ms(), "NOISE", self._psg_noise, 0, shift_hz, desc, "—"))
        self.psg_amp.setdefault("NOISE", []).append(db_to_gain(psg_level_db(self._psg_vol[_NOISE])))

    def _noise_shift(self) -> tuple[float, str]:
        """(LFSR shift rate Hz, description) of the noise register: 'white/N/512', 'white/tone2 N=0'."""
        noise_type = "white" if (self._psg_noise >> 2) & 1 else "periodic"
        rate = self._psg_noise & 0x3
        if rate != 3:
            return self._psg_clock / float(512 << rate), f"{noise_type}/{('N/512', 'N/1024', 'N/2048')[rate]}"

        # Clocked by tone ch2.  The Sonic 1 driver writes PSG3's own note divider there ($C0); nMaxPSG
        # maps to N=0, which the Sega VDP PSG treats as N=1 (maximum shift rate, near-white hiss).
        n2 = self._psg_freq[2]
        return self._psg_clock / (32.0 * (n2 if n2 > 0 else 1)), f"{noise_type}/tone2 N={n2}"

    # ---- DAC ----

    def dac_seek(self, offset: int) -> None:
        if self._wanted("DAC"):
            self.rows.append((self._now_ms(), "DAC", offset, 0, 0.0, f"seek {offset}", "—"))


def parse_vgm(
    data: bytes,
    fm_clock: int,
    psg_clock: int,
    channel_filter: set[str] | None,
    chip: str,
    psg_mod_cents: float = 70.0,
) -> tuple[list[tuple], dict[str, list[float]], dict[str, list[float]]]:
    """Parse VGM binary data and return a list of key-on event rows.

    An FM row is a key-on that starts a note: the driver also writes key-on for a tie (it only
    skips the key-OFF under smpsNoAttack), and that is not a row unless the pitch moved by more
    than ``psg_mod_cents``.

    The SN76489 has no key-on, so a PSG tone row starts when the channel becomes audible or when
    its period moves more than ``psg_mod_cents`` away from the period the current note started on.
    Smaller moves are the driver's modulation (smpsModSet rewrites the divider every few frames)
    and stay inside the note; pass 0 to get a row for every period write.

    FM rows:       (time_ms, chan_name, fnum, block, freq_hz, note_name)
    PSG tone rows: (time_ms, chan_name, period, 0, freq_hz, note_name)
    PSG noise rows:(time_ms, "NOISE",  noise_reg, 0, 0.0, noise_desc)

    Returns (rows, fm_amp_samples, psg_amp_samples).
    """
    fm_on, psg_on, dac_on = chip in ('fm', 'all'), chip in ('psg', 'all'), chip in ('dac', 'all')
    if fm_on:
        fm_clock = _header_clock(data, 0x2C, fm_clock)
    if psg_on:
        psg_clock = _header_clock(data, 0x0C, psg_clock)
    log = _ChipLog(fm_clock, psg_clock, channel_filter, psg_mod_cents)

    pos, end = _data_start(data), len(data)
    while pos < end:
        cmd = data[pos]
        pos += 1

        # Chip writes; the chips not analysed are skipped
        if cmd in _CMD_FM:
            if not fm_on:
                pos += 2
                continue
            if pos + 1 >= end:
                break
            log.fm_write(cmd - _CMD_FM[0], data[pos], data[pos + 1])
            pos += 2
        elif cmd == _CMD_PSG:
            if not psg_on:
                pos += 1
                continue
            if pos >= end:
                break
            pos += 1
            log.psg_write(data[pos - 1], _psg_data_byte_next(data, pos))

        # Time
        elif cmd in _WAITS:
            log.samples += _WAITS[cmd]
        elif cmd == _CMD_WAIT:
            if pos + 1 >= end:
                break
            log.samples += struct.unpack_from('<H', data, pos)[0]
            pos += 2
        elif cmd == _CMD_END:
            break

        # Data blocks and the DAC stream
        elif cmd == _CMD_DATA:
            if pos + 5 >= end:
                break
            pos += 2   # compat byte (always 0x66) and type byte
            pos += 4 + struct.unpack_from('<I', data, pos)[0]
        elif cmd == _CMD_PCM_SEEK:
            if pos + 4 > end:
                break
            if dac_on:
                log.dac_seek(struct.unpack_from('<I', data, pos)[0])
            pos += 4
        elif cmd in _DAC_STREAM_LENGTHS:
            pos += _DAC_STREAM_LENGTHS[cmd]

        # Unknown commands: skip 1 byte and continue (best-effort)

    return log.rows, log.fm_amp, log.psg_amp


# ---------------------------------------------------------------------------
# Volume suggestion
# ---------------------------------------------------------------------------

_CHANNEL_ORDER = ["FM1", "FM2", "FM3", "FM4", "FM5", "FM6", "PSG1", "PSG2", "PSG3", "NOISE"]


def print_volume_suggestions(
    fm_amp: dict[str, list[float]],
    psg_amp: dict[str, list[float]],
) -> None:
    """Print per-channel amplitude stats and suggested sample_list volumes (0–64)."""
    all_channels: dict[str, float] = {}
    for name, samples in {**fm_amp, **psg_amp}.items():
        if samples:
            all_channels[name] = statistics.median(samples)

    if not all_channels:
        print("No key-on events found.")
        return

    peak = max(all_channels.values())

    print(f"\n{'Channel':<8}  {'Median amp':>12}  {'Rel %':>7}  {'MOD vol (0-64)':>14}")
    print("-" * 50)
    for ch in _CHANNEL_ORDER:
        if ch not in all_channels:
            continue
        amp = all_channels[ch]
        rel = amp / peak
        vol = round(rel * 56)   # max → 56, leaving headroom for Cxx boosts
        print(f"{ch:<8}  {amp:>12.4f}  {rel * 100:>6.1f}%  {vol:>14}")

    print()
    print("# Suggested volumes for sample_list entries:")
    for ch in _CHANNEL_ORDER:
        if ch not in all_channels:
            continue
        vol = round(all_channels[ch] / peak * 56)
        print(f"# {ch}: volume ~{vol}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Analyze YM2612 FM and SN76489 PSG channel pitches from a VGM/VGZ recording",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("file", help="VGM or VGZ file to analyze")
    ap.add_argument(
        "--chip", choices=("fm", "psg", "dac", "all"), default="fm",
        help="Which chip to analyze: fm (default), psg, dac (PCM seeks), or all",
    )
    ap.add_argument(
        "--channel", nargs="+", metavar="CH",
        help="Show only these channels (e.g. --channel FM3 FM4 / --channel PSG1 NOISE DAC)",
    )
    ap.add_argument(
        "--clock", type=int, default=DEFAULT_FM_CLOCK,
        help=f"YM2612 clock in Hz (default {DEFAULT_FM_CLOCK}; read from file if present)",
    )
    ap.add_argument(
        "--psg-clock", type=int, default=DEFAULT_PSG_CLOCK,
        dest="psg_clock",
        help=f"SN76489 clock in Hz (default {DEFAULT_PSG_CLOCK}; read from file if present)",
    )
    ap.add_argument(
        "--psg-mod-cents", type=float, default=70.0, dest="psg_mod_cents", metavar="CENTS",
        help="PSG period moves within CENTS of the note's starting pitch are modulation, not a new "
             "note (default 70; 0 = one row per period write)",
    )
    ap.add_argument(
        "--max-rows", type=int, default=200,
        help="Maximum rows to print (default 200; use 0 for unlimited)",
    )
    ap.add_argument(
        "--volumes", action="store_true",
        help="Print per-channel amplitude stats and suggested sample_list volumes instead of event table",
    )
    args = ap.parse_args()

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    raw = path.read_bytes()

    # Detect and decompress VGZ (gzip-compressed VGM)
    if path.suffix.lower() == ".vgz" or raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)

    if raw[:4] != b'Vgm ':
        print("ERROR: not a valid VGM file (bad magic bytes)", file=sys.stderr)
        sys.exit(1)

    channel_filter = set(args.channel) if args.channel else None
    rows, fm_amp, psg_amp = parse_vgm(
        raw,
        fm_clock=args.clock,
        psg_clock=args.psg_clock,
        channel_filter=channel_filter,
        chip=args.chip,
        psg_mod_cents=args.psg_mod_cents,
    )

    print(f"File   : {path}")
    print(f"Chip   : {args.chip}")
    if args.chip in ('fm', 'all'):
        print(f"FM clk : {args.clock} Hz")
    if args.chip in ('psg', 'all'):
        print(f"PSG clk: {args.psg_clock} Hz")
    if channel_filter:
        print(f"Filter : {', '.join(sorted(channel_filter))}")
    print(f"Events : {len(rows)} key-on events found")

    if args.volumes:
        print_volume_suggestions(fm_amp, psg_amp)
        return

    print()
    print(f"{'time_ms':>10}  {'chan':<5}  {'data':>6}  {'blk':>3}  {'freq_hz':>10}  {'note':<8}  smps")
    print("-" * 68)

    limit = args.max_rows if args.max_rows > 0 else len(rows)
    for row in rows[:limit]:
        time_ms, ch, data_val, block, freq, note, smps = row
        blk_str  = str(block) if ch.startswith("FM") else "--"
        freq_str = f"{freq:>10.2f}" if freq > 0 else f"{'--':>10}"
        print(f"{time_ms:>10.1f}  {ch:<5}  {data_val:>6}  {blk_str:>3}  {freq_str}  {note:<8}  {smps}")

    if len(rows) > limit:
        print(f"  ... {len(rows) - limit} more rows (use --max-rows to show more)")

    # Per-channel unique note summary (smps convention), sorted by frequency
    seen_keys: dict[str, dict[tuple, float]] = {}   # chan -> {(note,smps): freq}
    for row in rows:
        _, ch, _, _, freq, note, smps = row
        seen_keys.setdefault(ch, {})
        key = (note, smps)
        if key not in seen_keys[ch]:
            seen_keys[ch][key] = freq

    if seen_keys:
        counts: dict[str, int] = {}
        for row in rows:
            counts[row[1]] = counts.get(row[1], 0) + 1
        print()
        print("Unique notes per channel  (standard -> SMPS label):")
        print("-" * 68)
        for ch_name in sorted(seen_keys.keys()):
            pairs = sorted(seen_keys[ch_name].items(), key=lambda kv: kv[1] if kv[1] > 0 else float('inf'))
            if all(smps == "—" for (_, smps), _ in pairs):
                # Noise / DAC channel — just list descriptors
                parts = [note for (note, _), _ in pairs]
            else:
                parts = [f"{note}->{smps}" for (note, smps), _ in pairs]
            print(f"  {ch_name:<6} ({counts[ch_name]:3d} key-ons): {',  '.join(parts)}")


if __name__ == "__main__":
    main()
