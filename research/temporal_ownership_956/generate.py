"""Generate frozen Round 956 native H3 source clips using ComfyUI."""
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

ROOT = Path(__file__).resolve().parent


def protocol():
    return json.loads((ROOT / "protocol.json").read_text())


def write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def workflow(plan, key, run_token):
    spec = plan["clips"][key]
    return h3_workflow(spec["prompt"], seed=spec["noise_seed"], width=plan["width"],
                       height=plan["height"], frames=plan["frames"], steps=plan["steps"],
                       prefix=f"videoflow/round956/{run_token}/{key.replace('/', '-')}")


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
    bundle = root / "round956-h3-source.zip"
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
    parser.add_argument("--output", type=Path, default=Path("runs/h3-holdout-956"))
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
