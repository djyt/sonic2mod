"""Each chip channel of a VGM/VGZ rendered alone (VGMPlay with mute masks), and each MOD channel
(ffmpeg + libopenmpt on channel-isolated copies): the audio vgm_compare measures.

VGMPlay 0.51.x (the libvgm line, Core = NUKE) is found by find_vgmplay: --vgmplay DIR, then
VGMPLAY_DIR, then reference/vgz/vgmplay/ (untracked).  Every render is 44 100 Hz stereo WAV.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..mod import isolate_channel

SR = 44100                  # every render's rate

# VGM channel name -> (YM2612 mute mask, SN76496 mute mask) that leaves ONLY that channel audible.
_YM_ALL, _SN_ALL = 0x7F, 0xF
VGM_CHANNELS = {
    "FM1": (_YM_ALL & ~0x01, _SN_ALL), "FM2": (_YM_ALL & ~0x02, _SN_ALL),
    "FM3": (_YM_ALL & ~0x04, _SN_ALL), "FM4": (_YM_ALL & ~0x08, _SN_ALL),
    "FM5": (_YM_ALL & ~0x10, _SN_ALL), "FM6": (_YM_ALL & ~0x20, _SN_ALL),
    "DAC": (_YM_ALL & ~0x40, _SN_ALL),
    "PSG1": (_YM_ALL, _SN_ALL & ~0x1), "PSG2": (_YM_ALL, _SN_ALL & ~0x2),
    "PSG3": (_YM_ALL, _SN_ALL & ~0x4), "NOISE": (_YM_ALL, _SN_ALL & ~0x8),
}

_VGMPLAY_EXES = ("VGMPlay64.exe", "VGMPlay.exe", "vgmplay")
_VGMPLAY_DEFAULT = Path(__file__).resolve().parents[2] / "reference" / "vgz" / "vgmplay"


def find_vgmplay(arg: str | None) -> Path:
    """Resolve the VGMPlay directory: --vgmplay, then VGMPLAY_DIR, then reference/vgz/vgmplay/."""
    cand = arg or os.environ.get("VGMPLAY_DIR")
    d = Path(cand) if cand else _VGMPLAY_DEFAULT
    if any((d / exe).exists() for exe in _VGMPLAY_EXES):
        return d
    if cand:
        raise SystemExit(f"ERROR: no VGMPlay executable found in {d}")
    raise SystemExit(
        f"ERROR: VGMPlay not found in {_VGMPLAY_DEFAULT}\n"
        "       Unzip a VGMPlay 0.51.x build there (the directory is untracked), or pass\n"
        "       --vgmplay DIR / set VGMPLAY_DIR.  See 'VGM comparison setup' in docs/pipeline.md.")


def _patch_ini(src: str, ym_mask: int, sn_mask: int, core: str) -> str:
    out, section = [], None
    for line in src.splitlines():
        m = re.match(r"^\[(.+)\]", line)
        if m:
            section = m.group(1)
            out.append(line)
            if section == "YM2612":
                out += [f"MuteMask = 0x{ym_mask:02X}", f"Core = {core}"]
            elif section == "SN76496":
                out.append(f"MuteMask = 0x{sn_mask:X}")
            continue
        if section == "General" and re.match(r"^\s*(LogSound|MaxLoops|SampleRate)\s*=", line):
            continue
        if section in ("YM2612", "SN76496") and re.match(r"^\s*(MuteMask|Core|MuteCh\d)\s*=", line):
            continue
        out.append(line)
    text = "\n".join(out) + "\n"
    # Force WAV logging, one pass, 44100 Hz.
    text = text.replace("[General]", "[General]\nLogSound = 1\nMaxLoops = 1\nSampleRate = 44100", 1)
    return text


def group_masks(sources: list[str]) -> tuple[int, int]:
    """The mute masks that leave every one of `sources` audible (a merged channel's sum)."""
    ym, sn = _YM_ALL, _SN_ALL
    for s in sources:
        y, n = VGM_CHANNELS[s]
        ym, sn = ym & y, sn & n
    return ym, sn


def render_vgm_channels(vgz: Path, names: list[str], vgmplay: Path, outdir: Path, core: str,
                        masks: dict[str, tuple[int, int]] | None = None) -> None:
    masks = masks or VGM_CHANNELS
    exe = next(e for e in _VGMPLAY_EXES if (vgmplay / e).exists())
    base_ini = (vgmplay / "VGMPlay.ini").read_text(encoding="utf-8", errors="replace")
    outdir.mkdir(parents=True, exist_ok=True)
    for name in ["FULL", *names]:
        ym, sn = (0x00, 0x0) if name == "FULL" else masks[name]
        work = outdir / f"_vgm_{name}"
        work.mkdir(exist_ok=True)
        for f in vgmplay.iterdir():
            if f.suffix.lower() in (".exe", ".dll"):
                shutil.copy(f, work / f.name)
        (work / "VGMPlay.ini").write_text(_patch_ini(base_ini, ym, sn, core), encoding="utf-8")
        shutil.copy(vgz, work / "ref.vgz")
        subprocess.run([str(work / exe), "ref.vgz"], cwd=work, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600, check=False)
        wav = work / "ref.wav"
        if not wav.exists():
            raise SystemExit(f"ERROR: VGMPlay produced no WAV for {name} (check LogSound support)")
        shutil.move(str(wav), str(outdir / f"vgm_{name}.wav"))
        shutil.rmtree(work, ignore_errors=True)
        print(f"  rendered VGM {name}")


def render_mod_channels(mod_path: Path, channels: dict[str, int], outdir: Path) -> None:
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ERROR: ffmpeg not found on PATH")
    probe = subprocess.run(["ffmpeg", "-hide_banner", "-h", "demuxer=libopenmpt"],
                           capture_output=True, text=True, check=False)
    if "libopenmpt" not in probe.stdout:
        raise SystemExit("ERROR: this ffmpeg build has no libopenmpt demuxer")
    data = mod_path.read_bytes()
    outdir.mkdir(parents=True, exist_ok=True)

    def render(item: tuple[str, int | None]) -> tuple[str, str | None]:
        name, ch = item
        iso = outdir / f"_mod_{name}.mod"
        iso.write_bytes(isolate_channel(data, ch))
        wav = outdir / f"mod_{name}.wav"
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "libopenmpt",
                            "-sample_rate", str(SR), "-i", str(iso), "-ar", str(SR), "-ac", "2", str(wav)],
                           capture_output=True, text=True, check=False)
        iso.unlink(missing_ok=True)
        return name, (r.stderr if r.returncode != 0 or not wav.exists() else None)

    # One ffmpeg per channel; they are independent, so they run at once (cores - 1 of them).
    items = [("FULL", None), *channels.items()]
    with ThreadPoolExecutor(max_workers=max(1, min(len(items), (os.cpu_count() or 2) - 1))) as pool:
        for name, err in pool.map(render, items):
            if err is not None:
                raise SystemExit(f"ERROR: ffmpeg failed for MOD channel {name}:\n{err}")
            print(f"  rendered MOD {name}")
