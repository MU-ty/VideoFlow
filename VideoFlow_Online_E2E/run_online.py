"""Run online evidence acquisition and prompt-led video editing against the actual worker."""
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
POLICIES = ('egdr', 'declared-worklist', 'full')


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default=ROOT / 'runtime', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--ollama', default='http://127.0.0.1:11434')
    parser.add_argument('--timeout', type=float, default=2400)
    parser.add_argument('--poll', type=float, default=2)
    parser.add_argument('--stories', nargs='+', choices=STORIES, default=list(STORIES))
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if args.poll <= 0 or args.timeout <= 0 or len(set(args.stories)) != len(args.stories):
        parser.error('poll and timeout must be positive and stories must be distinct')
    args.server = args.server.rstrip('/')
    args.ollama = args.ollama.rstrip('/')
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


def stage_inputs(api, root, archive, source_dir):
    root.mkdir()
    records = {}
    for row in archive['assets']:
        shot = row['asset']
        if shot not in CANDIDATES:
            continue
        sheet = root / f'{shot}.png'
        records[shot] = api['make_sheet'](source_dir / row['file'], sheet)
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
    from artifacts import archive_current, checksum, write_json
    from asset_runtime import AssetRuntime, fingerprint
    from asset_worker import pass_once
    from comfyui import ComfyUI
    from recovery_matrix import SubmissionView
    from recovery_reference_comfy import ReferenceFrames, check_backend, reference_evidence
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
            marker = case['distractor_marker'] if shot == 'S2' else case['target_marker']
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


def dependency_result(shots):
    expected = {'S3'}
    matched = {shot for shot, item in shots.items() if item['decision'] == 'match'}
    fallback = {shot for shot, item in shots.items() if item['decision'] == 'fallback'}
    selected = matched | fallback
    return dict(expected=sorted(expected), matched=sorted(matched), fallback=sorted(fallback),
                selected=sorted(selected), missing_matches=sorted(expected - matched),
                missed=sorted(expected - selected), excess=sorted(selected - expected),
                decisions={shot: {k: item[k] for k in ('decision', 'first', 'structured')}
                           for shot, item in shots.items()})


def run_policy(args, api, root, seed_db, source_archive, case, verifier, refs, backend, policy):
    root.mkdir()
    journal = root / 'events.jsonl'
    write = api['write_json']
    method = {'full': 'full', 'egdr': 'dair', 'declared-worklist': 'attr_dag'}[policy]

    class Runtime(api['AssetRuntime']):
        @staticmethod
        def _state_changes(assets, specs, edges, event):
            changed = api['AssetRuntime']._state_changes(assets, specs, edges, event)
            # The Full control is given the intended direct color targets.
            return changed | {'S3'} if policy == 'full' else changed

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
    with closing(Runtime(root / 'project.db')) as runtime:
        initial = {row['asset']: row for row in runtime.snapshot()['timeline']}
        event = dict(id='edit-2', expected_revision=1, target='S1', key=KEY,
                     value='scarlet red', method=method)
        if policy == 'egdr':
            event['evidence_candidates'] = list(CANDIDATES)

        def observe():
            started = time.monotonic()
            frozen = root / 'frozen'
            stage_inputs(api, frozen, source_archive, root.parent / 'source_media')
            evidence = acquire(args, api, frozen, verifier, case, 2, root / 'vlm.jsonl')
            evidence['started_with_frame_extraction_s'] = started
            return evidence

        with ThreadPoolExecutor(max_workers=1) as pool:
            deadline = time.monotonic() + args.timeout
            edit_at = time.time()
            plan = runtime.edit(event)
            log(journal, 'edit', revision=2, plan=plan)
            future = pool.submit(observe) if policy == 'egdr' else None
            evidence = None
            evidence_applied = None
            transport = Transport()
            while time.monotonic() < deadline:
                if future is not None and future.done() and evidence is None:
                    evidence = future.result()
                    write(root / 'evidence.json', evidence)
                    records = load_json(root / 'frozen/index.json')
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
                    check(policy != 'egdr' or evidence is not None, 'published before resolving evidence')
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
                        missing_targets=sorted(expected - regenerated),
                        excess=sorted(regenerated - expected),
                        byte_identical_to_source=sorted(shot for shot, row in final.items()
                            if row['sha256'] == initial_bytes[shot]),
                        reference_lineage=lineage,
                        edit_to_publish_s=published_at - edit_at),
                    initial_plan=plan['regenerate'],
                    evidence_acquisition_s=(evidence['finished_s'] - evidence['started_with_frame_extraction_s']
                                            if evidence else None),
                    edit_to_evidence_applied_s=evidence_applied - edit_at if evidence else None,
                    vlm_calls=sum(len(item['calls']) for item in evidence['shots'].values()) if evidence else 0,
                    vlm_parse_failures=sum(item['parse_failures'] for item in evidence['shots'].values()) if evidence else 0,
                    provider_acceptances=sum(e['action'] == 'provider_acceptance' for e in events))


