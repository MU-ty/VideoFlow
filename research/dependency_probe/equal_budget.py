"""Compare a direct identity judgment with archived structured verification."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
from time import monotonic
from urllib.error import URLError
from urllib.request import Request, urlopen


MODEL = "qwen2.5vl:7b-ctx8k"
OPTIONS = {"temperature": 0, "seed": 42, "num_predict": 384}
PAIRS = ((1, 2), (3, 4))
VERDICTS = {"target", "excluded", "absent_host", "unknown"}
PROMPT = """Inspect these two panels independently (1 left, 2 right).
The target is a dark gray desk timer with a white hexagonal badge on its
body and a visible silver control knob belonging to the same timer.
The excluded identity is a dark gray desk timer with a white circular
badge on its body and a visible silver control knob belonging to that timer.
Classify each panel directly: target, excluded, absent_host (no timer is
visible), or unknown (identity, badge, knob, or ownership is unclear).
Use unknown for ambiguous, incomplete, or contradictory evidence.
Return only JSON: {"panels":[{"panel":1,"verdict":"unknown"},
{"panel":2,"verdict":"unknown"}]}. Replace the example verdicts as appropriate."""


def read(path):
    return json.loads(path.read_text())


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse(raw):
    panels = json.loads(raw)["panels"]
    if len(panels) != 2 or {row["panel"] for row in panels} != {1, 2}:
        raise ValueError("expected exactly panels 1 and 2")
    verdicts = [row["verdict"] for row in sorted(panels, key=lambda row: row["panel"])]
    if not set(verdicts) <= VERDICTS:
        raise ValueError("invalid panel verdict")
    return verdicts


def aggregate(verdicts):
    if "target" in verdicts:
        return "confirmed"
    if "excluded" in verdicts and set(verdicts) <= {"excluded", "absent_host"}:
        return "rejected"
    return "unknown"


def prepare(run, output):
    protocol = read(run / "protocol.json")
    if protocol["story"] != "desk-timer" or protocol["vision_model"] != MODEL:
        raise ValueError(f"unexpected story or model in {run}")
    seed = protocol["seed"]
    evidence = read(run / "evidence.json")
    responses = run / "responses.jsonl"
    rows = [json.loads(line) for line in responses.read_text().splitlines()]
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != evidence["responses_sha256"]:
        raise ValueError(f"response log hash mismatch in {run}")
    calls = []
    structured = {}
    for shot, probe in sorted(evidence["audit"]["probes"].items()):
        if not probe["recheck_calls"]:
            continue
        matching = [row for row in rows if row["probe"] == f"probe-{shot}"
                    and row["stage"] == "structured"]
        if [tuple(row["pair"]) for row in matching] != list(PAIRS):
            raise ValueError(f"unexpected structured budget in {run}: {shot}")
        panels = {i: run / f"{shot}-panel-{i}.png" for pair in PAIRS for i in pair}
        if [sha256(path) for path in panels.values()] != probe["panels"]:
            raise ValueError(f"panel hash mismatch in {run}: {shot}")
        structured[shot] = probe["structured_advisory"]
        for left, right in PAIRS:
            sheet = output / f"{seed}-{shot}-{left}-{right}.png"
            subprocess.run(
                ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-i", str(panels[left]), "-i", str(panels[right]),
                 "-filter_complex_threads", "1", "-filter_complex", "hstack=inputs=2",
                 "-frames:v", "1", "-threads", "1", str(sheet)],
                check=True,
            )
            calls.append({"shot": shot, "pair": [left, right], "sheet": sheet.name,
                          "sha256": sha256(sheet)})
    return {
        "seed": seed, "calls": calls, "structured": structured,
        "anchors": protocol["case"]["declared"],
        "targets": read(run / "oracle_labels.json")["target_shots"],
        "protocol_hashes": protocol["holdout_files_sha256"],
        "archived_response_sha256": evidence["responses_sha256"],
    }


def request_json(url, payload=None, timeout=600):
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(url, data, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def compare(plan, log, args):
    direct = {shot: [] for shot in plan["structured"]}
    failures = 0
    for call in plan["calls"]:
        image = base64.b64encode((args.output / call["sheet"]).read_bytes()).decode()
        payload = {"model": MODEL, "stream": False, "format": "json", "options": OPTIONS,
                   "messages": [{"role": "user", "content": PROMPT, "images": [image]}]}
        start = monotonic()
        raw = None
        error = None
        try:
            raw = request_json(args.server + "/api/chat", payload, args.timeout)["message"]["content"]
            verdicts = parse(raw)
        except (URLError, OSError, ValueError, KeyError, TypeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            failures += 1
            verdicts = ["unknown", "unknown"]
        direct[call["shot"]].extend(verdicts)
        print(json.dumps({**call, "seed": plan["seed"], "raw": raw, "error": error,
                          "verdicts": verdicts, "seconds": monotonic() - start}),
              file=log, flush=True)
        print(plan["seed"], call["shot"], call["pair"], error or verdicts, flush=True)

    targets = set(plan["targets"])
    result = {"seed": plan["seed"], "calls_per_arm": len(plan["calls"]),
              "direct_failed_calls": failures}
    for method, verdicts in (("direct", {shot: aggregate(v) for shot, v in direct.items()}),
                             ("archived_structured", plan["structured"])):
        selected = set(plan["anchors"]) | {shot for shot, verdict in verdicts.items()
                                            if verdict != "rejected"}
        result[method] = {"verdicts": verdicts, "selected": sorted(selected),
                          "missed": sorted(targets - selected), "excess": sorted(selected - targets)}
    return result


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs=2, type=Path, help="paired archive directories")
    parser.add_argument("--output", required=True, type=Path, help="new output directory")
    parser.add_argument("--server", default="http://127.0.0.1:11434", type=lambda url: url.rstrip("/"))
    parser.add_argument("--timeout", default=600, type=float)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()

    metadata = {"model": MODEL, "options": OPTIONS, "prompt": PROMPT, "status": "prepared"}
    if not args.prepare_only:
        tags = request_json(args.server + "/api/tags", timeout=10)["models"]
        installed = next((tag for tag in tags if tag["name"] == MODEL), None)
        if installed is None:
            parser.error(f"model {MODEL} is unavailable on {args.server}")
        metadata["installed_model"] = installed

    args.output.mkdir(parents=True)
    plans = [prepare(run, args.output) for run in args.runs]
    if ({plan["seed"] for plan in plans} != {90601, 90602}
            or any(len(plan["calls"]) != 4 for plan in plans)
            or plans[0]["protocol_hashes"] != plans[1]["protocol_hashes"]):
        raise ValueError("expected the two frozen Desk-Timer archives with four calls per seed")
    write_json(args.output / "plan.json", {**metadata, "runs": plans})
    if args.prepare_only:
        print("Prepared and hash-checked 8 calls. No inference requested.")
        return

    with (args.output / "responses.jsonl").open("w") as log:
        results = [compare(plan, log, args) for plan in plans]
    status = "completed_with_call_failures" if any(r["direct_failed_calls"] for r in results) else "completed"
    write_json(args.output / "report.json", {"status": status, "results": results})


if __name__ == "__main__":
    main()
