"""Prespecified six-seed H3 holdout: generate, screen, lock and evaluate."""

import argparse
import csv
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from zipfile import ZIP_DEFLATED, ZipFile

import run_marker_online as online

pair = online.pair
split = online.split
SHOTS = ('S1', *pair.CANDIDATES)
PANELS = tuple(i + 1 for i in pair.VIEWS)
SPECS = (
    ('same-decal', 94101, 'benchtop power supply', 'output voltage dial', 'blue triangle'),
    ('viewpoint', 94111, 'desktop headphone amplifier', 'gain knob', 'white diamond'),
    ('occlusion', 94121, 'compact audio mixer', 'master level knob', 'yellow circle'),
)


def cohort():
    cases = {}
    for group, start, host, part, decal in SPECS:
        for seed in (start, start + 1):
            cases[f'{group}-{seed}'] = dict(
                group=group, seed=seed, host=f'a charcoal {host} on a wooden workbench',
                part='{{target.color}} ' + part, initial='matte black', target_marker=decal,
                distractor_marker=decal if group == 'same-decal' else 'red hexagon',
                edges=[['S3', 'S4'], ['S4' if seed % 2 else 'S3', 'S6']],
                reference_motion={s: f'Hold on the same {host} and preserve the {part}.'
                                  for s in ('S4', 'S6')},
                independent='A static shot of folded cloth on an empty wooden workbench.')
    return cases


def manifest(engine, case):
    plan = engine.generated_manifest(case)
    for asset in plan['assets']:
        shot = asset['id']
        if shot not in SHOTS:
            continue
        same_decal = case['group'] == 'same-decal' and shot == 'S2'
        decal = case['distractor_marker'] if shot == 'S2' else case['target_marker']
        view = 'Locked frontal product shot.'
        if shot == 'S3' and case['group'] == 'viewpoint':
            view = 'Strong side three-quarter view, with both the part and the decal readable.'
        if shot != 'S1' and case['group'] == 'occlusion':
            view = ('Slow camera slide: a foreground cable partly hides the decal at first '
                    'and then clears it. Keep the part visible.')
        link = f'The device has one solid {decal} decal directly beside its control.'
        if same_decal:
            link = (f'The device with the {case["part"]} has no decal. Beside it sits a physically '
                    f'separate small black speaker, with the only {decal} decal. Keep both bodies '
                    'separated by a visible gap and the decal and control in view.')
        asset['spec']['template'] = f'{view} Show {case["host"]} with a {case["part"]}. {link}'
    return plan


FIELDS = ('block', 'include', 'reason', 'S1_xy', 'S2_xy', 'S3_xy')
RULES = (
    'Review only source sheets, before any VLM results. Keep every prespecified seed; generation '
    'failures and screen exclusions remain in the report. Include only when all three designated '
    'shots separately meet their prompt-defined identity and placement in at least two of panels '
    '1,2,4 and the designated challenge is visibly present (separate decal host in same-decal S2, '
    'side view in viewpoint S3, some partial obstruction in at least one occlusion candidate panel). '
    'Mark a single center per panel in its local 768x432 coordinates; for a hidden sticker mark its '
    'expected position on the body. Use x:y|x:y|x:y with panel order 1,2,4. On exclusion give a visual reason.'
)


def review_bundle(output, cases, reports):
    sheet = output / 'holdout-screen.csv'
    with sheet.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for block in cases:
            failed = reports.get(block, {}).get('status') != 'completed'
            w.writerow(dict(block=block, include='no' if failed else '',
                            reason='source generation failed' if failed else ''))
    rules = output / 'screening-rules.txt'
    rules.write_text(RULES + '\n')
    with ZipFile(output / 'holdout-review.zip', 'x', ZIP_DEFLATED) as z:
        for path in (sheet, rules, output / 'holdout-protocol.json', *sorted((output / 'screen').glob('*/*.png'))):
            z.write(path, path.relative_to(output))


