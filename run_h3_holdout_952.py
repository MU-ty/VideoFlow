"""Generate the six prespecified VideoFlow Round 952 H3 source clips."""
import argparse
import fcntl
import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZIP_STORED, ZipFile

from comfyui import ComfyUI, SubmissionRejected, h3_workflow

SCENES = (
    (95211, "guitar-pedal",
     "a deep navy rectangular guitar effects pedal with exactly one round black gain knob",
     "a physically separate matte black compact electronic footswitch with no knob",
     "a pale gray repair mat in diffuse daylight"),
    (95221, "irrigation-timer",
     "a dark olive rectangular digital irrigation timer with exactly one round black selector knob",
     "a physically separate small black plastic relay box with no knob",
     "a matte white potting counter in diffuse daylight"),
)

RULES = (
    "Screen original videos before running any ownership rule. Retain a record for every seed. "
    "For S1 and S3, the blue triangle and knob must be clearly visible on the same host. "
    "For S2, the knob must be on the unmarked host and the only triangle on a second device, "
    "with a visible separation at some point in the clip. Report whether the gap opens over time. "
    "Record absent landmarks, copied decals, merged bodies, or other exclusions explicitly. "
    "Do not replace excluded seeds with newly chosen prompts in this batch."
)


def protocol():
    clips = {}
    for seed, case, host, neighbor, setting in SCENES:
        context = (
            f"Photorealistic continuous five-second product video on {setting}. "
            f"The main device is {host}. No cuts, hands, captions or extra logos. "
        )
        scripts = {
            "S1": (f"Stable frontal view. One small solid blue triangular sticker is attached "
                   f"to the flat face of the main device beside its knob. Show both points clearly."),
            "S2": (f"The main device has a knob and an entirely plain, unmarked face. On its right "
                   f"sits {neighbor}. The only solid blue triangular sticker is on the FRONT FACE "
                   f"of that second device. The devices never physically touch. Start from a "
                   f"shallow angle where their edges nearly overlap in projection, then slowly "
                   f"slide sideways so a strip of the tabletop is clearly visible between the two "
                   f"separate housings. Keep the main device's knob and the adjacent device's "
                   f"sticker visible in frame throughout. The main device has no sticker."),
            "S3": (f"Three-quarter view of the main device alone. One small solid blue triangular "
                   f"sticker is attached to the same flat face as the knob. Slight smooth camera "
                   f"orbit. Keep the sticker and the knob visible on one enclosure."),
        }
        for i, (shot, script) in enumerate(scripts.items()):
            key = f"{case}-{seed}/{shot}"
            clips[key] = {"noise_seed": seed + i, "prompt": context + script,
                          "expected_same_housing": shot != "S2"}
    return {"round": 952, "backend": "H3", "width": 864, "height": 480,
            "frames": 124, "steps": 20, "screening": RULES, "clips": clips}


def write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def workflow(plan, key, run_token):
    spec = plan["clips"][key]
    return h3_workflow(spec["prompt"], seed=spec["noise_seed"], width=plan["width"],
                       height=plan["height"], frames=plan["frames"], steps=plan["steps"],
                       prefix=f"videoflow/round952/{run_token}/{key.replace('/', '-')}")


def download(url, target):
    suffix = target.with_suffix(".download")
    with urlopen(url, timeout=120) as reader, suffix.open("wb") as stream:
        shutil.copyfileobj(reader, stream)
    with suffix.open("rb") as stream:
        header = stream.read(12)
    if len(header) < 12 or header[4:8] != b"ftyp":
        raise ValueError("expected an MP4 output from SaveVideo")
    suffix.replace(target)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(plan, backend, root, timeout, poll):
    state_path = root / "report.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        "run_token": uuid.uuid4().hex[:12], "jobs": {},
    }
    jobs = state["jobs"]
    for key in plan["clips"]:
        target = root / f"{key}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        record = jobs.get(key)
        if record and record["status"] == "complete":
            if not target.is_file() or digest(target) != record["sha256"]:
                raise RuntimeError(f"{key}: archived output is missing or has changed")
            continue
        if record and record["status"] == "failed":
            continue
        if record is None:
            identifier = str(uuid.uuid4())
            record = {"prompt_id": identifier, "status": "submitting"}
            jobs[key] = record
            write_json(state_path, state)
            print(f"{key}: submitting {identifier}", flush=True)
            try:
                backend.submit(workflow(plan, key, state["run_token"]), identifier)
            except SubmissionRejected as exc:
                record.update(status="failed", detail=str(exc))
                write_json(state_path, state)
                continue
            record["status"] = "accepted"
            write_json(state_path, state)
        identifier = record["prompt_id"]
        deadline = time.monotonic() + timeout
        unknown_since = None
        while time.monotonic() < deadline:
            status, detail = backend.inspect(identifier)
            if status == "succeeded":
                download(detail, target)
                record.update(status="complete", file=f"{key}.mp4", sha256=digest(target))
                print(f"{key}: saved {target}", flush=True)
                break
            if status == "failed":
                record.update(status="failed", detail=detail)
                print(f"{key}: generation failed", flush=True)
                break
            if status != "unknown":
                unknown_since = None
            elif unknown_since is None:
                unknown_since = time.monotonic()
            elif time.monotonic() - unknown_since >= 15:
                raise RuntimeError(
                    f"{key}: ComfyUI does not currently know prompt {identifier}. "
                    "The record is preserved to avoid a duplicate submission. Inspect server history."
                )
            time.sleep(poll)
        else:
            raise TimeoutError(f"{key}: still waiting for {identifier}. Resume with the same command.")
        write_json(state_path, state)
    bundle = root / "round952-h3-source.zip"
    scratch = bundle.with_suffix(".tmp")
    with ZipFile(scratch, "w", ZIP_STORED) as archive:
        for path in (root / "protocol.json", state_path, *sorted(root.glob("*/*.mp4"))):
            archive.write(path, path.relative_to(root))
    scratch.replace(bundle)
    print(f"Completed clips: {sum(j['status'] == 'complete' for j in jobs.values())}/{len(plan['clips'])}",
          flush=True)
    print(bundle, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "check", "run"))
    parser.add_argument("--server", default="http://127.0.0.1:8188")
    parser.add_argument("--output", type=Path, default=Path("runs/h3-holdout-952"))
    parser.add_argument("--timeout", type=float, default=2400)
    parser.add_argument("--poll", type=float, default=2)
    args = parser.parse_args()
    if args.timeout <= 0 or args.poll <= 0:
        parser.error("timeout and poll must be positive")
    plan = protocol()
    if args.mode == "plan":
        for key in plan["clips"]:
            workflow(plan, key, "dry-run")
        print(json.dumps(plan, indent=2))
        return
    backend = ComfyUI(args.server)
    backend.environment()
    if args.mode == "check":
        print("ComfyUI API reachable.")
        return
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / ".run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("another runner is using this output directory")
        plan_path = args.output / "protocol.json"
        if plan_path.exists() and json.loads(plan_path.read_text()) != plan:
            raise ValueError(f"{plan_path}: existing protocol differs")
        write_json(plan_path, plan)
        run(plan, backend, args.output, args.timeout, args.poll)


if __name__ == "__main__":
    main()
