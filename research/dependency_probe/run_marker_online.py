"""Run the matched marker comparison through the Table VI publication gate."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
from statistics import median
import sys
import time

import run_marker_split as split

pair = split.pair
ARMS = ('direct-worklist', 'egdr-split-marker')
ENGINE_FILES = {
    'run_strong_baseline.py': 'bf3d613362dc05334ee181a56ac84e9e832c784bdc55eb1c9d464c4b246ce059',
    'run_weak_identity.py': 'd5f481ac6772f988e66aa92ae5f84464b7a9ff8406261b2e44deb44651ef4628',
    'run_online.py': 'b9281af8e454ec0be51ce5077e1f982b4efcc9299e2bb901a37dd741c72de3a7',
}


def observer_for(prepared, inputs):
    def observe(args, api, frozen, verifier, case, revision, journal, policy):
        started = time.monotonic()
        source_hashes = json.loads((frozen / 'index.json').read_text())
        shots = {shot: dict(calls=[], votes=[]) for shot in pair.CANDIDATES}
        direct = pair.COMMON.format(part=case['part'].removeprefix('{{target.color}} ')) + pair.DIRECT
        for shot in shots:
            if source_hashes[shot]['clip_sha256'] != FROZEN_SCREEN[inputs[0]['block']][shot]['clip_sha256']:
                raise ValueError(f'live candidate is not the archived Rev.1 clip: {shot}')
        with journal.open('x', encoding='utf-8') as log:
            for item in inputs:
                shot = item['shot']
                if policy == ARMS[0]:
                    schema = pair.SCHEMAS[pair.ARMS[0]]
                    stages = [('direct-first', item, direct, schema),
                              ('direct-repeat', item, direct, schema)]
                else:
                    stages = [
                        ('read', item['crop'], split.READ, split.READ_SCHEMA),
                        ('ownership', item['outlined'], split.OWNERSHIP.format(
                            part=case['part'].removeprefix('{{target.color}} ')),
                         split.OWNERSHIP_SCHEMA),
                    ]
                responses = []
                for seed, (stage, image, prompt, schema) in enumerate(stages, pair.OPTIONS['seed']):
                    if pair.sha(prepared / image['file']) != image['sha256']:
                        raise ValueError(f'prepared input changed: {image["file"]}')
                    value, failed = split.query(args.ollama, verifier['model'], prepared, log,
                                                item, policy, stage, image, prompt, schema, seed)
                    if failed:
                        raise RuntimeError(f'model call failed: {shot}/{item["panel"]}/{stage}; see {journal}')
                    responses.append(value)
                    shots[shot]['calls'].append(dict(
                        phase=stage, panel=item['panel'], image_sha256=image['sha256'],
                        seed=seed, parsed=value))
                if policy == ARMS[0]:
                    left, right = (response['verdict'] for response in responses)
                    vote = left if left == right else 'unknown'
                else:
                    vote = split.combine(*responses)
                shots[shot]['votes'].append(vote)
        for shot, row in shots.items():
            verdict = pair.aggregate(row['votes'])
            row.update(decision='fallback' if verdict == 'unknown' else verdict,
                       first='unknown', verification={'match': 'confirmed', 'distinct': 'rejected',
                                                      'unknown': 'unknown'}[verdict],
                       source_clip_sha256=source_hashes[shot]['clip_sha256'],
                       parse_failures=0, request_failures=0)
        return dict(revision=revision, started_s=started, finished_s=time.monotonic(), shots=shots)
    return observe


def validate_sources(args, api, strong, engine, cases):
    source = engine.load_json(args.source_run / 'source-report.json')
    for block in pair.CASES:
        if source[block]['screening_inputs'] != FROZEN_SCREEN[block]:
            raise ValueError(f'not the archived Table VI source: {block}')
        seed = args.source_run / block
        archive = strong.source_archive(args, api, source, block, cases[block])
        clips = {row['asset']: row['sha256'] for row in archive['assets']}
        for shot, hashes in source[block]['screening_inputs'].items():
            if clips[shot] != hashes['clip_sha256']:
                raise ValueError(f'source screen/clip mismatch: {block}/{shot}')
        db_uri = seed.joinpath('source.db').resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(db_uri, uri=True)) as db:
            latest = db.execute(
                'SELECT revision, entries FROM vf_timelines ORDER BY revision DESC LIMIT 1'
            ).fetchone()
        if (not latest or latest[0] != 1
                or api['fingerprint'](json.loads(latest[1])) != archive['timeline_hash']):
            raise ValueError(f'expected the archived Rev.1 source ledger: {block}')
        yield block, archive


def summary(results):
    output = {}
    for policy in ARMS:
        rows = [row['policies'].get(policy, {}) for row in results.values()]
        rows = [row for row in rows if row.get('status') == 'completed']
        finals = [row['first_publication'] for row in rows]
        output[policy] = dict(
            completed=len(rows), total=len(results),
            exact=sum(p['includes_all_targets'] and not p['excess'] for p in finals),
            coverage=sum(p['includes_all_targets'] for p in finals),
            excess=sum(len(p['excess']) for p in finals),
            provider_acceptances=sum(row['provider_acceptances'] for row in rows),
            median_edit_to_publish_s=median(p['edit_to_publish_s'] for p in finals) if finals else None)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', type=Path, default=Path('VideoFlow_Evidence_Gap'))
    parser.add_argument('--source-run', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--ollama', default='http://127.0.0.1:11434')
    parser.add_argument('--timeout', type=float, default=2400)
    parser.add_argument('--poll', type=float, default=2)
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    if args.timeout <= 0 or args.poll <= 0:
        parser.error('timeout and poll must be positive')
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        parser.error('FFmpeg and FFprobe are required')
    args.server, args.ollama = args.server.rstrip('/'), args.ollama.rstrip('/')
    package = args.engine.resolve()
    for name, digest in ENGINE_FILES.items():
        path = package / name
        if not path.is_file() or pair.sha(path) != digest:
            parser.error(f'expected the original Table VI package at --engine: {path}')
    sys.path.insert(0, str(package))
    import run_online as engine
    import run_strong_baseline as strong
    import run_weak_identity as weak

    api = engine.live_setup(package / 'runtime')
    write = api['write_json']
    cases = strong.cases()
    archives = dict(validate_sources(args, api, strong, engine, cases))
    verifier = engine.load_json(package / 'verifier_protocol.json')
    if not args.validate_only:
        identity = pair.model_info(args.ollama, verifier['model'])
        if identity['digest'].removeprefix('sha256:') != split.region.REFERENCE_DIGEST:
            parser.error('expected the Round 925 model digest')
        backend = api['ComfyUI'](args.server)
        api['check_backend'](backend)
    args.output.mkdir(parents=True)
    prepared = args.output / 'paired_inputs'
    prepared.mkdir()
    regions = engine.load_json(Path(__file__).with_name('marker_regions_925.json'))
    prep_start = time.monotonic()
    inputs = split.prepare(args.source_run, prepared, regions)
    write(args.output / 'protocol.json', dict(
        blocks=list(archives), arms=list(ARMS), engine_sha256=ENGINE_FILES, regions=regions,
        panel_numbers=[i+1 for i in pair.VIEWS], calls_per_arm_per_block=12,
        preparation_s=time.monotonic()-prep_start,
        timing='from edit to first publication; excludes preparing shared fixed crop views',
        model=None if args.validate_only else identity))
    write(prepared / 'index.json', inputs)
    print(f'Validated {len(archives)} archived seeds and {len(inputs)} paired inputs.', flush=True)
    if args.validate_only:
        return
    results = {block: dict(status='pending', policies={}) for block in archives}
    for index, (block, archive) in enumerate(archives.items()):
        seed = args.source_run / block
        root = args.output / block
        root.mkdir()
        shutil.copytree(seed / 'source_media', root / 'source_media')
        observer = observer_for(prepared, [item for item in inputs if item['block'] == block])
        order = ARMS if index % 2 == 0 else ARMS[::-1]
        row = results[block]
        row['arm_order'] = list(order)
        row['status'] = 'running'
        for policy in order:
            print(f'{block}: {policy}', flush=True)
            row['policies'][policy] = dict(status='running')
            write(args.output / 'report.json', results)
            try:
                api['check_backend'](backend)
                refs = api['ReferenceFrames'](root / (policy + '-references'), backend)
                arm = weak.run_policy(args, api, root / policy, seed / 'source.db',
                                      archive, cases[block], verifier, refs, backend, policy,
                                      observer=observer)
                if arm['vlm_calls'] != 12 or pair.model_info(args.ollama, verifier['model']) != identity:
                    raise RuntimeError('visual call budget or model identity changed')
            except (OSError, RuntimeError, ValueError) as exc:
                arm = dict(policy=policy, status='failed', error=str(exc))
                print(f'{block}/{policy}: {exc}', flush=True)
            row['policies'][policy] = arm
            write(root / policy / 'report.json', arm)
        row['status'] = ('completed' if all(a['status'] == 'completed' for a in row['policies'].values())
                         else 'partial')
        write(args.output / 'report.json', results)
        write(args.output / 'summary.json', summary(results))
    if any(row['status'] != 'completed' for row in results.values()):
        raise SystemExit('Incomplete pairs recorded in report.json.')
    print(json.dumps(summary(results), indent=2), flush=True)


FROZEN_SCREEN = {'amplifier-chain-92511': {'S1': {'clip_sha256': '2bde32b69e31df10c94694f2916981b5bff323d48566229a78b59c6e638795c1',
                                  'sheet_sha256': '1c9935a8998af75e1658008106d9a0d162548acb49c4599b613cb204199a77b9'},
                           'S2': {'clip_sha256': '1a435a07ab67eecc151c7b86b6c5c10cbb406566477c708fc984d35e93b57340',
                                  'sheet_sha256': 'd73ac6c0782f4c6a18378207a0572dc4c9e94e91719db53f32e44e7d24732a8a'},
                           'S3': {'clip_sha256': 'b7f0f4a8e89f8dba7244cbe8c6f54115bbcd0d00ddd1e8bfd210f89a6ac587fc',
                                  'sheet_sha256': 'fbcd9c9abc8efe8fa3e3b7486c59c2e83a1d29d95632aba391ea406b9a94ebbc'}},
 'camera-fanout-92522': {'S1': {'clip_sha256': '645724c1d03cde9d0babfc9283d94dbb3354b8713ba3a3faa8e5e40bf754a6e6',
                                'sheet_sha256': '30d81d8f1e4b45761c338da9c0da0a83ca1eae211defe3993bf38c58dc67c159'},
                         'S2': {'clip_sha256': 'd65b6b8d5643e0c9a2d25566728e15b8e301b9cc4f218c959a22520ab9a4d012',
                                'sheet_sha256': '0849859cf61f54b49928fed585ce71840c3dd82a04019f4cb0972548b57d8dd2'},
                         'S3': {'clip_sha256': '469bab4cd448359d95acc33f72efdaf7396ff0b4b0c266127a7a1583c5b3c322',
                                'sheet_sha256': 'fc80596a16f4e0a5e801b63c232b2825cc4e2fcf546d4c1e3a24e7c5b1e7558c'}},
 'radio-fanout-92542': {'S1': {'clip_sha256': '57d2b05f97b24ec5d0a0876846314bee8daf621e6d4e26cc49e91879cad60741',
                               'sheet_sha256': 'ab335356047eda89f0178b94c12b77366f28585ee03ca50465c1cf90fb71c984'},
                        'S2': {'clip_sha256': 'cdb8cda16d888564f8a6e1d4b3c96a637fad636d5c997b6e57c6c6c72b57b8de',
                               'sheet_sha256': '14d5cd7cad46163409d1a08b8fc2f43ff22315083d30d26c029b36a2793bda1a'},
                        'S3': {'clip_sha256': 'fc64eb15662f5a5cd4ec3ba776f5e71296a229422e57254a2e6aba724f7e0dc3',
                               'sheet_sha256': '6a17b228876149a3377a8fe3e630fbfc0cf6ce55325124cef391065c7c5b2095'}}}


if __name__ == "__main__":
    main()
