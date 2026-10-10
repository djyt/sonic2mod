"""Warnings on how a note resolved: clamped to the MOD's three octaves, or outside every range of a
mapped voice."""

from ...config import ConversionConfig
from ...diagnostics import Diagnostics, WarningKind
from ...mod import ModNote
from ...plan import DriverState, ResolvedNote
from ...smps import semitone_to_note_name as _semitone_to_name

_HIGHEST_NOTE = ModNote.B3.value       # the last of the MOD's 36 notes


def warn_resolution(diag: Diagnostics, config: ConversionConfig, source: str, st: DriverState,
                    res: ResolvedNote, note) -> None:
    """Warn where a note was clamped to the MOD's three octaves, or fell outside every range
    of a mapped voice (a config gap rather than an intended fallback)."""
    src_name = _semitone_to_name(res.source)

    # A map gap: the voice has ranges, none holds the note
    if res.path == "transpose" and st.fm_ranges(source) and st.voice is not None:
        ranges = st.fm_ranges(source)
        diag.warn(WarningKind.MAP_GAP, channel=source, voice_idx=st.voice, extra_ctx=st.psg_label,
                  note_name=src_name, semitone=res.source, range_lo=_semitone_to_name(ranges[0].low),
                  range_hi=_semitone_to_name(ranges[-1].high))
    if not res.clamped:
        return

    # Clamped: the source note at which it runs off the MOD's range, as its path placed it
    high = res.raw_index > _HIGHEST_NOTE
    entry = res.entry
    kind = WarningKind.CLAMP_HIGH if high else WarningKind.CLAMP_LOW
    w = {'channel': source, 'src_name': src_name, 'note_value': note.note_value, 'transpose': 0}
    if res.path == "fm_root":
        assert entry is not None
        w.update(voice_idx=st.voice,
                 boundary=_semitone_to_name(entry.high if high else entry.low))
    elif res.path == "psg_root":
        assert entry is not None
        edge = entry.low + ((_HIGHEST_NOTE if high else 0) - entry.root.value)
        w.update(voice_idx=None, extra_ctx=st.psg_label, boundary=_semitone_to_name(edge))
    else:
        tr = res.total_transpose
        w.update(voice_idx=st.voice, extra_ctx=st.psg_label, transpose=tr,
                 boundary=_semitone_to_name((_HIGHEST_NOTE if high else 0) - tr))
        if st.is_psg and not st.psg_label and config.psg_voice_map:
            w['psg_available_labels'] = list(config.psg_voice_map.keys())
    diag.warn(kind, **w)