def step(api, runtime, transport, refs, journal):
    events = api['pass_once'](runtime, transport, build=refs.build, max_submit=0)
    free = max(0, 2 - len(runtime.unresolved_remote()))
    for job in runtime.pending()[:free]:
        for dep in runtime._inputs(job['asset'])['dependencies']:
            if dep['kind'] == 'reference':
                refs.stage(dep['output'])
    if free:
        events += api['pass_once'](api['SubmissionView'](runtime), transport, refs.build, max_submit=free)
    log(journal, 'worker', events=events)
    check(not any(e['action'] in ('failed', 'rejected', 'validation_error') for e in events),
          f'worker failure: {events}')


def main():
    args = cli()
    root = args.output
    root.mkdir(parents=True)
    reports = {block: dict(status='pending', policies=[]) for block, _ in cases(args.stories)}
    api = live_setup(args.repo.resolve())
    for index, (block, case) in enumerate(cases(args.stories)):
        block_path = root / block
        if not args.prepare_only:
            api['write_json'](root / 'report.json', reports)
        result = reports[block]
        try:
            run_seed(args, api, block_path, case, result, index)
        except Exception as exc:
            if not args.prepare_only:
                result.update(status='failed', error=str(exc))
                api['write_json'](root / 'report.json', reports)
            raise
        if not args.prepare_only:
            api['write_json'](root / 'report.json', reports)


def cases(stories):
    for story in stories:
        for seed in STORIES[story]['seeds']:
            yield f'{story}-{seed}', dict(STORIES[story], story=story, seed=seed)


def run_seed(args, api, root, case, result, index):
    verifier = load_json(ROOT / 'verifier_protocol.json')
    check(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg and ffprobe are required')
    root.mkdir()
    manifest = generated_manifest(case)
    write = api['write_json']
    write(root / 'story.json', case)
    write(root / 'manifest.json', manifest)
    write(root / 'run_config.json', dict(server=args.server, ollama=args.ollama,
          timeout=args.timeout, poll=args.poll, policies=list(POLICIES),
          reference_mode='prompt anchors and generated upstream first frames'))
    if args.prepare_only:
        with closing(api['AssetRuntime'](':memory:')) as runtime:
            runtime.initialize(manifest)
        print(f'{root.name}: source manifest validated')
        return

    model = model_identity(args, verifier['model'])
    write(root / 'model.json', model)
    backend = api['ComfyUI'](args.server)
    api['check_backend'](backend)
    source_db = root / 'source.db'

    print(f'{root.name}: generating common Rev.1', flush=True)
    source_refs = api['ReferenceFrames'](root / 'source_references', backend)
    with closing(api['AssetRuntime'](source_db)) as source:
        source.initialize(manifest)
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            step(api, source, backend, source_refs, root / 'source_events.jsonl')
            if source.snapshot()['timeline']:
                break
            time.sleep(args.poll)
        else:
            raise TimeoutError('source did not publish')
        source_archive = api['archive_current'](source, root / 'source_media', backend.base, probe=True)

    order = POLICIES if index % 2 == 0 else tuple(reversed(POLICIES))
    for policy in order:
        print(f'{root.name}: running {policy}', flush=True)
        api['check_backend'](backend)
        policy_root = root / policy
        # Fresh reference staging per arm avoids reusing staged prompt images across policies.
        refs = api['ReferenceFrames'](root / (policy + '-references'), backend)
        result['active_policy'] = policy
        summary = run_policy(args, api, policy_root, source_db, source_archive,
                             case, verifier, refs, backend, policy)
        if policy == 'egdr':
            check(model_identity(args, verifier['model']) == model, 'Ollama model changed mid-experiment')
        write(policy_root / 'report.json', summary)
        result['policies'].append(summary)
    result.pop('active_policy')
    result['status'] = 'completed'


if __name__ == '__main__':
    main()
