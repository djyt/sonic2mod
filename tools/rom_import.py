#!/usr/bin/env python3
"""Songs and sound effects read from a Mega Drive ROM's SMPS bytecode (core.rom): list them,
compare each with its asm, or write each as SMPS2ASM assembly.

The comparison is two-fold: core.smps.parse_differences (every event, spelling included - the
ROM and the asm are the same bytes, so nothing may differ) and, for music, compare_songs on what
the driver plays.  Both sides read with the data fixes (core/rom/variants.py: known for this exact ROM
only, as the asm's FixMusicAndSFXDataBugs), or with --shipped neither: the game as it was sold.

Usage::

    python tools/rom_import.py input/roms/sonic_rev01.bin                       # the indexes
    python tools/rom_import.py input/roms/sonic_rev01.bin --compare reference/smps_drivers/sonic_1     # every sound vs its asm
    python tools/rom_import.py input/roms/sonic_rev01.bin --compare reference/smps_drivers/sonic_1 --only 81 8A
    python tools/rom_import.py input/roms/sonic_rev01.bin --asm output/rom_asm  # MusXX.asm / SndXX.asm
    python tools/rom_import.py input/roms/sonic_rev01.bin --dac output/rom_dac  # DAC samples: .raw + manifest.yaml
    python tools/rom_import.py "input/roms/Michael Jackson's Moonwalker (World) (Rev A).md"   # Type 1a, detected

The driver is detected (core/rom/detect.py); --driver states it.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent
sys.path.insert(0, str(ROOT))

import yaml

from core.rom import (
    VARIANTS,
    RomError,
    RomImage,
    SmpsVariant,
    SoundIndex,
    dac_samples,
    data_fixes,
    detect_variant,
    locate_sounds,
    read_rom_code,
)
from core.smps import (
    SmpsDriver,
    SmpsParser,
    SmpsSong,
    SongDiff,
    align_songs,
    compare_songs,
    parse_differences,
    played_song,
    write_asm,
)
from core.ui import modifier_text, song_diff_lines

_DEFAULT_DIFFS = 12


def _prefix(index: SoundIndex, sound_id: int) -> str:
    """The disassembly's file prefix: Mus81, SndA0."""
    return f"{'Snd' if index.is_sfx(sound_id) else 'Mus'}{sound_id:02X}"


def _ids(index: SoundIndex, only: list[str] | None) -> list[int]:
    ids = sorted(index.music) + sorted(index.sfx)
    return [i for i in ids if not only or f"{i:02X}" in {o.upper() for o in only}]


def _list(rom: RomImage, index: SoundIndex, ids: list[int], fixed: bool, variant: SmpsVariant) -> None:
    print(f"{rom.title}  {rom.serial}  sha1 {rom.sha1}  driver {variant.name}")
    for fix in data_fixes(rom):
        print(f"  data fix ${fix.address:05X}  {fix.what}{'' if fixed else '  (off: --shipped)'}")
    for sound_id in ids:
        try:
            song = read_rom_code(rom, sound_id, index, fixed, variant)
        except RomError as e:
            print(f"  ${sound_id:02X}  ${index.address(sound_id):05X}  not read: {e}")
            continue
        h = song.header
        tracks = ", ".join(f"{n} {t}" for t in ("DAC", "FM", "PSG")
                           if (n := sum(c.channel_type == t for c in h.channels)))
        tempo = f"divider ${h.tempo_divider:02X}" + ("" if h.is_sfx else f" {modifier_text(h.tempo_modifier)}")
        dropped = ", ".join(f"{what} x{n}" for what, n in song.dropped.items())
        print(f"  ${sound_id:02X}  ${index.address(sound_id):05X}  {'SFX  ' if h.is_sfx else 'music'}  "
              f"{tracks:<20} {tempo:<26} {len(song.voices)} voices" + (f"   dropped: {dropped}" if dropped else ""))


def _compare(rom: RomImage, index: SoundIndex, ids: list[int], asm_dir: Path, max_diffs: int, fixed: bool,
             variant: SmpsVariant) -> bool:
    """Each sound against its asm; True when every one reads and plays the same."""
    same = 0
    for sound_id in ids:
        prefix = _prefix(index, sound_id)
        asm = next(iter(sorted(asm_dir.rglob(f"{prefix}*.asm"))), None)
        if asm is None:
            print(f"  ${sound_id:02X}  no {prefix}*.asm in {asm_dir}")
            continue

        got = read_rom_code(rom, sound_id, index, fixed, variant).song()
        want = SmpsParser(fix_data_bugs=fixed).parse_file(str(asm))
        found = parse_differences(want, got)
        diff = None if index.is_sfx(sound_id) else _played_diff(want, got)
        if not found and (diff is None or diff.ok):
            same += 1
            print(f"  ${sound_id:02X}  {asm.name:<34} same")
            continue

        print(f"  ${sound_id:02X}  {asm.name}")
        for line in found[:max_diffs]:
            print(f"        {line}")
        for line in song_diff_lines(diff, max_diffs) if diff is not None and not diff.ok else []:
            print(f"    {line}")
    print(f"{same} of {len(ids)} read as their asm")
    return same == len(ids)


