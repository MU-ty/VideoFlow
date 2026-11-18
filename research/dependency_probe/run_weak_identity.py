"""Run the frozen weak-identity holdout with separate source and edit phases."""
import argparse
import csv
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import random
import shutil
import subprocess
import sqlite3
import time
from zipfile import ZIP_STORED, ZipFile

from replay_gaps_3arm import ACTIONS
from replay_pairing import shot_prompt
from run_online import (
    CANDIDATES, KEY, ROOT, acquire, check, dependency_result, generated_manifest,
    live_setup, load_json, log, model_identity, stage_inputs, step,
)

POLICIES = ('declared-worklist', 'egdr-fixed', 'oracle')
ORDERS = (POLICIES, POLICIES[1:] + POLICIES[:1], POLICIES[2:] + POLICIES[:2],
          POLICIES[::-1])
STORIES = {
    'kettle-chain': dict(
        seeds=(92411, 92412), host='a gray electric kettle on a kitchen countertop',
        part='{{target.color}} lid release button', initial='matte black',
        target_marker='white circle', distractor_marker='white diamond',
        edges=[['S3', 'S4'], ['S4', 'S6']],
        reference_motion={
            'S4': 'Slow push toward the lid release button. Keep the kettle, its decal and button color intact.',
            'S6': 'Hold a closer view of the same lid release button and preserve its color.',
        },
        independent='A folded striped dish towel and a spoon on an otherwise empty countertop. No kettle.',
        weak_view='Film from a low oblique side angle so the tiny decal is foreshortened.',
    ),
    'sewing-machine-fanout': dict(
        seeds=(92421, 92422), host='an ivory sewing machine on a studio worktable',
        part='{{target.color}} stitch-selector dial', initial='charcoal gray',
        target_marker='blue circle', distractor_marker='blue hexagon',
        edges=[['S3', 'S4'], ['S3', 'S6']],
        reference_motion={
            'S4': 'Slow push toward the dial. Preserve the sewing machine, decal and dial color.',
            'S6': 'Slide gently to the right toward the same dial. Preserve its color.',
        },
        independent='A closed notebook and a short ruler on an empty worktable. No sewing machine.',
        weak_view='An out-of-focus foreground thread spool partly occludes the tiny decal during a slow slide.',
    ),
}
PANEL_COUNTS = tuple(str(n) for n in range(5))
SCREEN_FIELDS = ('block', 'shot', 'expected_marker', 'readable_linked_panels',
                 'part_visible', 'notes')


def frozen_cases():
    return {f'{name}-{seed}': dict(story, story=name, seed=seed)
            for name, story in STORIES.items() for seed in story['seeds']}


def weak_manifest(case):
    manifest = generated_manifest(case)
    for asset in manifest['assets']:
        shot = asset['id']
        if shot not in ('S1', *CANDIDATES):
            continue
        marker = case['distractor_marker'] if shot == 'S2' else case['target_marker']
        view = 'A clear frontal view.' if shot == 'S1' else case['weak_view']
        asset['spec']['template'] = (
            f"{view} Show {case['host']} with a {case['part']}. "
            f"A single small low-contrast {marker} decal sits on the body next to the part. "
            "The decal is legible in at least one moment. Keep the part visible. "
            "Show exactly one object of this type and preserve its design."
        )
    return manifest


def fixed_prompt(case):
    checklist = '\n'.join(f'- {instruction}' for instruction in ACTIONS.values())
    return shot_prompt(case) + '\nApply the relevant items in this fixed checklist:\n' + checklist


def fixed_image_reply(api, case, verifier):
    def reply(server, model, path, prompt, schema):
        if schema == verifier['structured_schema']:
            prompt = fixed_prompt(case)
        return api['image_reply'](server, model, path, prompt, schema)
    return reply


def oracle_evidence():
    now = time.monotonic()
    return dict(revision=2, source='frozen-oracle-labels', started_s=now, finished_s=now,
                shots={shot: dict(decision=decision, first='oracle', verification=verdict,
                                  calls=[], parse_failures=0, request_failures=0)
                       for shot, decision, verdict in (
                           ('S2', 'distinct', 'rejected'), ('S3', 'match', 'confirmed'))})


