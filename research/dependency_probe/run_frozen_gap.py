"""Render the audited 923 holdout selections with fixed, pre-acquired evidence."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from uuid import uuid4
from zipfile import ZipFile

from audit_gap_adjudication import audit
from VideoFlow_Evidence_Gap import run_frozen_backend as runner

ARMS = ('generic', 'gap-directed')


def load_runs(source, archive, api):
    audited = audit(archive)
    with ZipFile(archive) as bundle:
        (protocol_path,) = [name for name in bundle.namelist()
                            if name.rsplit('/', 1)[-1] == 'protocol.json']
        protocol = json.loads(bundle.read(protocol_path))
        report = json.loads(bundle.read(protocol_path.removesuffix('protocol.json') + 'report.json'))
    expected = {name for name, _ in runner.cases(runner.STORIES)}
    if len(expected) != 8 or any(name.split('-')[-1][:3] != '923' for name in expected):
        raise ValueError('use the Evidence Gap package with the frozen eight 923 seeds')
    if expected != set(audited['seeds']):
        raise ValueError('the replay archive must cover all eight 923 seeds')
    for name, block in protocol['blocks'].items():
        root = source / name
        database = root / 'source.db'
        case = runner.load_json(root / 'story.json')
        if case != block['record']['story']:
            raise ValueError(f'source story differs from replay: {name}')
        media = root / 'source_media'
        manifest = runner.load_json(media / 'revision-1.json')
        assets = {row['asset']: row for row in manifest['assets']}
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            published = db.execute('SELECT entries FROM vf_timelines WHERE revision=1').fetchone()
            if not published or api['fingerprint'](json.loads(published[0])) != manifest['timeline_hash']:
                raise ValueError(f'Rev.1 database does not match archive: {name}')
            if db.execute('SELECT MAX(revision) FROM vf_events').fetchone()[0] not in (None, 1):
                raise ValueError(f'source database has later edits: {name}')
        for shot, item in assets.items():
            if api['checksum'](media / item['file']) != item['sha256']:
                raise ValueError(f'archived source file has changed: {name}/{shot}')
            if shot in runner.CANDIDATES and block['record']['shots'][shot]['sha256'] != item['sha256']:
                raise ValueError(f'replay has different source bytes: {name}/{shot}')
        evidence = {}
        for arm in ARMS:
            shots = {}
            scored = audited['seeds'][name][0][arm]['original']
            for shot in runner.CANDIDATES:
                prior = block['record']['shots'][shot]
                verdict = scored['verdicts'][shot]
                decision = {'confirmed': 'match', 'rejected': 'distinct'}.get(verdict, prior['decision'])
                if (shot in scored['selected']) != (decision in ('match', 'fallback')):
                    raise ValueError(f'inconsistent selection: {name}/{shot}/{arm}')
                shots[shot] = dict(decision=decision, first=prior['first'], verification=verdict,
                                   replay_calls=report['trials'][name][shot][0][arm]['calls'],
                                   calls=[], parse_failures=0, request_failures=0)
            evidence[arm] = dict(revision=2, shots=shots, arm=arm, replay_round=1, rule='original')
        yield name, database, case, manifest, evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='existing 923 source run directory')
    parser.add_argument('--replay', type=Path, required=True, help='full 923 replay ZIP')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--timeout', type=float, default=2400)
    parser.add_argument('--poll', type=float, default=2)
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--resume', action='store_true', help='continue an existing output directory')
    parser.add_argument('--retry-unfinished', action='store_true',
                        help='archive the unfinished arm and start a new attempt')
    args = parser.parse_args()
    if args.timeout <= 0 or args.poll <= 0:
        parser.error('timeout and poll must be positive')
    if args.retry_unfinished and not args.resume:
        parser.error('--retry-unfinished requires --resume')
    args.server = args.server.rstrip('/')
    api = runner.live_setup(runner.ROOT / 'runtime')
    runs = list(load_runs(args.source, args.replay, api))
    print(f'Validated {len(runs)} paired seeds', flush=True)
    if args.validate_only:
        return

    write = api['write_json']
    backend = api['ComfyUI'](args.server)
    report = {
        'status': 'running',
        'replay_archive_sha256': api['checksum'](args.replay),
        'rule': 'original',
        'replay_round': 1,
        'evidence_timing': 'pre-acquired and applied before first dispatch',
        'seeds': {name: {'status': 'pending', 'arms': {}} for name, *_ in runs},
    }
    report_path = args.output / 'report.json'
    if args.resume:
        previous = runner.load_json(report_path)
        if any(previous[key] != report[key] for key in (
                'replay_archive_sha256', 'rule', 'replay_round', 'evidence_timing'
        )) or set(previous['seeds']) != set(report['seeds']):
            raise ValueError('existing report belongs to a different experiment')
        report = previous
        report['status'] = 'running'
    else:
        args.output.mkdir(parents=True)
    write(report_path, report)
    for index, (name, database, case, manifest, evidence) in enumerate(runs):
        result = report['seeds'][name]
        root = args.output / name
        root.mkdir(exist_ok=args.resume)
        order = ARMS if index % 2 == 0 else ARMS[::-1]
        try:
            for arm in order:
                if result['arms'].get(arm, {}).get('status') == 'completed':
                    continue
                print(f'{name} {arm}', flush=True)
                arm_root = root / arm
                arm_resume = args.resume and arm_root.is_dir()
                if arm_resume and args.retry_unfinished:
                    api['check_backend'](backend)
                    retry_archive = arm_root.with_name(f'{arm}-incomplete-{uuid4().hex}')
                    arm_root.rename(retry_archive)
                    result.setdefault('archived_attempts', {}).setdefault(arm, []).append(
                        retry_archive.relative_to(args.output).as_posix())
                    write(report_path, report)
                    arm_resume = False
                if not arm_resume:
                    api['check_backend'](backend)
                ref_root = root / (arm + '-references')
                if ref_root.exists():
                    ref_root = ref_root.with_name(f'{ref_root.name}-{uuid4().hex}')
                refs = api['ReferenceFrames'](ref_root, backend)
                result['arms'][arm] = runner.run_policy(
                    args, api, arm_root, database, manifest, case, None, refs, backend, 'egdr',
                    frozen_evidence=evidence[arm], resume=arm_resume)
                result['arms'][arm]['policy'] = arm
                attempts = result.get('archived_attempts', {}).get(arm)
                if attempts:
                    result['arms'][arm]['retry_of'] = attempts[-1]
                write(report_path, report)
            result['status'] = 'completed'
            result.pop('error', None)
        except Exception as exc:
            result.update(status='failed', error=str(exc))
            report['status'] = 'failed'
            write(report_path, report)
            raise
        write(report_path, report)
    report['status'] = 'completed'
    write(report_path, report)


if __name__ == '__main__':
    main()
