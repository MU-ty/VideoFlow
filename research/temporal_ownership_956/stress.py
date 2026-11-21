"""Stress the frozen Round 949 gate and replay it on causal frame prefixes."""
import argparse
import io
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from compare import control_tracks, find_gap, longest_run, ownership, replay, vote

ROOT = Path(__file__).resolve().parent
SHOTS = ("S1", "S2", "S3", "S4", "S6")
GAINS = {"dim15": 0.85, "dim30": 0.70, "bright15": 1.15, "bright30": 1.30}
FULL = ("original", *GAINS, "jpeg35", "blur1", "stride2", "stride4")
PREFIXES = (8, 12, 14, 15, 16, 18, 21)


def to_gray(image):
    return np.asarray(image.convert("L"), dtype=float)


def transformed(images, name):
    if name.startswith("stride"):
        stride = int(name[-1])
        indices = range(0, len(images), stride)
        return [to_gray(images[i]) for i in indices], list(indices)
    output = []
    for image in images:
        if name == "jpeg35":
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=35)
            with Image.open(buffer) as decoded:
                output.append(to_gray(decoded))
        elif name == "blur1":
            output.append(to_gray(image.filter(ImageFilter.GaussianBlur(1))))
        else:
            output.append(np.clip(to_gray(image) * GAINS.get(name, 1), 0, 255))
    return output, list(range(len(images)))


def evaluate(gray, tracks, cycles, indices):
    if any(len(v) != len(gray) for v in tracks):
        return {"decision": "unknown", "valid_tracks": False, "cycles": cycles, "gap_frames": [],
                "longest": 0, "sampled_frames": len(gray)}
    valid = all(val is not None and val <= 8 for val in cycles)
    labels = [ownership(frame, *points) for frame, points in zip(gray, zip(*tracks), strict=True)]
    separations = []
    for label, frame, points in zip(labels, gray, zip(*tracks), strict=True):
        separations.append(label == "distinct" and find_gap(frame, *points)[0] >= 4)
    span = longest_run(separations)
    base = vote(labels) if valid else "unknown"
    decision = "distinct" if valid and span >= 3 else ("same" if base == "same" else "unknown")
    return {"decision": decision, "valid_tracks": valid, "cycles": cycles,
            "gap_frames": [i+1 for i, accepted in zip(indices, separations, strict=True) if accepted],
            "longest": span, "sampled_frames": len(gray)}


def summarize(per_shot):
    plan = replay({shot: per_shot[shot]["decision"] for shot in ("S2", "S3")}, per_shot["S1"]["decision"])
    return {"shots": per_shot, "replay": plan,
            "false_distinct_controls": [s for s in ("S1", "S3", "S4", "S6") if per_shot[s]["decision"] == "distinct"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    anchor_clicks = json.loads((ROOT / "clicks.json").read_text())
    seeds = {s: record["first"] for s, record in anchor_clicks.items()}
    seeds.update(json.loads((ROOT / "control_clicks.json").read_text()))
    reference = json.loads((ROOT / "reference-949.json").read_text())
    data = {shot: [Image.open(path).convert("RGB") for path in sorted((args.frames/shot).glob("*.png"))]
            for shot in SHOTS}
    full, isolated, prefixes = {}, {}, {}
    for mode in FULL:
        details, fixed = {}, {}
        for shot, images in data.items():
            gray, indices = transformed(images, mode)
            details[shot] = evaluate(gray, *control_tracks(gray, seeds[shot]), indices)
            if mode not in ("stride2", "stride4"):
                record = reference["shots"][shot]
                tracks = [[frame[name] for frame in record["frames"]] for name in ("dial", "marker")]
                fixed[shot] = evaluate(gray, tracks, record["cycle_error_px"], indices)
        full[mode] = summarize(details)
        if fixed:
            isolated[mode] = summarize(fixed)
    assert full["original"]["replay"] == reference["replay"]["gated"]
    for shot in SHOTS:
        assert full["original"]["shots"][shot]["decision"] == reference["shots"][shot]["gated"]
    for count in PREFIXES:
        details = {}
        for shot, images in data.items():
            gray = [to_gray(image) for image in images[:count]]
            details[shot] = evaluate(gray, *control_tracks(gray, seeds[shot]), list(range(len(gray))))
        prefixes[str(count)] = summarize(details)
    result = {"full_pipeline": full, "fixed_tracking_diagnostic": isolated, "causal_prefix": prefixes}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    for block, group in result.items():
        print(block)
        for condition, detail in group.items():
            print(condition, [d["decision"] for d in detail["shots"].values()],
                  detail["replay"]["exact"], detail["shots"]["S2"]["gap_frames"])


if __name__ == "__main__":
    main()
