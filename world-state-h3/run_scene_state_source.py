"""Generate the fixed-camera H3 lid-placement pilot and archive four-frame sheets."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time

HERE = Path(__file__).resolve().parent
RUNNER = HERE.parent / 'VideoFlow_Natural_Gate'
SEEDS = (92801, 92802)
COMMON = (
    'One continuous wide overhead product shot. Locked fixed camera, no zoom, no pan, '
    'no cuts, no hands. Three evenly spaced items on an uncluttered pale tabletop: '
    'a dark rectangular tray on the left, a cream ceramic teapot with an orange star '
    'on the right, and an unrelated small green mug at the top center. '
    'Keep the entire tray, pot opening, and mug fully inside the frame from start to end.'
)
STATES = {
    'before': ' The pot mouth is open and empty. Its single teal ceramic lid with '
              'a black knob is lying flat on the left tray, detached from the pot.',
    'after': ' The left tray is empty. The single teal ceramic lid with a black knob '
             'sits flush over the mouth of the cream teapot on the right.',
}


def manifest(seed, stage, reference_digest=None):
    config = dict(seed=seed)
    prompt = COMMON + STATES[stage]
    if reference_digest is not None:
        config['reference_sha256'] = reference_digest
        prompt += (' The reference is the completed static arrangement. Hold that arrangement '
                   'from the first frame through the last. Keep the empty tray in its original '
                   'vertical orientation, the mug stationary, and the teapot continuously closed.')
    return dict(assets=[dict(id='wide', scene='wide', kind='video',
                            spec=dict(template=prompt, reads=[], values={}, cost=1, config=config))],
                edges=[])


def build_workflow(inputs, job_id, refs, reference):
    if reference is None:
        return refs.build(inputs, job_id)
    digest, provider_image = reference
    config = dict(inputs['spec']['config'])
    if config.pop('reference_sha256') != digest:
        raise ValueError('reference does not match the bound job digest')
    workflow = refs.build(dict(inputs, spec=dict(inputs['spec'], config=config)), job_id)
    workflow['15'] = dict(class_type='LoadImage', inputs=dict(image=provider_image))
    workflow['5']['inputs']['first_frame'] = ['15', 0]
    return workflow


def render_one(api, backend, root, data, timeout, reference_path=None):
    root.mkdir()
    api['write_json'](root / 'manifest.json', data)
    refs = api['ReferenceFrames'](root / 'references', backend)
    reference = None
    if reference_path is not None:
        local = root / 'reference.png'
        shutil.copyfile(reference_path, local)
        digest = api['checksum'](local)
        if digest != data['assets'][0]['spec']['config']['reference_sha256']:
            raise ValueError('reference changed after manifest creation')
        provider_image = api['upload_frame'](backend, local, digest)
        reference = (digest, provider_image)
        api['write_json'](root / 'reference.json', dict(sha256=digest, provider_image=provider_image))

    def build(inputs, job_id):
        return build_workflow(inputs, job_id, refs, reference)

    runtime = api['AssetRuntime'](root / 'run.db')
    try:
        runtime.initialize(data)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = api['pass_once'](runtime, backend, build=build, max_submit=0)
            free = int(not runtime.unresolved_remote())
            events += api['pass_once'](api['SubmissionView'](runtime), backend,
                                       build=build, max_submit=free)
            with (root / 'events.jsonl').open('a') as log:
                log.write(json.dumps(events) + '\n')
            if any(e['action'] in ('failed', 'rejected', 'validation_error') for e in events):
                raise RuntimeError(f'H3 worker error. See {root}/events.jsonl')
            if runtime.snapshot()['timeline']:
                break
            time.sleep(2)
        else:
            raise TimeoutError(f'No completed H3 render. See {root}/events.jsonl')
        archived = api['archive_current'](runtime, root / 'media', backend.base, probe=True)
        item, = archived['assets']
        api['make_sheet'](root / 'media' / item['file'], root / 'screen.png')
    finally:
        runtime.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--stage', choices=STATES, default='before')
    parser.add_argument('--seeds', nargs='+', type=int, choices=SEEDS)
    parser.add_argument('--timeout', type=float, default=2400)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--reference', type=Path, help='PNG first frame for one screened after seed')
    args = parser.parse_args()
    if args.stage == 'after' and not args.seeds:
        parser.error('pass the screened source seeds with --seeds')
    seeds = args.seeds or SEEDS
    if args.reference is not None and (args.stage != 'after' or len(seeds) != 1
                                       or not args.reference.is_file()
                                       or args.reference.suffix.lower() != '.png'):
        parser.error('--reference requires an existing PNG and one after seed')
    if args.timeout <= 0 or len(set(seeds)) != len(seeds):
        parser.error('timeout must be positive and seeds must be unique')
    sys.path.insert(0, str(RUNNER))
    from run_joint import live_setup
    api = live_setup((RUNNER / 'runtime').resolve())
    digest = api['checksum'](args.reference) if args.reference else None
    suffix = 'after-ref' if args.reference else args.stage
    if args.prepare_only:
        for seed in seeds:
            runtime = api['AssetRuntime'](':memory:')
            try:
                runtime.initialize(manifest(seed, args.stage, digest))
            finally:
                runtime.close()
        print('Validated', args.stage, 'manifests for', *seeds)
        return
    if not all(shutil.which(tool) for tool in ('ffmpeg', 'ffprobe')):
        parser.error('put ffmpeg and ffprobe on PATH')
    for seed in seeds:
        path = args.output / f'{seed}-{suffix}'
        if path.exists():
            parser.error(f'{path} already exists. Use another output directory or select unrun seeds.')
        if args.stage == 'after' and not (args.output / f'{seed}-before' / 'screen.png').is_file():
            parser.error(f'before sheet missing for {seed}')
    backend = api['ComfyUI'](args.server)
    api['check_backend'](backend)
    args.output.mkdir(parents=True, exist_ok=True)
    for seed in seeds:
        root = args.output / f'{seed}-{suffix}'
        print('Rendering', seed, args.stage, flush=True)
        render_one(api, backend, root, manifest(seed, args.stage, digest), args.timeout, args.reference)
        print(root / 'screen.png', flush=True)


if __name__ == '__main__':
    main()
