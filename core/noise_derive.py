"""What the song says about its noise instruments, so no config has to: the envelope each plays
with (the header voice or the last smpsPSGvoice) and, for rate-3 noise, the tone-2 divider the
driver writes from PSG3's own notes.
"""

from .config import ConversionConfig
from .driver_state import enabled_channels, walk_channel
from .driver_tables import psg_tone2_divider
from .smps_song import SmpsSong
from .tables import semitone_to_note_name as _semitone_to_name


def derive_noise_envelopes(song: SmpsSong, config: ConversionConfig) -> dict[int, dict]:
    """The envelope each noise instrument plays with, read from the song.

    smpsPSGform only switches the channel to noise; the envelope is the driver's VoiceIndex
    — the header voice (fTone_04 for sixteen of the eighteen PSG3 tracks, fTone_09 in Marble
    Zone, fTone_08 in Robotnik) or the last smpsPSGvoice.  Walking the PSG channels with the
    DriverState, every noise note votes for the label in force; an instrument's envelope is
    the label most of its notes play with (ties to the first heard).  A psg_map entry's
    `envelopes:` variants are their own instruments and carry their label.  A stated
    `envelope:` on the entry overrides the vote.

    Returns {instrument: {'envelope', 'derived', 'counts': {label: notes}}}; `counts` lists
    every label the instrument played with, so the caller can warn where one sample stands
    in for several envelopes (Credits' PSG3, which has no free slot for variants).
    """
    counts: dict[int, dict[str | None, int]] = {}
    for chan_cfg, channel in enabled_channels(song, config, ("PSG",)):
        for _event, st, res in walk_channel(channel, config, chan_cfg):
            if res is not None and st.in_noise_mode and st.psg_entry is not None:
                per = counts.setdefault(st.instrument, {})
                per[st.envelope] = per.get(st.envelope, 0) + 1

    out: dict[int, dict] = {}
    for entry in config.psg_map.values():
        base_counts = counts.get(entry.mod_instrument, {})
        if entry.envelope is not None:
            envelope, derived = entry.envelope, False
        elif base_counts:
            envelope, derived = max(base_counts, key=lambda k: base_counts[k]), True
        else:
            envelope, derived = None, True
        out[entry.mod_instrument] = {'envelope': envelope, 'derived': derived, 'counts': base_counts}
        for label, inst in entry.envelopes.items():
            out[inst] = {'envelope': label, 'derived': True, 'counts': counts.get(inst, {})}
    return out


def derive_rate3_dividers(song: SmpsSong, config: ConversionConfig) -> dict[int, dict]:
    """Tone-2 divider the driver writes for each rate-3 (`noise_rate: 3`) noise instrument.

    In rate-3 mode the LFSR is clocked by tone channel 2, and the Sonic 1 driver keeps writing
    PSG3's own note there: divider = PSGFrequencies[note − $81 + transpose].  So the right
    divider is in the song data, not something a config has to state: the hi-hat's `nMaxPSG`
    is table entry 69 = divider 0, which the Sega VDP PSG clocks as 1 (near-white hiss), and
    Marble Zone's pitched noise is whatever its notes say.

    The divider is taken at the entry's `low` note when it has one (the sample plays at `root`
    for that note, and MOD playback speed moves it from there), otherwise from the note the
    instrument plays most.  Returns {instrument: {'n', 'note', 'transpose', 'used'}};
    `used` is False when the config states `tone2_n` or `synth_root`, which win.
    """

    seen: dict[int, dict] = {}        # instrument -> {'entry', 'notes': {(note_value, transpose): count}}
    for chan_cfg, channel in enabled_channels(song, config, ("PSG",)):
        for event, st, res in walk_channel(channel, config, chan_cfg):
            if res is None:
                continue
            e = res.entry
            if e is not None and e.type != "tone" and e.noise_rate == 3:
                # An envelope variant (psg_map entry's `envelopes:`) is its own instrument
                rec = seen.setdefault(res.instrument, {'entry': e, 'notes': {}})
                key = (event.note.note_value, st.transpose)
                rec['notes'][key] = rec['notes'].get(key, 0) + 1

    out: dict[int, dict] = {}
    for inst, rec in seen.items():
        e, notes = rec['entry'], rec['notes']
        at_low = {k: c for k, c in notes.items() if e.low is not None and k[0] - 0x81 == e.low}
        if at_low:
            note_value, transpose = max(at_low, key=lambda k: at_low[k])
        else:
            note_value, transpose = max(notes, key=lambda k: notes[k])
            if e.low is not None:                       # anchor never played: same transpose, the anchor note
                note_value = e.low + 0x81
        n = psg_tone2_divider(note_value, transpose)
        out[inst] = {'n': n, 'note': _semitone_to_name(note_value - 0x81), 'transpose': transpose,
                     'used': e.tone2_n is None and e.synth_root is None}
    return out
