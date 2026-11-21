"""Compare equal-weight ownership voting with explicit gap evidence."""
import argparse
from itertools import groupby
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import map_coordinates
from PIL import Image, ImageDraw

from point_track import track
from probe import ownership, replay, vote


def longest_run(flags):
    return max((sum(1 for _ in run) for bit, run in groupby(flags) if bit), default=0)


def find_gap(gray, dial, marker):
    start, stop = np.asarray(dial, dtype=float), np.asarray(marker, dtype=float)
    direction = stop - start
    length = np.linalg.norm(direction)
    if length < 19:
        return 0, None
    axis = direction / length
    normal = np.array([-axis[1], axis[0]])
    centers = start + np.arange(8, length - 8)[:, None] * axis
    offsets = np.arange(-6, 7)
    points = centers[:, None, :] + offsets[None, :, None] * normal
    samples = map_coordinates(gray, (points[..., 1], points[..., 0]), order=1, cval=0)
    bright = (samples > 155).mean(axis=1) >= 0.75
    run, begin = 0, -1
    i = 0
    for bit, group in groupby(bright):
        size = sum(1 for _ in group)
        if bit and size > run:
            run, begin = size, i
        i += size
    if begin < 0:
        return 0, None
    middle = centers[begin+(run-1)//2]
    return run, [(middle + offset * normal).round().astype(int).tolist() for offset in (-6, 6)]


def control_tracks(gray, taps):
    tracks = [track(gray, tuple(p)) for p in taps]
    cycles = []
    for tap, trajectory in zip(taps, tracks, strict=True):
        back = track(gray[::-1], trajectory[-1]) if len(trajectory) == len(gray) else []
        cycles.append(round(float(np.linalg.norm(np.subtract(tap, back[-1]))), 3)
                      if len(back) == len(gray) else None)
    return tracks, cycles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(exist_ok=False, parents=True)
    root = Path(__file__).resolve().parent
    reference = json.loads((root / "reference-948.json").read_text())
    controls = json.loads((root / "control_clicks.json").read_text())
    shots = ("S1", "S2", "S3", "S4", "S6")
    summary = {}
    sheet = Image.new("RGB", (960, 5*200), "white")
    titles = ImageDraw.Draw(sheet)
    for row, shot in enumerate(shots):
        images = [Image.open(p).convert("RGB") for p in sorted((root/"frames"/shot).glob("*.png"))]
        gray = [np.asarray(i.convert("L"), dtype=float) for i in images]
        old = reference["shots"].get(shot)
        tracks, cycles = (old["tracks"], old["cycle_error_px"]) if old else control_tracks(gray, controls[shot])
        valid = all(error is not None and error <= 8 for error in cycles)
        details = []
        for i, (frame, (dial, marker)) in enumerate(zip(gray, zip(*tracks)), 1):
            original = ownership(frame, dial, marker)
            run, witness = find_gap(frame, dial, marker)
            details.append(dict(frame=i, dial=dial, marker=marker, original=original, width_px=run,
                                witness=witness, separating=original == "distinct" and run >= 4))
        original_votes = [f["original"] for f in details]
        if old is not None:
            assert original_votes == old["frame_votes"]
        previous = vote(original_votes) if valid else "unknown"
        run = longest_run(f["separating"] for f in details)
        decision = "distinct" if valid and run >= 3 else ("same" if previous == "same" else "unknown")
        summary[shot] = dict(valid=valid, cycle_error_px=cycles, baseline=previous, gated=decision,
                             separation_frames=[f["frame"] for f in details if f["separating"]],
                             longest_separation_run=run, frames=details)
        for col, t in enumerate((0, 12, 19)):
            im = images[t].copy()
            if t < len(details):
                draw = ImageDraw.Draw(im)
                d = details[t]
                for point, color in ((d["dial"], "red"), (d["marker"], "cyan")):
                    x,y = point
                    draw.ellipse((x-4,y-4,x+4,y+4), outline=color, width=2)
                if d["separating"]:
                    draw.line([tuple(p) for p in d["witness"]], fill="lime", width=3)
                label = f"{shot} frame {t+1} - {d['original']}, gap {d['width_px']} px"
                titles.text((col*320+8, row*200+4),label,fill="black")
            sheet.paste(im, (col*320,row*200+22))
    selected = {a: replay({s: summary[s][a] for s in ("S2","S3")},summary["S1"][a])
                for a in ("baseline","gated")}
    assert selected["baseline"] == reference["offline_replay"]["temporal"]
    (args.output / "results.json").write_text(json.dumps({"shots":summary,"replay":selected},indent=2)+"\n")
    sheet.save(args.output/"separation_review.jpg",quality=94)
    for s, d in summary.items():
        print(s, {k:v for k,v in d.items() if k != "frames"})
    print(selected)


if __name__ == "__main__":
    main()
