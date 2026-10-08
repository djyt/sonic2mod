"""How long each FM drum (core.smps.percussion) is heard: what its render may last.

A drum plays until the drum track's next hit (a rest does not stop it: the DAC channel plays a
sample out, and the driver lets FM3 ring), so a sample need hold no more than its longest such
gap, in the MOD's own seconds.  A program that never stops is rendered for exactly that.
"""

from __future__ import annotations

from ..config import ConversionConfig
from ..plan import Timeline, fm_drum_catalogue
from ..smps import SmpsSong


def drum_rings(song: SmpsSong, config: ConversionConfig, timeline: Timeline) -> dict[int, float]:
    """{FM drum slot: the longest any of its hits sounds, in seconds}."""
    slots = {d.name: d.inst for d in fm_drum_catalogue(song, config).values()}
    rings: dict[int, float] = {}
    for channel in song.channels:
        if channel.header.channel_type != "DAC":
            continue
        hits = [ev for ev in channel.events if ev.is_note and not ev.note.is_rest]
        ends = [ev.tick_position for ev in hits[1:]] + [song.end_tick()]
        for hit, end in zip(hits, ends, strict=True):
            slot = slots.get(hit.note.dac_name)
            if slot is None:
                continue
            rings[slot] = max(rings.get(slot, 0.0), timeline.span_secs(hit.tick_position, end))
    return rings
