"""Time Trax's glide step ($1739), a SlideLaw: the word moves `step` fnum units toward the target,
never past it.  Past fnum $4D2 up, or under $245 down, it changes block, the fnum taken to the
next block's $269 / $48C plus the overshoot's high byte (what the driver keeps of it):

    up    block 3, fnum $4C8 + 40 = $4F0   ->   block 4, fnum $269 + ($4F0 - $4D2) >> 8 = $269
    down  block 4, fnum $250 - 40 = $228   ->   block 3, fnum $48C + ($228 - $245) >> 8 = $48B
"""

from __future__ import annotations

from dataclasses import dataclass

from core.chips import freq_word, split_freq_word

_BLOCKS = 8
_UP_AT, _UP_TO = 0x4D2, 0x269            # fnum: at or past it the block rises, the fnum to this
_DOWN_UNDER, _DOWN_TO = 0x245, 0x48C     # under it the block drops, the fnum to this
_BYTE_BITS = 8


@dataclass(frozen=True)
class TimeTraxGlide:
    def toward(self, word: int, target: int, step: int) -> int:
        """`word` moved `step` fnum units toward `target`, never past it (SlideLaw.toward)."""
        if target >= word:
            return min(_up(word + step), target)
        return max(_down(word - step), target)


def _up(word: int) -> int:
    fnum, block = split_freq_word(word)
    if fnum < _UP_AT:
        return word
    return freq_word(_UP_TO + ((fnum - _UP_AT) >> _BYTE_BITS), (block + 1) % _BLOCKS)


def _down(word: int) -> int:
    fnum, block = split_freq_word(word)
    if fnum >= _DOWN_UNDER:
        return word
    return freq_word(_DOWN_TO + ((fnum - _DOWN_UNDER) >> _BYTE_BITS), (block - 1) % _BLOCKS)
