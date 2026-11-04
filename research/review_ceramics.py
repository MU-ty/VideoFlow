"""Extract archived video pairs and run a read-only visual review with Gemini."""
import argparse
import base64
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from zipfile import ZipFile

PREFIX = "v8-pilot-01/ceramics-seed-42/dair/artifacts/"
TIMES = (0, 1, 2, 3, 4)
PROMPTS = {
    "S6": (
        "比较原片和改后片。目标是只把同一把茶壶的白色釉面改成宝蓝色。"
        "分别判断壶身轮廓、把手、壶嘴、壶盖和背景是否保持。"
        "每项给出保持、变化或看不清，注明时间点及可见依据。"
        "最后分别回答改色是否成功、茶壶结构是否保留。"
    ),
    "S1": "比较原片和改后片，判断是否有可见差异。仅报告有画面依据的差异并标明时间点。",
}


def extract_pairs(archive_path, output):
    pairs = {scene: [] for scene in PROMPTS}
    with ZipFile(archive_path) as archive:
        for revision, label in ((1, "before"), (2, "after")):
            manifest = json.loads(archive.read(f"{PREFIX}revision-{revision}.json"))
            for scene, pair in pairs.items():
                asset, = [a for a in manifest["assets"] if a["scene"] == scene]
                filename = asset["file"]
                if Path(filename).name != filename or not filename.endswith(".mp4"):
                    raise ValueError(f"Unexpected archive member name for {scene}")
                video = archive.read(PREFIX + filename)
                digest = hashlib.sha256(video).hexdigest()
                if digest != asset["sha256"]:
                    raise ValueError(f"Archive SHA-256 mismatch for {scene} {label}")
                path = output / f"{scene}_{label}.mp4"
                path.write_bytes(video)
                pair.append({"label": label, "file": path.name, "sha256": digest})
    return pairs


def ffmpeg_path():
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise SystemExit("需要 ffmpeg，或在当前 Python 环境安装 imageio-ffmpeg。") from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


def build_parts(output, pair, prompt, ffmpeg):
    parts = [{"text": prompt + " 只根据给定的抽样画面判断。用中文回答。"}]
    for second in TIMES:
        for item in pair:
            image = subprocess.check_output([
                ffmpeg, "-nostdin", "-v", "error", "-ss", str(second),
                "-i", str(output / item["file"]), "-frames:v", "1",
                "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ])
            if not image:
                raise ValueError(f"Missing frame: {item['file']} at {second}s")
            label = "原片" if item["label"] == "before" else "改后"
            parts.extend([
                {"text": f"{label} {second} 秒"},
                {"inline_data": {"mime_type": "image/jpeg",
                                 "data": base64.b64encode(image).decode("ascii")}},
            ])
    return parts


def write_local_review(output, pairs, ffmpeg):
    sections = []
    for scene, pair in pairs.items():
        parts = build_parts(output, pair, PROMPTS[scene], ffmpeg)
        frames = [part["inline_data"]["data"] for part in parts if "inline_data" in part]
        rows = []
        for i, second in enumerate(TIMES):
            images = [
                f'<td><img alt="{label} {second}s" src="data:image/jpeg;base64,{data}"></td>'
                for label, data in zip(("原片", "改后"), frames[2*i:2*i+2])
            ]
            rows.append(f"<tr><th>{second}s</th>{''.join(images)}</tr>")
        links = " · ".join(
            f'<a href="{escape(item["file"])}">{escape(item["file"])}</a>'
            for item in pair
        )
        sections.append(
            f"<section><h2>{scene}</h2><p>{escape(PROMPTS[scene])}</p><p>{links}</p>"
            "<table><thead><tr><th>时刻</th><th>原片</th><th>改后</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></section>"
        )
    report = output / "review.html"
    report.write_text(
        '<!doctype html><html lang="zh"><meta charset="utf-8">'
        '<title>陶瓷案例前后对照</title>'
        '<style>body{font:16px system-ui;margin:24px auto;max-width:1000px;padding:0 16px}'
        'table{width:100%;table-layout:fixed}th:first-child{width:55px}'
        'img{width:100%;display:block}section{margin-bottom:32px}</style>'
        '<h1>陶瓷案例前后对照</h1><p>人工复核抽样画面。'
        '完整动作请打开每个镜头的视频文件。</p>'
        + "".join(sections) + "</html>",
        encoding="utf-8",
    )
    print(f"Local review: {report}")


def call_model(model, key, parts):
    body = json.dumps({"contents": [{"role": "user", "parts": parts}]}).encode()
    if len(body) > 18_000_000:
        raise ValueError("Review payload is too large for inline submission.")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    request = urllib.request.Request(
        url, data=body, headers={"x-goog-api-key": key, "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Gemini API HTTP {exc.code}. Check model access, key and quota.") from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default="gemini-4-argon")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare-only", action="store_true")
    mode.add_argument("--local-review", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", args.model):
        parser.error("Use a Gemini model ID without the models/ prefix.")
    key = os.environ.get("GEMINI_API_KEY")
    if not (args.prepare_only or args.local_review) and not key:
        parser.error("Set GEMINI_API_KEY, or use --prepare-only.")
    ffmpeg = None if args.prepare_only else ffmpeg_path()
    args.output.mkdir(parents=True, exist_ok=False)
    pairs = extract_pairs(args.archive, args.output)
    if pairs["S1"][0]["sha256"] != pairs["S1"][1]["sha256"]:
        raise ValueError("Expected identical S1 control videos.")
    if pairs["S6"][0]["sha256"] == pairs["S6"][1]["sha256"]:
        raise ValueError("Expected distinct S6 videos.")
    (args.output / "pairs.json").write_text(
        json.dumps({"model": None if args.local_review or args.prepare_only else args.model, "sample_seconds": TIMES, "pairs": pairs},
                   ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.prepare_only:
        print(f"Verified video pairs: {args.output}")
        return
    if args.local_review:
        write_local_review(args.output, pairs, ffmpeg)
        return
    for scene, prompt in PROMPTS.items():
        parts = build_parts(args.output, pairs[scene], prompt, ffmpeg)
        result = call_model(args.model, key, parts)
        (args.output / f"{scene}_response.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        candidates = result.get("candidates", [])
        answer = "\n".join(
            part["text"] for candidate in candidates
            for part in candidate.get("content", {}).get("parts", [])
            if "text" in part and not part.get("thought")
        ).strip()
        if not answer:
            raise SystemExit(f"No review text for {scene}; inspect the saved response JSON.")
        (args.output / f"{scene}_review.txt").write_text(answer + "\n", encoding="utf-8")
        print(f"Saved {scene}: {args.output / (scene + '_review.txt')}")


if __name__ == "__main__":
    main()