def run_policy(args, api, root, seed_db, source_archive, case, verifier, refs, backend, policy):
    root.mkdir()
    journal = root / 'events.jsonl'
    write = api['write_json']
    gated = policy != 'declared-worklist'
    method = 'dair' if gated else 'attr_dag'

    class Transport:
        base = backend.base

        def submit(self, workflow, prompt_id):
            result = backend.submit(workflow, prompt_id)
            log(journal, 'provider_acceptance', prompt_id=prompt_id)
            return result

        def inspect(self, prompt_id):
            result = backend.inspect(prompt_id)
            self.last_inspection = backend.last_inspection
            return result

    with closing(sqlite3.connect(seed_db)) as source_db, closing(sqlite3.connect(root / 'project.db')) as dest_db:
        source_db.backup(dest_db)
    with closing(api['AssetRuntime'](root / 'project.db')) as runtime:
        source_timeline = runtime.snapshot()['timeline']
        check(source_timeline and api['fingerprint'](source_timeline) == source_archive['timeline_hash'],
              'arm database does not match archived Rev.1')
        initial = {row['asset']: row for row in source_timeline}
        event = dict(id='edit-2', expected_revision=1, target='S1', key=KEY,
                     value='scarlet red', method=method)
        if gated:
            event['evidence_candidates'] = list(CANDIDATES)

        def observe():
            started = time.monotonic()
            frozen = root / 'frozen'
            stage_inputs(api, frozen, source_archive, root.parent / 'source_media')
            evidence = acquire(args, dict(api, image_reply=fixed_image_reply(api, case, verifier)),
                               frozen, verifier, case, 2, root / 'vlm.jsonl', 'egdr')
            evidence['started_with_frame_extraction_s'] = started
            return evidence

        with ThreadPoolExecutor(max_workers=1) as pool:
            deadline = time.monotonic() + args.timeout
            edit_at = time.time()
            plan = runtime.edit(event)
            log(journal, 'edit', revision=2, plan=plan)
            future = pool.submit(observe) if policy == 'egdr-fixed' else None
            evidence = None
            evidence_applied = None
            transport = Transport()
            while time.monotonic() < deadline:
                if evidence is None and policy == 'oracle':
                    evidence = oracle_evidence()
                elif evidence is None and future is not None and future.done():
                    evidence = future.result()
                if evidence is not None and evidence_applied is None:
                    write(root / 'evidence.json', evidence)
                    if policy == 'egdr-fixed':
                        records = load_json(root / 'frozen/index.json')
                    else:
                        records = {row['asset']: {'clip_sha256': row['sha256']}
                                   for row in source_archive['assets']}
                    for shot, item in evidence['shots'].items():
                        proof = api['fingerprint'](dict(revision=2, anchor='S1', key=KEY, shot=shot,
                            source_input_hash=initial[shot]['input_hash'],
                            source_clip_sha256=records[shot]['clip_sha256'], evidence=item))
                        result = runtime.resolve_evidence(2, shot, initial[shot]['input_hash'],
                                                          proof, item['decision'])
                        log(journal, 'evidence_applied', **result)
                    evidence_applied = time.time()

                step(api, runtime, transport, refs, journal)
                timeline = runtime.snapshot()['timeline']
                if timeline:
                    check(not gated or evidence is not None, 'published before resolving evidence')
                    break
                time.sleep(args.poll)
            else:
                raise TimeoutError('edit reached the publication deadline')

        published_at = runtime.db.execute(
            'SELECT published_at FROM vf_timelines WHERE revision=2').fetchone()[0]
        archive = api['archive_current'](runtime, root / 'media', backend.base, probe=True)
        final = {row['asset']: row for row in archive['assets']}
        jobs = {(j['asset'], j['revision']): j for j in runtime.snapshot()['jobs']}
        expected = {'S1', 'S3', *(child for _, child in case['edges'])}
        regenerated = {shot for shot, row in final.items()
                       if row['input_hash'] != initial[shot]['input_hash']}
        initial_bytes = {row['asset']: row['sha256'] for row in source_archive['assets']}
        for parent, _ in case['edges']:
            refs.stage(final[parent]['output'])
        lineage = api['reference_evidence']([final], jobs, refs, case['edges'])
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        return dict(policy=policy, status='completed', expected=sorted(expected),
                    dependency_discovery=dependency_result(evidence['shots']) if evidence else None,
                    first_publication=dict(revision=2, regenerated=sorted(regenerated),
                        includes_all_targets=expected <= regenerated,
                        missing_targets=sorted(expected - regenerated),
                        excess=sorted(regenerated - expected),
                        byte_identical_to_source=sorted(shot for shot, row in final.items()
                            if row['sha256'] == initial_bytes[shot]),
                        reference_lineage=lineage,
                        edit_to_publish_s=published_at - edit_at),
                    initial_plan=plan['regenerate'],
                    evidence_acquisition_s=(evidence['finished_s'] - evidence['started_with_frame_extraction_s']
                                            if policy == 'egdr-fixed' else None),
                    edit_to_evidence_applied_s=evidence_applied - edit_at if evidence else None,
                    vlm_calls=sum(len(item['calls']) for item in evidence['shots'].values()) if evidence else 0,
                    vlm_parse_failures=sum(item['parse_failures'] for item in evidence['shots'].values()) if evidence else 0,
                    vlm_request_failures=sum(item['request_failures'] for item in evidence['shots'].values()) if evidence else 0,
                    provider_acceptances=sum(e['action'] == 'provider_acceptance' for e in events))