def screened(path, source, cases):
    with path.open(newline='', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    if len(rows) != len(cases) or {r.get('block') for r in rows} != set(cases):
        raise ValueError('screen must contain exactly one row for every prespecified seed')
    regions, audit = {}, {}
    for row in rows:
        b = row['block']
        decision, reason = row['include'].strip().lower(), row['reason'].strip()
        if decision not in ('yes', 'no') or (decision == 'no' and not reason):
            raise ValueError(f'{b}: fill include (yes/no) and give a reason for exclusions')
        if source[b]['status'] != 'completed' and decision == 'yes':
            raise ValueError(f'{b}: cannot include a failed source')
        audit[b] = dict(status='eligible' if decision == 'yes' else 'excluded',
                        reason=reason, source_status=source[b]['status'], group=cases[b]['group'])
        if decision == 'yes':
            regions[b] = {}
            for shot in SHOTS:
                points = [tuple(map(int, p.split(':'))) for p in row[f'{shot}_xy'].strip().split('|')]
                if len(points) != len(PANELS) or any(
                    len(p) != 2 or not (0 <= p[0] < 768 and 0 <= p[1] < 432) for p in points
                ):
                    raise ValueError(f'{b}/{shot}: expected three in-frame x:y centers')
                regions[b][shot] = points
    return regions, audit


def cost(path):
    calls = [json.loads(line) for line in (path / 'vlm.jsonl').read_text().splitlines()]
    with online.closing(sqlite3.connect(path / 'project.db')) as db:
        jobs = db.execute(
            'SELECT asset, accepted_observed_at, completed_at FROM vf_jobs WHERE revision=2 ORDER BY asset'
        ).fetchall()
    return dict(vlm_http_wall_s=sum(c['elapsed_s'] for c in calls),
                provider_observed_jobs=[
                    dict(shot=shot, accepted_wall_s=start, committed_wall_s=end)
                    for shot, start, end in jobs])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('plan', 'source', 'edit'))
    parser.add_argument('--engine', type=Path, default=Path('VideoFlow_Evidence_Gap'))
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--source-run', type=Path)
    parser.add_argument('--screen', type=Path)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--ollama', default='http://127.0.0.1:11434')
    parser.add_argument('--timeout', type=float, default=2400)
    parser.add_argument('--poll', type=float, default=2)
    args = parser.parse_args()
    if args.timeout <= 0 or args.poll <= 0:
        parser.error('timeout and poll must be positive')
    if args.phase == 'edit' and not (args.source_run and args.screen):
        parser.error('edit requires --source-run and --screen')
    package = args.engine.resolve()
    for name, sha in online.ENGINE_FILES.items():
        if not (package / name).is_file() or pair.sha(package / name) != sha:
            parser.error(f'expected the original Table VI package: {package / name}')
    sys.path.insert(0, str(package))
    import run_online as engine
    import run_weak_identity as weak

    api = engine.live_setup(package / 'runtime')
    write = api['write_json']
    cases = cohort()
    manifests = {b: manifest(engine, case) for b, case in cases.items()}
    protocol = dict(cases=cases, manifests=manifests, panels=list(PANELS), screening=RULES,
                    arms=list(online.ARMS), calls_per_arm_per_seed=12,
                    prior_online_seeds_excluded=list(pair.CASES))
    if args.phase == 'plan':
        args.output.mkdir(parents=True)
        for m in manifests.values():
            with online.closing(api['AssetRuntime'](':memory:')) as runtime:
                runtime.initialize(m)
        write(args.output / 'holdout-protocol.json', protocol)
        print(f'Validated {len(cases)} frozen cases.')
        return

    backend = api['ComfyUI'](args.server.rstrip('/'))
    api['check_backend'](backend)
    args.ollama = args.ollama.rstrip('/')
    if args.phase == 'source':
        args.output.mkdir(parents=True)
        write(args.output / 'holdout-protocol.json', protocol)
        # Reuse the engine's source renderer with this experiment's frozen storyboard.
        original = weak.weak_manifest
        weak.weak_manifest = lambda case: manifest(engine, case)
        try:
            weak.stage_sources(args, api, cases, backend)
        finally:
            weak.weak_manifest = original
        review_bundle(args.output, cases, engine.load_json(args.output / 'source-report.json'))
        print(args.output / 'holdout-review.zip')
        return

    edit_stage_start = time.monotonic()
    if engine.load_json(args.source_run / 'holdout-protocol.json') != protocol:
        raise ValueError('source run protocol changed')
    source = engine.load_json(args.source_run / 'source-report.json')
    regions, audit = screened(args.screen, source, cases)
    verifier = engine.load_json(package / 'verifier_protocol.json')
    identity = pair.model_info(args.ollama, verifier['model'])
    if identity['digest'].removeprefix('sha256:') != split.region.REFERENCE_DIGEST:
        raise ValueError('expected the Round 925 model digest')
    args.output.mkdir(parents=True)
    shutil.copyfile(args.screen, args.output / 'locked-screen.csv')
    write(args.output / 'holdout-protocol.json', dict(protocol, model=identity))
    results = {b: dict(status='pending', policies={}) for b in regions}
    write(args.output / 'screen-audit.json', audit)
    write(args.output / 'report.json', results)
    if not regions:
        write(args.output / 'summary.json', dict(prespecified=len(cases), eligible=0, complete_pairs=0))
        print('No eligible seed; see screen-audit.json.')
        return
    prepared = args.output / 'paired_inputs'
    prepared.mkdir()
    prep_start = time.monotonic()
    items = split.prepare(args.source_run, prepared, regions, {b: dict(targets=['S3']) for b in regions})
    write(prepared / 'index.json', items)
    write(args.output / 'preparation.json', dict(shared_input_preparation_s=time.monotonic()-prep_start))
    hashes = {b: source[b]['screening_inputs'] for b in regions}
    for index, (block, case) in enumerate(cases.items()):
        if block not in regions:
            continue
        root = args.output / block
        seed = args.source_run / block
        if engine.load_json(seed / 'manifest.json') != manifests[block]:
            raise ValueError(f'changed source manifest: {block}')
        source_dir = seed / 'source_media'
        archive = engine.load_json(source_dir / 'revision-1.json')
        for asset in archive['assets']:
            if api['checksum'](source_dir / asset['file']) != asset['sha256']:
                raise ValueError(f'changed source clip: {block}/{asset["asset"]}')
        root.mkdir()
        shutil.copytree(source_dir, root / 'source_media')
        observe = online.observer_for(prepared, [i for i in items if i['block'] == block], hashes)
        order = online.ARMS if index % 2 == 0 else online.ARMS[::-1]
        row = results[block]
        row['arm_order'] = list(order)
        for arm in order:
            started = time.monotonic()
            try:
                api['check_backend'](backend)
                refs = api['ReferenceFrames'](root / (arm + '-references'), backend)
                result = weak.run_policy(args, api, root / arm, seed / 'source.db', archive,
                                         case, verifier, refs, backend, arm, observer=observe)
                if result['vlm_calls'] != 12 or pair.model_info(args.ollama, verifier['model']) != identity:
                    raise ValueError('call budget or model identity changed')
                result['cost'] = cost(root / arm)
            except (OSError, RuntimeError, ValueError) as exc:
                result = dict(status='failed', error=str(exc))
            result['attempt_wall_s'] = time.monotonic() - started
            row['policies'][arm] = result
            print(block, arm, result['status'], result.get('error', ''), flush=True)
            write(args.output / 'report.json', results)
        row['status'] = ('completed' if all(v['status'] == 'completed' for v in row['policies'].values())
                         else 'partial')
        write(args.output / 'report.json', results)
    paired = {b: row for b, row in results.items() if row['status'] == 'completed'}
    write(args.output / 'summary.json', dict(
        prespecified=len(cases), eligible=len(regions), complete_pairs=len(paired),
        paired=online.summary(paired), edit_stage_wall_s=time.monotonic()-edit_stage_start))
    if len(paired) != len(results):
        raise SystemExit('Incomplete pairs recorded in report.json.')


if __name__ == '__main__':
    main()
