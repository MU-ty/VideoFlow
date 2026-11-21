"""Prepare a blind-to-predictions screen and score frozen ownership arms."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw

from baselines import full_frame
from generate import protocol
from run import classify, control_tracks, make_observations, to_gray

SHOTS = ("S1", "S2", "S3")
CANDIDATES = SHOTS[1:]
ARMS = ("marker_match", "framewise_geometric", "temporal_geometric", "oracle")
CONDITIONS = ("same-decal", "cross-view", "partial-occlusion")


def write(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def verified_clips(root, plan):
    if json.loads((root / "protocol.json").read_text()) != plan:
        raise ValueError("source archive protocol differs from the frozen plan")
    jobs = json.loads((root / "report.json").read_text())["jobs"]
    if set(jobs) != set(plan["clips"]):
        raise ValueError("generation journal does not cover the frozen clip roster")
    complete = {}
    for key, job in jobs.items():
        spec = plan["clips"][key]
        expected = spec["reference_sha256"]
        archived = root / spec["reference"]
        if job.get("reference_sha256") != expected or hashlib.sha256(archived.read_bytes()).hexdigest() != expected:
            raise ValueError(f"{key}: archived reference does not match the frozen input")
        if job["status"] == "failed":
            continue
        media = root / f"{key}.mp4"
        if job["status"] != "complete" or hashlib.sha256(media.read_bytes()).hexdigest() != job["sha256"]:
            raise ValueError(f"{key}: source generation is incomplete or media changed")
        complete[key] = media
    return complete


def frames(media):
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(media), "-vf", "fps=4,scale=320:178",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-threads", "1", "pipe:1"],
        capture_output=True, check=True,
    )
    size = 320 * 178 * 3
    if len(proc.stdout) < size or len(proc.stdout) % size:
        raise ValueError(f"invalid sampled video: {media}")
    return [Image.frombytes("RGB", (320, 178), proc.stdout[i:i + size])
            for i in range(0, len(proc.stdout), size)]


def prepare(out, plan, clips):
    screen = {}
    for case in plan["cases"]:
        complete = all(f"{case}/{shot}" in clips for shot in SHOTS)
        screen[case] = {"eligible": None if complete else False,
                        "reason": "" if complete else "generation-failed",
                        "clicks": {}}
        if not complete:
            continue
        preview = Image.new("RGB", (1600, 600), "white")
        labels = ImageDraw.Draw(preview)
        for row, shot in enumerate(SHOTS):
            sequence = frames(clips[f"{case}/{shot}"])
            sequence[0].save(out / f"{case}-{shot}-first.png")
            for col, i in enumerate((0, len(sequence)//4, len(sequence)//2, 3*len(sequence)//4,
                                     len(sequence)-1)):
                labels.text((col*320+6, row*200+4), f"{shot}  {i/4:.2f}s", fill="black")
                preview.paste(sequence[i], (col*320, row*200+20))
        preview.save(out / f"{case}.jpg", quality=92)
    template = out / "screen.template.json"
    if not template.exists():
        write(template, screen)


def compare_clip(images, clicks):
    if len(clicks) != 2 or any(len(point) != 2 or any(type(v) is not int for v in point)
                               or not (0 <= point[0] < 320 and 0 <= point[1] < 178)
                               for point in clicks):
        raise ValueError("clicks must be two in-frame integer pairs, dial first and decal second")
    gray = [to_gray(image) for image in images]
    tracks, cycles = control_tracks(gray, clicks)
    rows = make_observations(gray, tracks, list(range(len(gray))), gain=1)
    detail = classify(rows, cycles, len(gray), stride=1, scale_time=True)
    return dict(detail, frame_rows=rows, tracks=tracks)


def score(truth, raw):
    final = {shot: raw[shot] if raw["S1"] == "same" else "unknown" for shot in CANDIDATES}
    target = {shot for shot in SHOTS if truth[shot] == "same"}
    selected = {"S1"} | {shot for shot in CANDIDATES if final[shot] != "distinct"}
    return {
        "raw": raw, "after_anchor": final, "selected": sorted(selected),
        "exact": selected == target, "coverage": target <= selected,
        "extra_planned": len(selected - target),
    }


def summarize(rows, arm):
    counts = Counter(dict.fromkeys((
        "exact", "coverage", "extra_planned", "positive", "negative", "false_accept",
        "false_reject", "abstain_positive", "abstain_negative", "effective_abstain_positive",
        "effective_abstain_negative", "wrongly_retained", "wrongly_dropped", "anchor_abstain",
    ), 0))
    for row in rows:
        value = row["arms"][arm]
        counts["exact"] += value["exact"]
        counts["coverage"] += value["coverage"]
        counts["extra_planned"] += value["extra_planned"]
        selected = set(value["selected"])
        for shot in CANDIDATES:
            negative = row["truth"][shot] == "distinct"
            kind = "negative" if negative else "positive"
            counts[kind] += 1
            counts["false_accept"] += negative and value["raw"][shot] == "same"
            counts["false_reject"] += not negative and value["raw"][shot] == "distinct"
            counts[f"abstain_{kind}"] += value["raw"][shot] == "unknown"
            counts[f"effective_abstain_{kind}"] += value["after_anchor"][shot] == "unknown"
            counts["wrongly_retained"] += negative and shot in selected
            counts["wrongly_dropped"] += not negative and shot not in selected
        counts["anchor_abstain"] += value["raw"]["S1"] == "unknown"
    return {"seeds": len(rows), **dict(counts)}


def evaluate(out, plan, clips, screen_path):
    screen = json.loads(screen_path.read_text())
    if set(screen) != set(plan["cases"]):
        raise ValueError("screen must cover every prespecified seed")
    results = {}
    for case, spec in plan["cases"].items():
        entry = screen[case]
        status = entry["eligible"]
        reason = entry["reason"].strip()
        if type(status) is not bool or not reason:
            raise ValueError(f"{case}: enter a screening decision and observations")
        result = results[case] = dict(spec, eligible=status, reason=reason)
        if not status:
            continue
        if set(entry["clicks"]) != set(SHOTS):
            raise ValueError(f"{case}: mark both landmarks in all three first frames")
        detail = {}
        for shot in SHOTS:
            key = f"{case}/{shot}"
            if key not in clips:
                raise ValueError(f"{case}: a failed generation was marked eligible")
            detail[shot] = compare_clip(frames(clips[key]), entry["clicks"][shot])
        by_arm = {
            "marker_match": dict.fromkeys(SHOTS, "same"),
            "framewise_geometric": {shot: full_frame(detail[shot]) for shot in SHOTS},
            "temporal_geometric": {shot: detail[shot]["decision"] for shot in SHOTS},
            "oracle": spec["truth"],
        }
        result.update(detail=detail, arms={arm: score(spec["truth"], verdicts)
                                           for arm, verdicts in by_arm.items()})
    aggregate = {}
    for group in ("all", *CONDITIONS):
        prespecified = [row for row in results.values() if group in ("all", row["condition"])]
        eligible = [row for row in prespecified if row["eligible"]]
        aggregate[group] = {"prespecified": len(prespecified), "eligible": len(eligible),
                            "arms": {arm: summarize(eligible, arm) for arm in ARMS}}
    write(out / "results.json", {
        "protocol_sha256": hashlib.sha256((Path(__file__).parent / "protocol.json").read_bytes()).hexdigest(),
        "screen_sha256": hashlib.sha256(screen_path.read_bytes()).hexdigest(),
        "aggregate": aggregate, "cases": results,
    })
    print(json.dumps(aggregate, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "score"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--screen", type=Path)
    args = parser.parse_args()
    if args.mode == "score" and args.screen is None:
        parser.error("score requires the completed --screen")
    plan = protocol()
    clips = verified_clips(args.source, plan)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.mode == "prepare":
        prepare(args.output, plan, clips)
    else:
        evaluate(args.output, plan, clips, args.screen)


if __name__ == "__main__":
    main()
