"""The conversion: SmpsToModConverter and the passes it runs.

    smps2mod.py        SmpsToModConverter: song -> ModFile
    level_plan.py      baked levels, FM render levels
    sustain_plan.py    how long each sample holds
    vibrato.py         smpsModSet -> 4xy
    channel_writer/    one channel into MOD cells
    layout.py          C00 / Fxx / Bxx / Dxx in the cells left free
    survey.py          the song prepared as the merged build prepares it (merge_survey, fold_csv)
"""

from .smps2mod import SmpsToModConverter
from .survey import SurveyContext, survey_context
from .vibrato import modulation_offset, modulation_slides, vibrato_depth

__all__ = [
    "SmpsToModConverter", "SurveyContext", "modulation_offset", "modulation_slides", "survey_context", "vibrato_depth"
]
