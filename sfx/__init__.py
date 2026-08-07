"""Offline Sonic 1 SMPS SFX driver — renders SFX assembly to WAV.

Unlike the MOD conversion path (which flattens events onto a tracker row grid),
this package runs a tick-accurate software reimplementation of the Sonic 1 sound
driver against the cycle-accurate YM2612 and SN76489 emulators, producing a
continuous audio timeline.

Public entry point::

    from sfx.batch import render_all
"""
