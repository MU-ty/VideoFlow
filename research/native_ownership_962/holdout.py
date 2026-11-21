"""Prepare and score a prospectively numbered native ownership holdout."""
import argparse
import base64
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen

from evaluate import compare_clip, frames, prepare, score, summarize, write

ROOT = Path(__file__).resolve().parent
LABELS = {"same", "distinct", "unknown"}


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def request(url, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = Request(url, data=body, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=180) as response:
        return json.load(response)


def encode(frame):
    data = BytesIO()
    frame.save(data, format="PNG")
    return base64.b64encode(data.getvalue()).decode("ascii")


def vlm_vote(server, plan, images):
    indices = [round((len(images) - 1) * fraction) for fraction in plan["frame_fractions"]]
    payload = {
        "model": plan["model"], "stream": False,
        "options": {"temperature": 0, "seed": 962, "num_predict": 16},
        "messages": [{"role": "user", "content": plan["vlm_instruction"],
                      "images": [encode(images[i]) for i in indices]}],
    }
    payload_digest = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    start = time.monotonic()
    result = request(server + "/api/chat", payload)
    elapsed = time.monotonic() - start
    raw = result["message"]["content"]
    label = raw.strip().lower()
    return {"label": label if label in LABELS else "unknown", "raw": raw,
            "unparsed": label not in LABELS, "seconds": elapsed,
            "indices_zero_based": indices, "payload_sha256": payload_digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "lock", "score"))
    parser.add_argument("--root", type=Path, required=True, help="Root with 962xx/S1.mp4 etc.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--screen", type=Path)
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--ollama", default="http://127.0.0.1:11434")
    args = parser.parse_args()
    plan_path = ROOT / "plan.json"
    plan = json.loads(plan_path.read_text())
    cases = plan["cases"]
    shots = plan["shots"]
    source = {f"{case}/{s}": args.root / case / f"{s}.mp4"
              for case in cases for s in shots}
    manifest = {key: digest(path) if path.is_file() else None
                for key, path in source.items()}
    populated = [value for value in manifest.values() if value]
    if len(populated) != len(set(populated)):
        raise ValueError("Video files must not be reused across slots")
    args.out.mkdir(parents=True, exist_ok=True)
    if args.mode == "prepare":
        prepare(args.out, plan, {k: p for k, p in source.items() if manifest[k]})
        return
    if args.screen is None:
        parser.error("lock and score require --screen")
    screen = json.loads(args.screen.read_text())
    if set(screen) != set(cases):
        raise ValueError("Screen must include all eight planned cases")
    for case, entry in screen.items():
        if type(entry["eligible"]) is not bool or not entry["reason"].strip():
            raise ValueError(f"{case}: eligibility and reason required")
        if entry["eligible"] and (set(entry["clicks"]) != set(shots)
                                  or any(not manifest[f"{case}/{s}"] for s in shots)):
            raise ValueError(f"{case}: all three clips and paired points are required")
    stamp = {"plan_sha256": digest(plan_path), "screen_sha256": digest(args.screen),
             "source_sha256": manifest}
    if args.mode == "lock":
        with (args.out / "input-lock.json").open("x") as out:
            json.dump(stamp, out, indent=2)
        return
    if args.lock is None or json.loads(args.lock.read_text()) != stamp:
        raise ValueError("Source, plan, or human screen differs from the frozen lock")
    server = args.ollama.rstrip("/")
    models = request(server + "/api/tags")["models"]
    matches = [m["digest"] for m in models if m["name"] == plan["model"]]
    if len(matches) != 1 or not matches[0]:
        raise ValueError("The exact frozen VLM tag is missing")
    rows = {}
    for case, entry in screen.items():
        row = {"eligible": entry["eligible"], "reason": entry["reason"],
               "challenges": cases[case], "truth": plan["truth"]}
        rows[case] = row
        if not entry["eligible"]:
            continue
        temporal, vlm = {}, {}
        for shot in shots:
            images = frames(source[f"{case}/{shot}"])
            if len(images) < 12:
                raise ValueError(f"{case}/{shot}: too short for fixed sampling")
            temporal[shot] = compare_clip(images, entry["clicks"][shot])
            vlm[shot] = vlm_vote(server, plan, images)
        raw = {
            "marker_match": dict.fromkeys(shots, "same"),
            "full_frame_vlm": {s: vlm[s]["label"] for s in shots},
            "temporal_geometry": {s: temporal[s]["decision"] for s in shots},
            "oracle": plan["truth"],
        }
        row.update(temporal=temporal, vlm=vlm,
                   arms={name: score(plan["truth"], labels) for name, labels in raw.items()})
        for arm in row["arms"].values():
            arm["planned_jobs"] = len(arm["selected"])
        write(args.out / "progress.json", rows)
    end_digests = [m["digest"] for m in request(server + "/api/tags")["models"]
                   if m["name"] == plan["model"]]
    if end_digests != matches:
        raise RuntimeError("VLM digest changed during evaluation")
    tags = sorted({tag for labels in cases.values() for tag in labels})
    methods = ("marker_match", "full_frame_vlm", "temporal_geometry", "oracle")
    aggregate = {}
    for tag in ("all", *tags):
        group = [r for r in rows.values() if tag == "all" or tag in r["challenges"]]
        eligible = [r for r in group if r["eligible"]]
        aggregate[tag] = {
            "screened": len(group), "eligible": len(eligible),
            "arms": {m: dict(summarize(eligible, m),
                             planned_jobs=sum(r["arms"][m]["planned_jobs"] for r in eligible))
                     for m in methods},
        }
    write(args.out / "results.json", {
        "inputs": stamp, "vlm_digest": matches[0], "aggregate": aggregate,
        "actual_provider_jobs": None, "cases": rows,
    })


if __name__ == "__main__":
    main()
