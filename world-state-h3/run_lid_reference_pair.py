"""Run the same-prompt H3 reference intervention for lid pilot seed 92802."""

import argparse
import hashlib
from pathlib import Path
import shutil
import sys

from audit_lid_video import audit, read_video
from run_scene_state_source import RUNNER, manifest, render_one


SEED = 92802
REFERENCE_SHA256 = '7373d9c45c1af3dadede37163218d9e031c61a411c048cbb00eaf4deb91d480b'
SOURCE_SHA256 = '608c7377f7566256be0ad0cb708a1f1b10ef8110117f4d7d83015dc13d3fe5c6'


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-video', required=True, type=Path)
    parser.add_argument('--reference', required=True, type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--timeout', type=float, default=2400)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()

    if not args.source_video.is_file() or sha256(args.source_video) != SOURCE_SHA256:
        parser.error('pass the archived seed 92802 before MP4')
    if not args.reference.is_file() or sha256(args.reference) != REFERENCE_SHA256:
        parser.error('pass the frozen lid-closed-reference.png')
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    if not args.prepare_only and (args.output is None or args.output.exists()):
        parser.error('pass a new --output directory')
    if not all(shutil.which(tool) for tool in ('ffmpeg', 'ffprobe')):
        parser.error('put ffmpeg and ffprobe on PATH')

    # Use the same target text even when the conditioning image is absent.
    text_only = manifest(SEED, 'after')
    image_conditioned = manifest(SEED, 'after')
    image_conditioned['assets'][0]['spec']['config']['reference_sha256'] = REFERENCE_SHA256
    arms = (('text-only', text_only, None),
            ('reference', image_conditioned, args.reference))

    sys.path.insert(0, str(RUNNER))
    from run_joint import live_setup
    api = live_setup(RUNNER / 'runtime')
    for _, data, _ in arms:
        runtime = api['AssetRuntime'](':memory:')
        try:
            runtime.initialize(data)
        finally:
            runtime.close()
    source = read_video(args.source_video, estimate=True)
    print(f'Validated both manifests and {len(source)} source frames', flush=True)
    if args.prepare_only:
        return

    backend = api['ComfyUI'](args.server)
    api['check_backend'](backend)
    args.output.mkdir(parents=True)
    api['write_json'](args.output / 'protocol.json', dict(
        seed=SEED, source_video_sha256=SOURCE_SHA256,
        reference_sha256=REFERENCE_SHA256, arm_order=[name for name, _, _ in arms],
        prompt=text_only['assets'][0]['spec']['template']))
    for name, data, reference in arms:
        print(f'Rendering {name}', flush=True)
        root = args.output / name
        render_one(api, backend, root, data, args.timeout, reference)
        video, = (root / 'media').glob('*.mp4')
        report = audit(source, read_video(video))
        api['write_json'](root / 'lid-audit.json', report)
        after = report['after']
        print(f"{name}: {after['consistent_frames']}/{after['total_frames']} frames",
              flush=True)


if __name__ == '__main__':
    main()