def write_screen_template(path, rows):
    with path.open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.DictWriter(file, fieldnames=SCREEN_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def stage_sources(args, api, cases, backend):
    write = api['write_json']
    report = {}
    rows = []
    for block, case in cases.items():
        root = args.output / block
        root.mkdir()
        manifest = weak_manifest(case)
        write(root / 'story.json', case)
        write(root / 'manifest.json', manifest)
        report[block] = dict(status='running')
        write(args.output / 'source-report.json', report)
        try:
            api['check_backend'](backend)
            print(f'{block}: generating shared Rev.1', flush=True)
            refs = api['ReferenceFrames'](root / 'source_references', backend)
            with closing(api['AssetRuntime'](root / 'source.db')) as runtime:
                runtime.initialize(manifest)
                deadline = time.monotonic() + args.timeout
                while time.monotonic() < deadline:
                    step(api, runtime, backend, refs, root / 'source_events.jsonl')
                    if runtime.snapshot()['timeline']:
                        break
                    time.sleep(args.poll)
                else:
                    raise TimeoutError('source did not publish before the deadline')
                archive = api['archive_current'](runtime, root / 'source_media',
                                                 backend.base, probe=True)
            screen_dir = args.output / 'screen' / block
            screen_dir.mkdir(parents=True)
            assets = {row['asset']: row for row in archive['assets']}
            screened = {}
            for shot in ('S1', *CANDIDATES):
                sheet = screen_dir / f'{shot}.png'
                result = api['make_sheet'](root / 'source_media' / assets[shot]['file'], sheet)
                check(result['clip_sha256'] == assets[shot]['sha256'], 'source sheet used wrong video')
                screened[shot] = dict(clip_sha256=result['clip_sha256'],
                                      sheet_sha256=api['checksum'](sheet))
                marker = case['distractor_marker'] if shot == 'S2' else case['target_marker']
                rows.append(dict(block=block, shot=shot, expected_marker=marker))
            report[block] = dict(status='completed', screening_inputs=screened)
        except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
            report[block] = dict(status='failed', error=str(exc))
            print(f'{block}: source failed: {exc}', flush=True)
        write(args.output / 'source-report.json', report)
    write_screen_template(args.output / 'source-screen.csv', rows)


def read_screen(path, source_report, cases):
    items = {}
    with path.open(newline='', encoding='utf-8-sig') as file:
        reader = csv.DictReader(file)
        check(tuple(reader.fieldnames or ()) == SCREEN_FIELDS, 'use the generated source screen template')
        for row in reader:
            block, shot = row['block'], row['shot']
            check(block in cases and shot in ('S1', *CANDIDATES), 'unexpected source screen row')
            if source_report[block]['status'] != 'completed':
                continue
            key = (block, shot)
            check(key not in items, f'duplicate source screening row {key}')
            case = cases[block]
            marker = case['distractor_marker'] if shot == 'S2' else case['target_marker']
            check(row['expected_marker'] == marker, f'changed source label {key}')
            count = (row['readable_linked_panels'] or '').strip()
            visible = (row['part_visible'] or '').strip().lower()
            check(count in PANEL_COUNTS and visible in ('yes', 'no', 'unclear'),
                  f'fill source screen for {block}/{shot}')
            check(count == '0' or visible == 'yes', f'linked panels require a visible part: {key}')
            items[key] = dict(panels=int(count), part_visible=visible, notes=row['notes'])
    decisions = {}
    for block in cases:
        if source_report[block]['status'] != 'completed':
            decisions[block] = dict(status='source-failed', error=source_report[block].get('error'))
            continue
        check(all((block, shot) in items for shot in ('S1', *CANDIDATES)),
              f'incomplete source screening for {block}')
        shots = {shot: items[block, shot] for shot in ('S1', *CANDIDATES)}
        readable = all(row['panels'] >= 1 and row['part_visible'] == 'yes' for row in shots.values())
        weak = any(shots[shot]['panels'] < 4 for shot in CANDIDATES)
        reason = None if readable and weak else ('insufficient readable evidence' if not readable
                                                 else 'no ambiguous candidate panel')
        decisions[block] = dict(status='eligible' if reason is None else 'excluded',
                                reason=reason, screening=shots)
    return decisions


def stage_edits(args, api, cases, backend):
    original = args.source_run
    source_report = load_json(original / 'source-report.json')
    check(set(source_report) == set(cases), 'source run does not contain all four prespecified seeds')
    protocol = load_json(original / 'protocol.json')
    check(protocol == load_json(args.output / 'protocol.json'), 'source run protocol has changed')
    results = read_screen(args.screen, source_report, cases)
    write = api['write_json']
    shutil.copyfile(args.screen, args.output / 'locked-source-screen.csv')
    write(args.output / 'report.json', results)
    if not any(row['status'] == 'eligible' for row in results.values()):
        print('No source met eligibility. All prespecified seeds are retained in report.json.')
        return
    model = model_identity(args, protocol['verifier']['model'])
    write(args.output / 'model.json', model)
    for index, (block, case) in enumerate(cases.items()):
        result = results[block]
        if result['status'] != 'eligible':
            continue
        seed = original / block
        check(load_json(seed / 'manifest.json') == weak_manifest(case), f'source manifest changed: {block}')
        archive = load_json(seed / 'source_media' / 'revision-1.json')
        for row in archive['assets']:
            check(api['checksum'](seed / 'source_media' / row['file']) == row['sha256'],
                  f'changed source video: {block}/{row["asset"]}')
        for shot, hashes in source_report[block]['screening_inputs'].items():
            check(api['checksum'](original / 'screen' / block / f'{shot}.png') == hashes['sheet_sha256'],
                  f'changed screening sheet: {block}/{shot}')
        root = args.output / block
        root.mkdir()
        write(root / 'story.json', case)
        shutil.copytree(seed / 'source_media', root / 'source_media')
        order = ORDERS[index]
        result.update(status='running', arm_order=list(order), policies=[])
        for policy in order:
            result['active_policy'] = policy
            write(args.output / 'report.json', results)
            try:
                api['check_backend'](backend)
                refs = api['ReferenceFrames'](root / (policy + '-references'), backend)
                print(f'{block}: running {policy}', flush=True)
                run = run_policy(args, api, root / policy, seed / 'source.db', archive,
                                 case, protocol['verifier'], refs, backend, policy)
                if policy == 'egdr-fixed':
                    check(model_identity(args, model['model']) == model,
                          'Ollama model or server version changed mid-experiment')
                    check(run['vlm_calls'] == 6, 'incorrect EGDR visual call budget')
                if policy == 'oracle':
                    check(run['first_publication']['includes_all_targets'] and
                          not run['first_publication']['excess'],
                          'oracle graph closure did not match the frozen labels')
                write(root / policy / 'report.json', run)
                result['policies'].append(run)
            except (RuntimeError, OSError, ValueError) as exc:
                result['policies'].append(dict(policy=policy, status='failed', error=str(exc)))
                print(f'{block}/{policy}: {exc}', flush=True)
        result.pop('active_policy')
        result['status'] = ('completed' if all(x['status'] == 'completed' for x in result['policies'])
                            else 'partial')
        write(args.output / 'report.json', results)



def make_blind(args, api):
    run = args.source_run
    results = load_json(run / 'report.json')
    items = []
    for block, result in results.items():
        directory = run / block
        for arm in result.get('policies', []):
            if arm['status'] == 'completed':
                media = directory / arm['policy'] / 'media'
                for row in load_json(media / 'revision-2.json')['assets']:
                    items.append((block, arm['policy'], media, row))
    check(items, 'no completed publications to export')
    random.SystemRandom().shuffle(items)
    review = args.output / 'reviewer'
    clips = review / 'outputs'
    clips.mkdir(parents=True)
    key = {}
    copied = set()

    def export(directory, row, name):
        src = directory / row['file']
        check(api['checksum'](src) == row['sha256'], f'changed video: {src}')
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(src), '-map', '0:v:0',
                        '-c:v', 'copy', '-map_metadata', '-1', '-map_chapters', '-1',
                        str(review / name)], check=True, timeout=90)

    with (review / 'ratings.csv').open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.writer(file)
        writer.writerow(['clip_id', 'source_file', 'part', 'expected_appearance', 'strict_recolor',
                         'identity_preserved', 'temporal_consistency', 'unintended_change', 'notes'])
        for index, (block, policy, media, row) in enumerate(items, 1):
            shot = row['asset']
            clip = f'V{index:03d}'
            export(media, row, Path('outputs') / f'{clip}.mp4')
            source = Path('source') / block / f'{shot}.mp4'
            if source not in copied:
                folder = run / block / 'source_media'
                original = next(x for x in load_json(folder / 'revision-1.json')['assets']
                                if x['asset'] == shot)
                (review / source).parent.mkdir(parents=True, exist_ok=True)
                export(folder, original, source)
                copied.add(source)
            target = shot in ('S1', 'S3', 'S4', 'S6')
            case = load_json(run / block / 'story.json')
            part = case['part'].replace('{{target.color}} ', '') if shot != 'S5' else ''
            writer.writerow([clip, source.as_posix(), part, 'scarlet red' if target else 'unchanged',
                             '', '', '', '', ''])
            key[clip] = dict(block=block, policy=policy, shot=shot, target=target)
    api['write_json'](args.output / 'KEEP_PRIVATE_key.json', key)
    (review / 'REVIEW.txt').write_text(
        'Review the full output against its source. Score independently and return the original CSV.\n'
        'For strict_recolor, use yes if the target part is consistently scarlet red whenever visible, '
        'no if the condition fails, unclear if it cannot be judged, and NA if expected unchanged.\n'
        'Score identity_preserved and temporal_consistency as yes/no/unclear. '
        'Use temporal_consistency=no for visible flicker, color reversion or identity drift.\n'
        'For unintended_change, use yes for material appearance changes outside the edited part, '
        'including changes in non-target shots. Otherwise use no or unclear and explain.\n',
        encoding='utf-8')
    with ZipFile(args.output / 'send_to_reviewers.zip', 'w', ZIP_STORED) as zipfile:
        for file in sorted(review.rglob('*')):
            if file.is_file():
                zipfile.write(file, file.relative_to(review))
    print(f'{len(items)} output clips exported with randomized IDs.')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('prepare', 'source', 'edit', 'blind'), required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--source-run', type=Path)
    parser.add_argument('--screen', type=Path)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--ollama', default='http://127.0.0.1:11434')
    parser.add_argument('--timeout', type=float, default=2400)
    parser.add_argument('--poll', type=float, default=2)
    args = parser.parse_args()
    if args.mode == 'edit' and not (args.source_run and args.screen):
        parser.error('edit requires --source-run and --screen')
    if args.mode == 'blind' and args.source_run is None:
        parser.error('blind requires --source-run pointing to the edit run')
    if args.timeout <= 0 or args.poll <= 0:
        parser.error('timeout and poll must be positive')
    args.server = args.server.rstrip('/')
    args.ollama = args.ollama.rstrip('/')
    cases = frozen_cases()
    api = live_setup(ROOT / 'runtime')
    args.output.mkdir(parents=True)
    if args.mode == 'blind':
        make_blind(args, api)
        return
    verifier = load_json(ROOT / 'verifier_protocol.json')
    protocol = dict(cases=cases, policies=list(POLICIES), orders=ORDERS, verifier=verifier,
                    prompts={block: fixed_prompt(case) for block, case in cases.items()},
                    binary_source_eligibility=('S1/S2/S3 each have at least one readable linked panel; '
                                               'S2 or S3 has at least one unreadable panel'),
                    source_sample_fractions=[0.10, 0.35, 0.60, 0.85],
                    ground_truth=dict(S2='distinct', S3='match', targets=['S1', 'S3', 'S4', 'S6']),
                    edit='scarlet red',
                    references='prompt-only S1/S3 and automatic upstream first frames for S4/S6')
    api['write_json'](args.output / 'protocol.json', protocol)
    if args.mode == 'prepare':
        for block, case in cases.items():
            manifest = weak_manifest(case)
            with closing(api['AssetRuntime'](':memory:')) as runtime:
                runtime.initialize(manifest)
            api['write_json'](args.output / f'{block}.json', manifest)
        print(f'{len(cases)} frozen manifests validated. No service needed.')
        return
    check(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg and ffprobe are required')
    backend = api['ComfyUI'](args.server)
    api['check_backend'](backend)
    if args.mode == 'source':
        stage_sources(args, api, cases, backend)
    else:
        stage_edits(args, api, cases, backend)


if __name__ == '__main__':
    main()
