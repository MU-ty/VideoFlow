"""Generate frozen Round 957 controlled H3 image-to-video source clips using ComfyUI."""
import argparse
import fcntl
import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path
from urllib import parse, request
from zipfile import ZIP_STORED, ZipFile

from comfyui import ComfyUI, SubmissionRejected, h3_workflow

ROOT = Path(__file__).resolve().parent


def protocol():
    return json.loads((ROOT / "protocol.json").read_text())


def write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def reference(plan, key):
    spec = plan["clips"][key]
    path = ROOT / spec["reference"]
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != spec["reference_sha256"]:
        raise ValueError(f"{key}: reference hash differs from the frozen protocol")
    return content


def check_backend(backend):
    backend.environment()
    info = backend._request("/object_info/MiniMaxH3ImageToVideo")
    node = info.get("MiniMaxH3ImageToVideo", {}).get("input", {})
    if not any("first_frame" in node.get(kind, {}) for kind in ("optional", "required")):
        raise RuntimeError("ComfyUI does not expose MiniMaxH3ImageToVideo.first_frame")
    if "LoadImage" not in backend._request("/object_info/LoadImage"):
        raise RuntimeError("ComfyUI does not expose LoadImage")


def upload_reference(backend, content):
    checksum = hashlib.sha256(content).hexdigest()
    filename = checksum + ".png"
    subfolder = "videoflow_round957"
    boundary = "videoflow-" + uuid.uuid4().hex
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            'Content-Type: image/png\r\n\r\n')
    tail = (f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="subfolder"\r\n\r\n'
            f'{subfolder}\r\n--{boundary}--\r\n')
    req = request.Request(backend.base + "/upload/image", data=head.encode() + content + tail.encode(),
                          headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with request.urlopen(req, timeout=backend.timeout) as res:
        result = json.load(res)
    if (result.get("name"), result.get("subfolder"), result.get("type")) != (filename, subfolder, "input"):
        raise RuntimeError(f"unexpected input upload response: {result}")
    params = parse.urlencode({"filename": filename, "subfolder": subfolder, "type": "input"})
    with request.urlopen(backend.base + "/view?" + params, timeout=backend.timeout) as res:
        if hashlib.sha256(res.read()).hexdigest() != checksum:
            raise RuntimeError("uploaded first-frame bytes changed")
    return f"{subfolder}/{filename}"


def workflow(plan, key, run_token, provider_image):
    spec = plan["clips"][key]
    graph = h3_workflow(spec["prompt"], seed=spec["noise_seed"], width=plan["width"],
                        height=plan["height"], frames=plan["frames"], steps=plan["steps"],
                        prefix=f"videoflow/round957/{run_token}/{key.replace('/', '-')}")
    graph["15"] = {"class_type": "LoadImage", "inputs": {"image": provider_image}}
    graph["5"]["inputs"]["first_frame"] = ["15", 0]
    return graph


def download(url, target):
    suffix = target.with_suffix(".download")
    with request.urlopen(url, timeout=120) as reader, suffix.open("wb") as stream:
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
            provider_image = upload_reference(backend, reference(plan, key))
            identifier = str(uuid.uuid4())
            record = {"prompt_id": identifier, "status": "submitting",
                      "reference_sha256": plan["clips"][key]["reference_sha256"],
                      "provider_image": provider_image}
            jobs[key] = record
            write_json(state_path, state)
            print(f"{key}: submitting {identifier}", flush=True)
            try:
                backend.submit(workflow(plan, key, state["run_token"], provider_image), identifier)
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
    bundle = root / "round957-h3-source.zip"
    scratch = bundle.with_suffix(".tmp")
    with ZipFile(scratch, "w", ZIP_STORED) as archive:
        for path in (root / "protocol.json", state_path, *sorted(root.glob("*/*.mp4"))):
            archive.write(path, path.relative_to(root))
        for spec in plan["clips"].values():
            archive.write(ROOT / spec["reference"], spec["reference"])
    scratch.replace(bundle)
    print(f"Completed clips: {sum(j['status'] == 'complete' for j in jobs.values())}/{len(plan['clips'])}",
          flush=True)
    print(bundle, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "check", "run"))
    parser.add_argument("--server", default="http://127.0.0.1:8188")
    parser.add_argument("--output", type=Path, default=Path("runs/h3-holdout-957"))
    parser.add_argument("--timeout", type=float, default=2400)
    parser.add_argument("--poll", type=float, default=2)
    args = parser.parse_args()
    if args.timeout <= 0 or args.poll <= 0:
        parser.error("timeout and poll must be positive")
    plan = protocol()
    if args.mode == "plan":
        for key in plan["clips"]:
            reference(plan, key)
            workflow(plan, key, "dry-run", plan["clips"][key]["reference"])
        print(json.dumps(plan, indent=2))
        return
    backend = ComfyUI(args.server)
    for key in plan["clips"]:
        reference(plan, key)
    check_backend(backend)
    if args.mode == "check":
        print("ComfyUI H3 first_frame and LoadImage verified.")
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