def _played_diff(want: SmpsSong, got: SmpsSong) -> SongDiff:
    """What the driver plays, compared (music only: played_song follows the music driver; an SFX
    may run past the FM table, which the SFX driver caps)."""
    expected, played = played_song(want), played_song(got)
    return compare_songs(expected, played, offset=align_songs(expected, played))


def _write(rom: RomImage, index: SoundIndex, ids: list[int], out_dir: Path, fixed: bool, variant: SmpsVariant) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for sound_id in ids:
        prefix = _prefix(index, sound_id)
        comment = f"{prefix}: ${index.address(sound_id):05X} in {rom.title} ({rom.serial}), by tools/rom_import.py"
        fixes = data_fixes(rom) if fixed else ()
        if fixes:
            comment += (f"\nFixMusicAndSFXDataBugs: the {len(fixes)} data fixes known for this ROM applied "
                        "(labels keep its addresses)")
        path = out_dir / f"{prefix}.asm"
        try:
            text = write_asm(read_rom_code(rom, sound_id, index, fixed, variant), prefix, comment)
        except ValueError as e:                     # RomError among them
            print(f"  ${sound_id:02X}  not written: {e}")
            continue
        path.write_text(text, encoding="utf-8")
        print(f"  {path}")


def _write_dac(rom: RomImage, out_dir: Path, variant: SmpsVariant) -> None:
    """Each DAC sample as signed 8-bit .raw (what samples/ holds), and a manifest of their rates;
    a pitched copy (Sonic 1's $88-$8B timpani, Moonwalker's $88-$97) shares its sample's file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    samples = dac_samples(rom, variant)
    files = {s.pcm: f"{s.name}.raw" for s in samples if not s.is_pitched_copy}
    for pcm, name in files.items():
        (out_dir / name).write_bytes(pcm)

    manifest = [{"sound": f"${s.sound:02X}", "name": s.name, "file": files[s.pcm], "bytes": len(s.pcm),
                 "pitch": s.pitch, "rate_hz": round(s.rate, 1)} | ({"plays": f"${s.of:02X}"} if s.of else {})
                for s in samples]
    (out_dir / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    for s in samples:
        print(f"  ${s.sound:02X}  {s.name:<13} {len(s.pcm):>5} bytes  pitch {s.pitch:>2}  {s.rate:>7.0f} Hz  {files[s.pcm]}")
    print(f"  {out_dir / 'manifest.yaml'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom", help="the ROM (.bin / .md / .gen)")
    ap.add_argument("--compare", metavar="DIR", help="compare each sound with DIR's MusXX* / SndXX* .asm (searched recursively)")
    ap.add_argument("--asm", metavar="DIR", help="write each sound as SMPS2ASM assembly into DIR")
    ap.add_argument("--dac", metavar="DIR", help="write the DAC samples (.raw, signed 8-bit) and manifest.yaml into DIR")
    ap.add_argument("--driver", choices=list(SmpsDriver), help="the driver, not detected")
    ap.add_argument("--shipped", action="store_true", help="no data fixes: the game as sold (the asm read alike)")
    ap.add_argument("--only", nargs="+", metavar="ID", help="these sound IDs (hex: 81 A0)")
    ap.add_argument("--diffs", type=int, default=_DEFAULT_DIFFS, help=f"differences listed per sound (default {_DEFAULT_DIFFS})")
    args = ap.parse_args()
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    try:
        rom = RomImage.load(args.rom)
        variant = VARIANTS[SmpsDriver(args.driver)] if args.driver else detect_variant(rom)
        index = locate_sounds(rom, variant)
    except (OSError, RomError) as e:
        sys.exit(f"error: {e}")
    ids = _ids(index, args.only)

    if args.asm:
        _write(rom, index, ids, Path(args.asm), not args.shipped, variant)
    if args.dac:
        _write_dac(rom, Path(args.dac), variant)
    if args.compare:
        sys.exit(0 if _compare(rom, index, ids, Path(args.compare), args.diffs, not args.shipped, variant) else 1)
    if not (args.asm or args.dac):
        _list(rom, index, ids, not args.shipped, variant)


if __name__ == "__main__":
    main()
