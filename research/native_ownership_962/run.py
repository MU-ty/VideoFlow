"""Run the factorial exposure/cadence ablation."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image

from compare import find_gap, longest_run, ownership
from stress import FULL, SHOTS, control_tracks, summarize, to_gray, transformed

ROOT = Path(__file__).resolve().parent
EXTRA = ("dim15_stride2", "dim30_stride4", "s2_dim30", "gamma18")
POLICIES = {
    "original": (False, False),
    "exposure": (True, False),
    "time": (False, True),
    "combined": (True, True),
}


def observed(images, shot, mode):
    if mode in FULL:
        gray, indices = transformed(images, mode)
        return gray, indices, int(mode[-1]) if mode.startswith("stride") else 1
    if mode == "s2_dim30":
        gray, indices = transformed(images, "dim30" if shot == "S2" else "original")
        return gray, indices, 1
    if mode == "gamma18":
        return [(to_gray(image) / 255) ** 1.8 * 255 for image in images], list(range(len(images))), 1
    gain, stride_name = mode.split("_")
    stride = int(stride_name[-1])
    gray, _ = transformed(images, gain)
    return gray[::stride], list(range(0, len(images), stride)), stride


def make_observations(gray, tracks, indices, gain):
    frames = []
    for index, frame, points in zip(indices, gray, zip(*tracks), strict=False):
        calibrated = np.clip(frame * gain, 0, 255)
        label = ownership(calibrated, *points)
        separated = label == "distinct" and find_gap(calibrated, *points)[0] >= 4
        frames.append((index + 1, label, separated))
    return frames


def classify(rows, cycles, expected_frames, stride, scale_time):
    interval = stride / 4
    min_votes = math.ceil(2.75 / interval) + 1 if scale_time else 12
    min_run = math.ceil(0.5 / interval) + 1 if scale_time else 3
    valid = len(rows) == expected_frames and all(v is not None and v <= 8 for v in cycles)
    counts = Counter(label for _, label, _ in rows)
    direction = counts["same"] + counts["distinct"]
    span = longest_run(separated for _, _, separated in rows)
    label = "unknown"
    if valid and span >= min_run:
        label = "distinct"
    elif valid and direction >= min_votes and counts["same"] >= 0.75 * direction:
        label = "same"
    return {"decision": label, "min_directional": min_votes, "min_separation_run": min_run,
            "track_cycles": cycles, "valid_tracks": valid, "directional_counts": dict(counts),
            "evidence_frames": [i for i, _, support in rows if support], "longest": span}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    clicks = json.loads((ROOT / "clicks.json").read_text())
    seeds = {shot: taps["first"] for shot, taps in clicks.items()}
    seeds.update(json.loads((ROOT / "control_clicks.json").read_text()))
    images = {}
    for shot in SHOTS:
        images[shot] = []
        for path in sorted((args.frames / shot).glob("*.png")):
            with Image.open(path) as image:
                images[shot].append(image.convert("RGB"))
    reference_p85 = float(np.percentile(to_gray(images["S1"][0]), 85))
    reference = json.loads((ROOT / "reference-950.json").read_text())["full_pipeline"]
    results = {}
    for mode in (*FULL, *EXTRA):
        observed_clips = {shot: observed(images[shot], shot, mode) for shot in SHOTS}
        anchor_p85 = float(np.percentile(observed_clips["S1"][0][0], 85))
        gain = float(np.clip(reference_p85 / max(anchor_p85, 1), 0.5, 2))
        policy_shots = {name: {} for name in POLICIES}
        for shot, (gray, indices, stride) in observed_clips.items():
            tracks, cycles = control_tracks(gray, seeds[shot])
            rows_by_exposure = {flag: make_observations(gray, tracks, indices, gain if flag else 1)
                                for flag in (False, True)}
            for name, (normalize, scale_time) in POLICIES.items():
                policy_shots[name][shot] = classify(rows_by_exposure[normalize], cycles, len(gray), stride, scale_time)
        outcomes = {name: summarize(shots) for name, shots in policy_shots.items()}
        if mode in FULL:
            assert outcomes["original"]["replay"] == reference[mode]["replay"]
            for shot in SHOTS:
                assert policy_shots["original"][shot]["decision"] == reference[mode]["shots"][shot]["decision"]
        results[mode] = {"anchor_p85": anchor_p85, "gain": gain, "outcomes": outcomes}
        print(mode, round(gain,3), [(name, data["replay"]["exact"], data["false_distinct_controls"])
                                    for name, data in outcomes.items()], flush=True)
    args.output.write_text(json.dumps({"reference_s1_p85": reference_p85, "conditions": results}, indent=2) + "\n")


if __name__ == "__main__":
    main()
