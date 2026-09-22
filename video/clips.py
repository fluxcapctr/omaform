"""Cut the rendered video into short looping clips for the README.

GitHub plays animated images inline, not video files, so each feature gets
its own animated WebP, cut on scene boundaries from src/timeline.json and
trimmed past the transitions so it loops cleanly.

    python3 clips.py      # after npm run render; writes ../docs/tour/*.webp
"""

import json
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "docs" / "tour"
VIDEO = HERE / "out" / "omaform.mp4"

TL = json.load(open(HERE / "src" / "timeline.json"))
BAR = TL["fps"] * 60 * 4 / TL["bpm"]
RANGE, _at = {}, 0.0
for scene in TL["scenes"]:
    RANGE[scene["name"]] = (round(_at * BAR), round((_at + scene["bars"]) * BAR))
    _at += scene["bars"]

CLIPS = {
    "intro": ["problem", "title"],
    "fill": ["w9"],
    "sure": ["list"],
    "vault": ["vault"],
    "signature": ["sigdialog", "sign"],
    "identities": ["identities"],
    "flat": ["flat"],
    "sections": ["i9"],
    "blackout": ["blackout"],
    "pages": ["pages"],
    "agent": ["agent", "ask"],
    "word": ["docx"],
}
TRIM = 8          # frames kept clear of each cut's wipe and blur


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, scenes in CLIPS.items():
        start = RANGE[scenes[0]][0] + (TRIM if RANGE[scenes[0]][0] else 0)
        end = RANGE[scenes[-1]][1] - TRIM
        target = OUT / f"{name}.webp"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y",
            "-ss", f"{start / TL['fps']:.3f}", "-t", f"{(end - start) / TL['fps']:.3f}",
            "-i", str(VIDEO), "-an",
            "-vf", "fps=15,scale=800:-2:flags=lanczos",
            "-c:v", "libwebp_anim", "-lossless", "0", "-quality", "60",
            "-compression_level", "6", "-loop", "0", str(target)], check=True)
        print(f"{target.name:18} {target.stat().st_size / 1e6:5.2f} MB  "
              f"{(end - start) / TL['fps']:.1f}s")


if __name__ == "__main__":
    main()
