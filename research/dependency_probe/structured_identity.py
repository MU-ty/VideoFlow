"""Run a structured identity recheck on the archived VideoFlow panels."""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError

SHAPES = ("circle", "triangle", "diamond", "star", "hexagon", "rectangle", "stripe", "other", "unknown")
COLORS = ("white", "black", "red", "orange", "yellow", "blue", "green", "purple", "silver", "other", "unknown")
LOCATIONS = ("body", "base", "handle", "lid", "shade", "other", "unknown")
TRISTATE = ("yes", "no", "unknown")

# Explicit exclusions require both attributes. Unknown color stays unknown.
RULES = {
    "diamond-canister": ("canister lid", "canister", ("diamond", "white", "body"),
                        [("circle", "white", "body")]),
    "star-lunchbox": ("carry handle", "lunchbox", ("star", "white", "body"),
                      [("triangle", "white", "body")]),
    "hexagon-lamp": ("lamp shade", "lamp", ("hexagon", "black", "base"),
                    [("triangle", "orange", "base"), ("triangle", "red", "base")]),
}

FIELDS = {
    "panel": {"enum": [1, 2]},
    "host_visible": {"enum": list(TRISTATE)},
    "part_visible": {"enum": list(TRISTATE)},
    "same_instance": {"enum": list(TRISTATE)},
    "complete": {"type": "boolean"},
    "shape": {"enum": list(SHAPES)},
    "color": {"enum": list(COLORS)},
    "location": {"enum": list(LOCATIONS)},
}
SCHEMA = {
    "type": "object",
    "properties": {"panels": {"type": "array", "minItems": 2, "maxItems": 2, "items": {
        "type": "object", "properties": FIELDS, "required": list(FIELDS),
        "additionalProperties": False}}},
    "required": ["panels"],
    "additionalProperties": False,
}
INSTRUCTIONS = (
    "Describe two video frames independently, left panel 1 and right panel 2. "
    "Inspect the host object for an identity mark printed or painted on it. "
    "The color or shape of the edited part itself is not an identity mark. "
    "Record host_visible and part_visible independently. Record mark shape, color, "
    "and location on the host, and whether the mark belongs to the same instance "
    "as the edited part. Do not guess hidden colors or shapes. Use unknown for each "
    "unreadable field. Set complete=true only if the panel is readable and you can "
    "rule out an unexamined host instance. If more than one host instance is visible, "
    "set complete=false and same_instance=unknown. Return two panel records only."
)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def parse_panels(raw, pair):
    reply = json.loads(raw)
    if not isinstance(reply, dict) or set(reply) != {"panels"}:
        raise ValueError("expected a panels object")
    rows = reply["panels"]
    if not isinstance(rows, list) or len(rows) != 2:
        raise ValueError("expected two panel records")
    parsed = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(FIELDS):
            raise ValueError("unexpected panel fields")
        for field, spec in FIELDS.items():
            value = row[field]
            valid = type(value) is bool if field == "complete" else (
                type(value) is (int if field == "panel" else str) and value in spec["enum"])
            if not valid:
                raise ValueError(f"invalid {field}")
        panel = pair[row["panel"] - 1]
        if panel in parsed:
            raise ValueError("duplicate panel")
        parsed[panel] = {**row, "panel": panel}
    return parsed


def decide(panels, target, excluded):
    classifications = {}
    for index, row in panels.items():
        marker = tuple(row[key] for key in ("shape", "color", "location"))
        linked = row["host_visible"] == row["part_visible"] == row["same_instance"] == "yes"
        if linked and marker == target:
            status = "target"
        elif linked and row["complete"] and marker in excluded:
            status = "excluded"
        elif (row["complete"] and row["host_visible"] == row["part_visible"] == "no"
              and row["same_instance"] == row["shape"] == row["color"] == row["location"] == "unknown"):
            status = "absent_host"
        else:
            status = "unknown"
        classifications[index] = status
    states = set(classifications.values())
    if "target" in states:
        decision = "confirmed"
    elif set(panels) == {1, 2, 3, 4} and "excluded" in states and states <= {"excluded", "absent_host"}:
        decision = "rejected"
    else:
        decision = "unknown"
    return decision, classifications


def recheck(root, server, image_reply):
    protocol = read(root / "protocol.json")
    story = protocol["story"]
    part, host, target, excluded = RULES[story]
    prompt = INSTRUCTIONS + f" Edited part to locate: {part}. Host type: {host}."
    report = read(root / "report.json")
    evidence = read(root / "evidence.json")
    policies = {row["policy"]: set(row["final_selection"]) for row in report["rows"]}
    worklist = policies["worklist"]
    archived = policies["online_egdr"]
    selected = archived.copy()
    reviews = {}
    for shot in sorted(archived - worklist):
        files = [root / f"{shot}-panel-{index}.png" for index in range(1, 5)]
        hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in files]
        if hashes != evidence["audit"]["probes"][shot]["panels"]:
            raise ValueError(f"{root}: panel digest mismatch for {shot}")
        panels = {}
        calls = []
        for pair in ((1, 2), (3, 4)):
            raw = None
            try:
                with TemporaryDirectory() as directory:
                    sheet = Path(directory) / "pair.png"
                    subprocess.run(
                        ["ffmpeg", "-v", "error", "-i", str(files[pair[0]-1]),
                         "-i", str(files[pair[1]-1]), "-filter_complex_threads", "1",
                         "-filter_complex", "hstack=inputs=2", "-frames:v", "1",
                         "-threads", "1", str(sheet)], check=True)
                    raw = image_reply(server, protocol["vision_model"], sheet, prompt, SCHEMA)
                panels.update(parse_panels(raw, pair))
                error = None
            except (ValueError, URLError, TimeoutError) as exc:
                error = str(exc)
            calls.append(dict(pair=pair, raw=raw, error=error))
            if decide(panels, target, excluded)[0] == "confirmed":
                break
        decision, states = decide(panels, target, excluded)
        if decision == "rejected":
            selected.remove(shot)
        reviews[shot] = dict(decision=decision, panels=panels, states=states,
                             hashes=hashes, calls=calls)

    labels = read(root / "oracle_labels.json")
    source = read(root / "source/revision-1.json")
    hashes = {entry["asset"]: hashlib.sha256((root / "source" / entry["file"]).read_bytes()).hexdigest()
              for entry in source["assets"]}
    if hashes != labels["source_sha256"] or source["timeline_hash"] != report["source_timeline_hash"]:
        raise ValueError(f"{root}: oracle and source archive do not match")
    truth = set(labels["target_shots"])
    return dict(story=story, seed=protocol["seed"], model=protocol["vision_model"], prompt=prompt,
                archived=dict(selected=sorted(archived), missed=sorted(truth-archived),
                              excess=sorted(archived-truth)),
                structured=dict(selected=sorted(selected), missed=sorted(truth-selected),
                                excess=sorted(selected-truth)),
                attempted_calls=sum(len(row["calls"]) for row in reviews.values()), reviews=reviews)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--server", default="http://127.0.0.1:11434")
    parser.add_argument("runs", type=Path, nargs="+")
    args = parser.parse_args()
    sys.path[:0] = [str(args.repo.resolve()),
                    str((args.repo / "research/dependency_probe").resolve())]
    from vision_probe import image_reply

    result = dict(scope="retrospective_shot_selection", calls_per_candidate_budget=2,
                  rules=RULES, schema=SCHEMA, runs=[])
    for run in args.runs:
        print(f"Rechecking {run}", file=sys.stderr, flush=True)
        result["runs"].append(recheck(run, args.server, image_reply))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
