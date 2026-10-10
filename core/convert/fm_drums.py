"""How long each FM drum (core.smps.percussion) is heard: what its render may last.

A drum plays until the drum track's next hit (a rest does not stop it: the DAC channel plays a
sample out, and the driver lets FM3 ring), so a sample need hold no more than its longest such
gap, in the MOD's own seconds.  A program that never stops is rendered for exactly that.  The
track's last hit rings across its loop, to the first hit after the loop point:

    hit ... last hit |jump      loop point ... first hit
             |-------|              |----------|
             to the end      +      from the loop point
"""

from __future__ import annotations

from ..config import ConversionConfig
from ..plan import Timeline, fm_drum_catalogue
from ..smps import ChannelType, SmpsChannel, SmpsSong


def drum_rings(song: SmpsSong, config: ConversionConfig, timeline: Timeline) -> dict[int, float]:
    """{FM drum slot: the longest any of its hits sounds, in seconds}."""
    slots = {d.name: d.inst for d in fm_drum_catalogue(song, config).values()}
    rings: dict[int, float] = {}
    for channel in song.channels:
        if channel.header.channel_type != ChannelType.DAC:
            continue
        hits = [(ev.tick_position, ev.note.dac_name) for ev in channel.events if ev.note is not None and not ev.note.is_rest]
        if not hits:
            continue
        ticks = [tick for tick, _ in hits]
        ends = [*ticks[1:], song.end_tick()]
        secs = [timeline.span_secs(tick, end) for tick, end in zip(ticks, ends, strict=True)]
        secs[-1] += _after_loop(channel, ticks, song.end_tick(), timeline)

        for (_, name), ring in zip(hits, secs, strict=True):
            slot = slots.get(name)
            if slot is not None:
                rings[slot] = max(rings.get(slot, 0.0), ring)
    return rings


def _after_loop(channel: SmpsChannel, hits: list[int], end: int, timeline: Timeline) -> float:
    """How long the last hit rings on past the jump: from the loop point to the first hit after
    it (to the end again when the loop holds none); 0 for a track that stops."""
    if not channel.has_jump or channel.loop_tick is None:
        return 0.0
    after = next((tick for tick in hits if tick >= channel.loop_tick), end)
    return timeline.span_secs(channel.loop_tick, after)
