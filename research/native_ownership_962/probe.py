"""Apply a seeded point tracker and dark-enclosure ownership test."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi

from point_track import track


def disk(radius):
    coords = np.arange(-radius, radius+1)
    return coords[:, None]**2 + coords[None, :]**2 <= radius**2


def nearest(labels, x, y):
    radius = 8
    y0, x0 = max(0, y-radius), max(0, x-radius)
    ys, xs = np.nonzero(labels[y0:y+radius+1, x0:x+radius+1])
    yy, xx = ys+y0, xs+x0
    distances = (xx-x)**2 + (yy-y)**2
    if not len(distances) or distances.min() > radius**2:
        return None
    at_min = distances == distances.min()
    winners = set(labels[yy[at_min], xx[at_min]])
    return int(winners.pop()) if len(winners) == 1 else None


def ownership(gray, dial, marker):
    x0, y0 = np.minimum(dial, marker) - 12
    x1, y1 = np.maximum(dial, marker) + 13
    x0, y0 = max(0, x0), max(0, y0)
    dark = gray[y0:y1, x0:x1] <= 135
    bodies = ndi.binary_opening(dark, structure=disk(3))
    components, _ = ndi.label(bodies, structure=np.ones((3, 3)))
    a, b = [nearest(components, x-x0, y-y0) for x, y in (dial, marker)]
    if a is None or b is None:
        return "unknown"
    return "same" if a == b else "distinct"


def vote(values):
    counts = Counter(values)
    directional = counts["same"] + counts["distinct"]
    if directional < 12:
        return "unknown"
    for label in ("same", "distinct"):
        if counts[label] >= 0.75 * directional:
            return label
    return "unknown"


def replay(decisions, anchor):
    keep = ["S1"] + [s for s, label in decisions.items() if not (anchor == "same" and label == "distinct")]
    if "S3" in keep:
        keep.extend(("S4", "S6"))
    required = {"S1", "S3", "S4", "S6"}
    selected = set(keep)
    return {"selected": sorted(selected), "missing": sorted(required - selected),
            "excess": sorted(selected - required), "exact": selected == required}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=Path, required=True)
    parser.add_argument("--clicks", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    clicks = json.loads(args.clicks.read_text())
    results = {}
    preview = Image.new("RGB", (960, 600), "white")
    preview_draw = ImageDraw.Draw(preview)
    for row, (shot, taps) in enumerate(clicks.items()):
        paths = sorted((args.frames / shot).glob("*.png"))
        images = [Image.open(path).convert("RGB") for path in paths]
        gray = [np.asarray(im.convert("L"), dtype=float) for im in images]
        tracks = [track(gray, tuple(tap)) for tap in taps["first"]]
        cycle_errors = []
        for tap, positions in zip(taps["first"], tracks, strict=True):
            backwards = track(gray[::-1], positions[-1]) if len(positions) == len(gray) else []
            cycle_errors.append(round(float(np.linalg.norm(np.subtract(tap, backwards[-1]))), 3)
                                if len(backwards) == len(gray) else None)
        valid = all(error is not None and error <= 8 for error in cycle_errors)
        votes = [ownership(frame, *points) for frame, points in zip(gray, zip(*tracks))]
        counts = Counter(votes)
        check_error = [round(float(np.linalg.norm(np.subtract(points[19], expected))), 3)
                       if len(points) >= 20 else None
                       for points, expected in zip(tracks, taps["frame20_check"], strict=True)]
        results[shot] = {
            "sampled_frames": len(gray), "valid_tracks": valid, "lengths": [len(x) for x in tracks],
            "frame_sha256": [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths],
            "cycle_error_px": cycle_errors, "frame20_manual_error_px": check_error, "tracks": tracks,
            "frame_votes": votes, "counts": dict(counts), "first": votes[0],
            "temporal": vote(votes) if valid else "unknown",
        }
        for col, t in enumerate((0, 9, 19)):
            frame = images[t].copy()
            draw = ImageDraw.Draw(frame)
            for positions, color in zip(tracks, ("#ff3b31", "#00e4ff"), strict=True):
                if len(positions) > t:
                    x, y = positions[t]
                    draw.ellipse((x-5, y-5, x+5, y+5), outline=color, width=2)
            preview.paste(frame, (col*320, row*200+22))
            decision = votes[t] if len(votes) > t else "untracked"
            preview_draw.text((col*320+8,row*200+4),f"{shot} frame {t+1} / {decision}",fill="black")
    planners = {"marker_only": replay({"S2": "same", "S3": "same"}, "same")}
    for method in ("first", "temporal"):
        planners[method] = replay({shot: results[shot][method] for shot in ("S2", "S3")},
                                  results["S1"][method])
    result = {"shots": results, "offline_replay": planners}
    (args.output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    preview.save(args.output / "tracked_frames.jpg", quality=93)
    for shot, row in results.items():
        print(shot, row["counts"], row["cycle_error_px"], row["frame20_manual_error_px"],
              row["first"], row["temporal"])
    print(planners)


if __name__ == "__main__":
    main()
