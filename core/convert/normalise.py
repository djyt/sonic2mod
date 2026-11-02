"""normalise_volumes: the reference build's volumes raised by one gain until its loudest note plays
at 64, so the MOD is as loud as the format allows and its balance unchanged.

    sample_list volumes    4   12   44   52 (a drum)       the loudest note asks for 52
    gain                   64 / 52 = +1.8 dB
    written                5   15   54   64                every Cxx raised alike

The loudest note is what the channel writers ask for (EmissionStats.peak_volume), so a first
write, thrown away, measures it; the samples' volumes are raised before the one kept.
"""

from ..mod import MOD_MAX_VOLUME, ModFile, clamp_mod_volume
from ..plan import SampleTable


def normalise_gain(peak: float) -> float:
    """The gain lifting `peak`, the loudest volume a note asked for, to 64; 1 where it is there
    already, or past it (a note clamped)."""
    if peak <= 0 or peak >= MOD_MAX_VOLUME:
        return 1.0
    return MOD_MAX_VOLUME / peak


def raise_volumes(mod: ModFile, samples: SampleTable, gain: float) -> None:
    """Every sample's volume times `gain`: the table's rows (what the writers scale Cxx from) and
    the MOD's sample headers alike."""
    for row in samples:
        row.volume = clamp_mod_volume(row.volume * gain)
    for sample in mod.samples:
        if sample.length:
            sample.set_volume(clamp_mod_volume(sample.volume * gain))
