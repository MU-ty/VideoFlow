"""Run late EGDR evidence and masked H3 status against the actual worker."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import subprocess
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
CANDIDATES = ('S2', 'S3')
KEY = 'target.color'
STORIES = json.loads((ROOT / 'stories.json').read_text())
POLICIES = ('declared-worklist', 'ungated-worklist', 'egdr', 'gated-worklist', 'full')
ORDERS = (
    POLICIES,
    ('egdr', 'gated-worklist', 'full', 'declared-worklist', 'ungated-worklist'),
    ('full', 'ungated-worklist', 'gated-worklist', 'egdr', 'declared-worklist'),
)


def cli():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', default=ROOT / 'runtime', type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--server', default='http://127.0.0.1:8188')
    p.add_argument('--ollama', default='http://127.0.0.1:11434')
    p.add_argument('--timeout', type=float, default=2400)
    p.add_argument('--poll', type=float, default=2)
    p.add_argument('--policy', nargs='+', choices=POLICIES,
                   default=['ungated-worklist', 'egdr', 'gated-worklist', 'full'])
    p.add_argument('--stories', nargs='+', choices=STORIES, default=list(STORIES))
    p.add_argument('--hold', type=float, default=12, help='minimum exposure after anchor commit in seconds')
    p.add_argument('--reuse-source-from', type=Path, help='copy completed Rev.1 blocks from a previous run')
    p.add_argument('--prepare-only', action='store_true')
    args = p.parse_args()
    if args.poll <= 0 or args.timeout <= 0 or len(set(args.policy)) != len(args.policy):
        p.error('poll and timeout must be positive and policies must be unique')
    if args.hold <= 0 or len(set(args.stories)) != len(args.stories):
        p.error('hold must be positive and stories must be distinct')
    return args


def load_json(path):
    return json.loads(path.read_text())


def log(path, action, **fields):
    with path.open('a', encoding='utf-8') as stream:
        print(json.dumps(dict(action=action, wall_s=time.time(), monotonic_s=time.monotonic(), **fields)),
              file=stream)


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def stage_inputs(api, root, archive):
    root.mkdir()
    records = {}
    for row in archive['assets']:
        shot = row['asset']
        if shot not in CANDIDATES:
            continue
        sheet = root / f'{shot}.png'
        records[shot] = api['make_sheet'](root.parent / 'source_media' / row['file'], sheet)
        check(records[shot]['clip_sha256'] == row['sha256'], 'VLM source does not match Rev.1')
        for pair, y in [('1-2', 0), ('3-4', 432)]:
            pair_path = root / f'{shot}-{pair}.png'
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(sheet),
                            '-vf', f'crop=1536:432:0:{y}', '-frames:v', '1', '-threads', '1',
                            str(pair_path)], check=True, timeout=60)
            records[shot][pair] = api['checksum'](pair_path)
    api['write_json'](root / 'index.json', records)


def live_setup(repo):
    sys.path[:0] = [str(repo), str(repo / 'research'), str(repo / 'research' / 'dependency_probe')]
    from artifacts import archive_current, checksum, trusted_download, write_json
    from asset_runtime import AssetRuntime, Conflict, fingerprint
    from asset_worker import pass_once
    from comfyui import ComfyUI
    from provider_audit import summarize_inspections
    from recovery_matrix import SubmissionView
    from recovery_reference_comfy import ReferenceFrames, check_backend, reference_evidence, upload_frame
    from vision_probe import image_reply, make_sheet, parse_visual, visual_reply
    return locals()


def get(url):
    with urlopen(url, timeout=15) as response:
        return json.load(response)


def model_identity(args, model):
    found = [m['digest'] for m in get(args.ollama + '/api/tags')['models'] if m['name'] == model]
    check(len(found) == 1 and found[0], f'model unavailable: {model}')
    return dict(model=model, digest=found[0], version=get(args.ollama + '/api/version')['version'])


def structured_state(panels, case):
    def linked(panel, marker):
        color, shape = marker.split()
        return (panel['host_visible'], panel['part_visible'], panel['same_instance'],
                panel['shape'], panel['color']) == ('yes', 'yes', 'yes', shape, color)

    if any(linked(panel, case['target_marker']) for panel in panels):
        return 'confirmed'
    if (len(panels) == 4 and any(linked(p, case['distractor_marker']) for p in panels)
            and all(p['complete'] is True and
                    (linked(p, case['distractor_marker']) or p['host_visible'] == 'no') for p in panels)):
        return 'rejected'
    return 'unknown'


def acquire(args, api, frozen, verifier, case, revision, log_path):
    started = time.monotonic()
    model = verifier['model']
    results = {}
    part = case['part'].replace('{{target.color}} ', '')
    question = (f"Does any source panel show the {part} on {case['host']} with a visible "
                f"{case['target_marker']} on the same object? Exclude the {case['distractor_marker']} lookalike.")
    for shot in CANDIDATES:
        image = frozen / f'{shot}.png'
        calls = []
        def call(phase, path, run, parse):
            for attempt in (1, 2):
                start = time.monotonic()
                raw = run(attempt)
                row = dict(phase=phase, attempt=attempt, raw=raw,
                           image_sha256=api['checksum'](path),
                           started_s=start, finished_s=time.monotonic())
                try:
                    parsed = parse(raw)
                except (ValueError, KeyError, TypeError) as exc:
                    row['parse_error'] = str(exc)
                    calls.append(row)
                    log(log_path, 'vlm_invalid', revision=revision, shot=shot, **row)
                else:
                    calls.append(row)
                    log(log_path, 'vlm_reply', revision=revision, shot=shot, **row)
                    return parsed
            return None

        discovery = call('discover', image, lambda attempt: api['visual_reply'](
            args.ollama, model, f'Recolor the {part} scarlet red.', image,
            visibility_question=question if attempt == 1 else question +
            ' If the marker is ambiguous, answer unknown with a null panel.', format_retry=True), api['parse_visual'])
        first = discovery['decision'] if discovery else 'unknown'
        state = 'skipped'
        if first != 'no':
            panels = []
            for pair in ('1-2', '3-4'):
                image = frozen / f'{shot}-{pair}.png'
                prompt = (f'For each of two panels (1 left, 2 right), inspect {case["host"]}. '
                          f'Report visibility of the host and its {part}. Record the shape and color '
                          'of the sticker on the body, ignoring the shape or color of the edited part. '
                          'Set same_instance=yes only when both are on one object. Set complete=true '
                          'only when the host and its marker can be evaluated or the host is clearly absent. '
                          'Use unknown for obscured or ambiguous fields.')
                schema = verifier['structured_schema']
                def parse(raw):
                    rows = json.loads(raw)['panels']
                    if not isinstance(rows, list) or len(rows) != 2:
                        raise ValueError('expected two panel records')
                    fields = schema['properties']['panels']['items']['properties']
                    for row in rows:
                        if not isinstance(row, dict) or set(row) != set(fields):
                            raise ValueError('unexpected panel fields')
                        for key, spec in fields.items():
                            value = row[key]
                            if ('enum' in spec and value not in spec['enum']
                                    or key == 'panel' and type(value) is not int
                                    or key == 'complete' and type(value) is not bool):
                                raise ValueError(f'invalid panel field: {key}')
                    if {row['panel'] for row in rows} != {1, 2}:
                        raise ValueError('missing panel 1 or 2')
                    return rows
                parsed = call('verify-' + pair, image, lambda attempt: api['image_reply'](
                    args.ollama, model, image,
                    prompt if attempt == 1 else prompt + ' Return exactly one schema record for each panel.',
                    schema), parse)
                if parsed is not None:
                    panels.extend(parsed)
            state = structured_state(panels, case)
        decision = ('distinct' if state in ('rejected', 'skipped') else
                    'match' if state == 'confirmed' or first == 'yes' else 'fallback')
        results[shot] = dict(decision=decision, first=first, structured=state, calls=calls,
                             parse_failures=sum('parse_error' in row for row in calls))
    return dict(revision=revision, started_s=started, finished_s=time.monotonic(), shots=results)


def generated_manifest(case):
    assets = []
    for i, shot in enumerate(('S1', 'S2', 'S3', 'S4', 'S5', 'S6')):
        values = {}
        if shot in ('S1', 'S2', 'S3'):
            edited = shot == 'S1' or shot in case.get('expected', ('S3',))
            marker = case['target_marker'] if edited else case['distractor_marker']
            angle = 'Front three-quarter view' if shot != 'S3' else 'Tight side three-quarter view'
            prompt = (f"{angle} of {case['host']} with a {case['part']}. "
                      f"Clearly show one large solid {marker} sticker fixed to the front body "
                      "beside the edited part. Keep the sticker and edited part visible and unchanged "
                      "throughout the shot. Only one object of this type is present.")
            values = {KEY: case['initial']}
        elif shot in case['reference_motion']:
            prompt = 'Start from the supplied input frame. ' + case['reference_motion'][shot]
        else:
            prompt = case['independent']
        assets.append(dict(id=shot, scene=shot, kind='video',
                           spec=dict(template=prompt, reads=list(values), values=values,
                                     cost=1, config=dict(seed=case['seed'] + i))))
    return dict(assets=assets, edges=[dict(parent=p, child=c, kind='reference')
                                     for p, c in case['edges']])


def run_policy(args, api, root, seed_db, frozen, case, verifier, refs, backend, policy):
    root.mkdir()
    journal = root / 'events.jsonl'
    write = api['write_json']
    class Runtime(api['AssetRuntime']):
        @staticmethod
        def _plan(assets, specs, edges, event):
            selected_method = ('full' if policy == 'full' and event['id'] == 'edit-3'
                               else 'dair' if policy == 'egdr' else 'attr_dag')
            return api['AssetRuntime']._plan(assets, specs, edges, dict(event, method=selected_method))

        @staticmethod
        def _state_changes(assets, specs, edges, event):
            changed = api['AssetRuntime']._state_changes(assets, specs, edges, event)
            # Full uses the protocol's known target for red conditioning, without reading VLM verdicts.
            return changed | {'S3'} if policy == 'full' and event['id'] == 'edit-3' else changed

        def _publication_pending(self, revision):
            return (policy in ('egdr', 'gated-worklist')
                    and super()._publication_pending(revision))

    class Transport:
        base = backend.base
        last_inspection = None
        masked = None

        def submit(self, workflow, prompt_id):
            result = backend.submit(workflow, prompt_id)
            log(journal, 'provider_acceptance', prompt_id=prompt_id)
            return result

        def inspect(self, prompt_id):
            self.last_inspection = None
            if prompt_id == self.masked:
                return 'unknown', None
            result = backend.inspect(prompt_id)
            self.last_inspection = backend.last_inspection
            return result

    transport = Transport()
    db = root / 'project.db'
    with closing(sqlite3.connect(seed_db)) as source_db, closing(sqlite3.connect(db)) as dest_db:
        source_db.backup(dest_db)
    runtime = Runtime(db)
    initial = {row['asset']: row for row in runtime.snapshot()['timeline']}
    try:
        def step(cap):
            events = api['pass_once'](runtime, transport, build=refs.build, max_submit=0)
            free = min(cap, max(0, 2 - len(runtime.unresolved_remote())))
            for job in runtime.pending()[:free]:
                for dep in runtime._inputs(job['asset'])['dependencies']:
                    if dep['kind'] == 'reference':
                        refs.stage(dep['output'])
            if free:
                events += api['pass_once'](api['SubmissionView'](runtime), transport, refs.build, max_submit=free)
            log(journal, 'worker', events=events)
            check(not any(e['action'] in ('failed','rejected','validation_error') for e in events),
                  f'worker failure: {events}')
            return events

        def edit(revision, value):
            event = dict(id=f'edit-{revision}', expected_revision=revision-1, target='S1',
                         key=KEY, value=value)
            if policy in ('egdr', 'gated-worklist', 'ungated-worklist'):
                event['evidence_candidates'] = list(CANDIDATES)
            result = runtime.edit(event)
            log(journal, 'edit', revision=revision, value=value, plan=result)
            expected = list(initial) if policy == 'full' and revision == 3 else ['S1']
            check(result['regenerate'] == expected, 'unexpected initial plan')

        def check_rejection(revision, shot, source_hash, proof, decision):
            try:
                runtime.resolve_evidence(revision, shot, source_hash, proof, decision)
            except api['Conflict'] as exc:
                log(journal, 'evidence_rejected', revision=revision, shot=shot, reason=str(exc))
            else:
                raise RuntimeError('invalid evidence passed the publication gate')

        started = time.monotonic()
        deadline = started + args.timeout
        edit(2, 'cobalt blue')
        step(1)
        old = next(j for j in runtime.active() if j['asset'] == 'S1' and j['revision'] == 2)
        check(old['status'] == 'submitted', 'old anchor was not accepted')
        transport.masked = old['external_id']
        check(any(e['action'] == 'unknown_no_resubmit' for e in step(0)), 'mask was not observed')
        edit(3, 'scarlet red')
        runtime.close()
        runtime = Runtime(db)
        log(journal, 'worker_reopened', revision=3)
        step(1)
        check(any(j['asset'] == 'S1' and j['revision'] == 3 and j['status'] == 'submitted'
                  for j in runtime.active()), 'current anchor was not accepted')

        evidence_path = args.output / 'shared_evidence.json'
        stale = False
        ready = None
        hold_until = None

        def watch(cap):
            nonlocal stale
            stale |= any(e.get('result') == 'stale' and e['job'] == old['id'] for e in step(cap))

        # Keep the old task opaque until the current anchor has been accepted.
        transport.masked = None
        log(journal, 'mask_revealed', prompt_id=old['external_id'])
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = None if evidence_path.exists() else pool.submit(
                acquire, args, api, frozen, verifier, case, 3, args.output / 'shared_vlm.jsonl')
            while time.monotonic() < deadline:
                watch(2)
                snapshot = runtime.snapshot()
                anchor = next(j for j in snapshot['jobs'] if j['asset']=='S1' and j['revision']==3)
                if ready is None and anchor['status'] == 'succeeded':
                    ready = anchor['completed_at']
                    hold_until = time.monotonic() + args.hold
                    log(journal, 'anchor_ready', all_outputs=all(a['output'] for a in snapshot['assets']),
                        manifest_exists=snapshot['timeline'] is not None)
                if (ready is not None and time.monotonic() >= hold_until
                        and (future is None or future.done())):
                    break
                time.sleep(args.poll)
            else:
                raise TimeoutError('anchor/evidence barrier did not complete')
            if future is not None:
                write(evidence_path, future.result())
        current = load_json(evidence_path)
        write(root / 'evidence.json', current)
        gated = policy in ('egdr', 'gated-worklist')
        if policy != 'full':
            snapshot = runtime.snapshot()
            check(all(a['output'] for a in snapshot['assets']), 'other outputs missing at gate challenge')
            check((snapshot['timeline'] is None) == gated, 'unexpected pre-evidence publication')
        def proof(shot, item):
            return api['fingerprint'](dict(revision=3, anchor='S1', key=KEY, shot=shot,
                source_input_hash=initial[shot]['input_hash'],
                source_clip_sha256=load_json(frozen / 'index.json')[shot]['clip_sha256'], evidence=item))

        # Replay an explicitly injected stale positive, separate from the live VLM verdict.
        check_rejection(2, 'S3', initial['S3']['input_hash'],
                        api['fingerprint'](dict(revision=2, shot='S3', injected='match')), 'match')
        if policy in ('egdr', 'gated-worklist', 'ungated-worklist'):
            check_rejection(3, 'S3', '0' * 64, proof('S3', current['shots']['S3']), 'match')
        if gated:
            check(runtime.snapshot()['timeline'] is None, 'stale verdict opened publication gate')
        release = time.time()
        log(journal, 'evidence_released')
        if policy in ('egdr', 'gated-worklist', 'ungated-worklist'):
            for shot, item in current['shots'].items():
                result = runtime.resolve_evidence(3, shot, initial[shot]['input_hash'],
                                                  proof(shot, item), item['decision'])
                log(journal, 'evidence_applied', **result)
        while time.monotonic() < deadline:
            watch(2)
            snapshot = runtime.snapshot()
            if snapshot['timeline'] and all(a['output'] for a in snapshot['assets']) and not runtime.unresolved_remote():
                break
            time.sleep(args.poll)
        else:
            raise TimeoutError('gate run did not publish and drain')
        end_wall = runtime.db.execute('SELECT published_at FROM vf_timelines WHERE revision=3').fetchone()[0]
        # Use SQLite's publication instant, before audit, archive, or provider drain.
        edits = dict(runtime.db.execute('SELECT revision, applied_at FROM vf_events'))
        final_archive = api['archive_current'](runtime, root / 'media', backend.base, probe=True)
        final = {row['asset']: row for row in final_archive['assets']}
        jobs = {(j['asset'], j['revision']): j for j in runtime.snapshot()['jobs']}
        lineage = api['reference_evidence']([final], jobs, refs, case['edges'])
        materialized = {a['id']: a for a in snapshot['assets']}
        unpublished = {}
        current_rows = {}
        for shot, asset in materialized.items():
            row = final[shot]
            if asset['output'] != row['output']:
                path = root / f'unpublished-{shot}.mp4'
                api['trusted_download'](asset['output'], backend.base, path, 2 * 1024**3, backend.timeout)
                row = dict(row, revision=asset['revision'], output=asset['output'],
                           input_hash=asset['committed_hash'], sha256=api['checksum'](path))
                unpublished[shot] = dict(file=path.name, sha256=row['sha256'])
            current_rows[shot] = row
        computed_lineage = api['reference_evidence']([current_rows], jobs, refs, case['edges'])
        initial_bytes = {r['asset']: r['sha256'] for r in
                         load_json(args.output / 'source_media/revision-1.json')['assets']}
        for shot in final:
            if final[shot]['input_hash'] == initial[shot]['input_hash']:
                check(final[shot]['sha256'] == initial_bytes[shot], f'reused {shot} bytes disagree')
        publications = [row[0] for row in runtime.db.execute('SELECT revision FROM vf_timelines ORDER BY revision')]
        check(publications == [1, 3] and stale, 'expected Rev.3 only after Rev.1 and a stale completion')
        regenerated = [shot for shot in final if final[shot]['input_hash'] != initial[shot]['input_hash']]
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        expected = sorted({'S1', 'S3', *(c for _, c in case['edges'])})
        summary = dict(policy=policy, expected=expected, publications=publications, regenerated=regenerated,
                       matches_expected_selection=sorted(regenerated) == expected,
                       published_required_targets=all(shot in regenerated for shot in expected),
                       published_before_evidence=end_wall < release,
                       published_missing_required_targets=any(s not in regenerated for s in expected),
                       unpublished_renders=unpublished,
                       anchor_commit_wall_s=ready, release_wall_s=release, publication_wall_s=end_wall,
                       anchor_to_release_s=release-ready,
                       publication_gate_hold_s=release-ready if gated else None,
                       computed_reference=computed_lineage,
                       computed_assets={name: dict(epoch=a['revision'], output=a['output'],
                                                  input_hash=a['committed_hash'])
                                        for name,a in materialized.items()},
                       stale_completion_rejected=stale, reference_lineage=lineage,
                       provider_acceptances=sum(e['action']=='provider_acceptance' for e in events),
                       first_edit_to_publish_s=end_wall-edits[2], final_edit_to_publish_s=end_wall-edits[3],
                       shared_evidence_sha256=api['checksum'](evidence_path),
                       shared_acquisition_s=current['finished_s']-current['started_s'],
                       acquisition_overlaps_this_arm=future is not None,
                       source_clip_sha256={s: f['clip_sha256'] for s, f in load_json(frozen / 'index.json').items()},
                       reused=[shot for shot in final if shot not in regenerated],
                       provider_video_jobs=api['summarize_inspections'](runtime)['jobs'],
                       gpu_seconds=None)
        summary['shared_vlm_calls'] = sum(len(item['calls']) for item in current['shots'].values())
        summary['shared_vlm_parse_failures'] = sum(item['parse_failures'] for item in current['shots'].values())
        write(root / 'report.json', summary)
        return summary
    finally:
        runtime.close()


def main():
    args = cli()
    root = args.output
    root.mkdir(parents=True)
    reports = {}
    index = 0
    for story in args.stories:
        for seed in STORIES[story]['seeds']:
            block = f'{story}-{seed}'
            args.output = root / block
            run_seed(args, dict(STORIES[story], story=story, seed=seed), index)
            index += 1
            if not args.prepare_only:
                reports[block] = load_json(args.output / 'report.json')
                (root / 'report.json').write_text(json.dumps(reports, indent=2) + '\n')


def run_seed(args, case, index, *, run_arm=run_policy, policies=None):
    args.ollama = args.ollama.rstrip('/')
    seed = case['seed']
    verifier = load_json(ROOT / 'verifier_protocol.json')
    check(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg and ffprobe are required')
    args.output.mkdir(parents=True)
    frozen = args.output / 'frozen'
    api = live_setup(args.repo.resolve())
    api['write_json'](args.output / 'story.json', case)
    manifest = generated_manifest(case)
    api['write_json'](args.output / 'manifest.json', manifest)
    api['write_json'](args.output / 'run_config.json', vars(args) | {
        key: str(value) for key, value in vars(args).items() if isinstance(value, Path)})
    if args.prepare_only:
        runtime = api['AssetRuntime'](':memory:')
        runtime.initialize(manifest)
        runtime.close()
        print('Validated the joint manifest without contacting either backend.')
        return
    model = model_identity(args, verifier['model'])
    api['write_json'](args.output / 'model.json', model)
    backend = api['ComfyUI'](args.server.rstrip('/'))
    api['check_backend'](backend)
    refs = api['ReferenceFrames'](args.output / 'references', backend)
    db = args.output / 'source.db'
    prior = args.reuse_source_from / args.output.name if args.reuse_source_from else None
    if prior is not None and (prior / 'source_media/revision-1.json').is_file():
        print(f"{case['story']} seed {seed}: reusing common Rev.1 from {prior}", flush=True)
        check(load_json(prior / 'story.json') == case and load_json(prior / 'manifest.json') == manifest,
              'saved source uses a different story or manifest')
        previous_archive = load_json(prior / 'source_media/revision-1.json')
        with closing(sqlite3.connect((prior / 'source.db').resolve().as_uri() + '?mode=ro', uri=True)) as old_db:
            with closing(sqlite3.connect(db)) as new_db:
                old_db.backup(new_db)
        source = api['AssetRuntime'](db)
        try:
            archive = api['archive_current'](source, args.output / 'source_media', backend.base, probe=True)
            check(archive['revision'] == 1 and
                  {row['asset']: row['sha256'] for row in archive['assets']} ==
                  {row['asset']: row['sha256'] for row in previous_archive['assets']},
                  'provider outputs do not match the archived Rev.1 bytes')
            by_shot = {row['asset']: row for row in archive['assets']}
            for parent, _ in case['edges']:
                refs.stage(by_shot[parent]['output'])
            jobs = {(j['asset'], j['revision']): j for j in source.snapshot()['jobs']}
            api['reference_evidence']([by_shot], jobs, refs, case['edges'])
            stage_inputs(api, frozen, archive)
        finally:
            source.close()
    else:
        print(f"{case['story']} seed {seed}: generating common Rev.1", flush=True)
        source = api['AssetRuntime'](db)
        try:
            source.initialize(manifest)
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                events = api['pass_once'](source, backend, refs.build, max_submit=0)
                free = max(0, 2 - len(source.unresolved_remote()))
                for job in source.pending()[:free]:
                    for dep in source._inputs(job['asset'])['dependencies']:
                        if dep['kind'] == 'reference':
                            refs.stage(dep['output'])
                events += api['pass_once'](api['SubmissionView'](source), backend, refs.build, max_submit=free)
                log(args.output / 'source_events.jsonl', 'worker', events=events)
                check(not any(e['action'] in ('failed','rejected','validation_error') for e in events),
                      f'source workflow failure: {events}')
                if source.snapshot()['timeline']:
                    break
                time.sleep(args.poll)
            else:
                raise TimeoutError('source did not publish')
            archive = api['archive_current'](source, args.output / 'source_media', backend.base, probe=True)
            stage_inputs(api, frozen, archive)
        finally:
            source.close()
    reports = []
    order = policies if policies is not None else [p for p in ORDERS[index % len(ORDERS)] if p in args.policy]
    for policy in order:
        print(f"{case['story']} seed {seed}: running {policy}", flush=True)
        api['check_backend'](backend)
        reports.append(run_arm(args, api, args.output / policy, db, frozen, case, verifier, refs, backend, policy))
        check(model_identity(args, verifier['model']) == model, 'Ollama model changed mid-experiment')
        api['write_json'](args.output / 'report.json', dict(status='completed' if len(reports) == len(args.policy) else 'partial', policies=reports))


if __name__ == '__main__':
    main()
