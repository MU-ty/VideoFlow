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
VISUAL_POLICIES = ('naive-vlm', 'egdr-no-verification', 'egdr')
POLICIES = ('declared-worklist', *VISUAL_POLICIES, 'full')


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
    parser.add_argument('--source-probes-only', action='store_true',
                        help='generate Rev.1 and archive initial EGDR probes without rendering an edit')
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


def parse_panels(raw, schema):
    rows = json.loads(raw)['panels']
    if not isinstance(rows, list) or len(rows) != 2:
        raise ValueError('expected two panel records')
    fields = schema['properties']['panels']['items']['properties']
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(fields):
            raise ValueError('unexpected panel fields')
        for name, spec in fields.items():
            value = row[name]
            if ('enum' in spec and value not in spec['enum']
                    or name == 'panel' and type(value) is not int
                    or name == 'complete' and type(value) is not bool):
                raise ValueError(f'invalid panel field: {name}')
    if {row['panel'] for row in rows} != {1, 2}:
        raise ValueError('missing panel 1 or 2')
    return rows


def direct_state(panels):
    verdicts = [panel['verdict'] for panel in panels]
    if 'target' in verdicts:
        return 'confirmed'
    if len(verdicts) == 4 and 'excluded' in verdicts and set(verdicts) <= {'excluded', 'absent_host'}:
        return 'rejected'
    return 'unknown'


def final_decision(first, state):
    if state == 'confirmed':
        return 'match'
    if state == 'rejected':
        return 'distinct'
    return {'yes': 'match', 'no': 'distinct', 'unknown': 'fallback'}[first]


def acquire(args, api, frozen, verifier, case, revision, log_path, policy):
    started = time.monotonic()
    model = verifier['model']
    results = {}
    part = case['part'].replace('{{target.color}} ', '')
    host, target, distractor = (case[key] for key in ('host', 'target_marker', 'distractor_marker'))
    question = (f'Does any source panel show the {part} on {host} with a visible '
                f'{target} on the same object? Exclude the {distractor} lookalike.')
    structured_prompt = (
        f'For each panel independently inspect {host} and its {part}. The target marker is {target}; '
        f'the distractor marker is {distractor}. Record host and part visibility and the body marker. '
        'The part itself is not a marker. Mark same_instance=yes only if the marker and part are on '
        'the same object. Set complete=true only when the evidence is readable and no unexamined '
        'host could be present, or when no host is visible. Use unknown for obscured or ambiguous '
        'fields. Return panels 1 (left) and 2 (right).')
    direct_prompt = (
        f'For each panel independently inspect {host} and its {part}. The target marker is {target}; '
        f'the distractor marker is {distractor}. The marker and part must be visible on the same '
        'object. The part itself is not a marker. Use target for a clear target match, excluded for '
        'a readable distractor when no unexamined host could be present, absent_host when clearly no '
        'host is visible, and unknown otherwise. Return panels 1 (left) and 2 (right).')

    for shot in CANDIDATES:
        calls = []

        def call(phase, path, request, parse):
            start = time.monotonic()
            row = dict(phase=phase, image_sha256=api['checksum'](path), started_s=start)
            try:
                row['raw'] = request()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                row['request_error'] = str(exc)
                parsed = None
            else:
                try:
                    parsed = parse(row['raw'])
                except (ValueError, KeyError, TypeError) as exc:
                    row['parse_error'] = str(exc)
                    parsed = None
            row['finished_s'] = time.monotonic()
            calls.append(row)
            log(log_path, 'vlm_reply', policy=policy, revision=revision, shot=shot, **row)
            return parsed

        sheet = frozen / f'{shot}.png'
        discovery = call('discover', sheet, lambda: api['visual_reply'](
            args.ollama, model, f'Recolor the {part} scarlet red.', sheet,
            visibility_question=question, format_retry=True), api['parse_visual'])
        first = discovery['decision'] if discovery else 'unknown'
        state = 'skipped'
        if policy != 'egdr-no-verification':
            structured = policy == 'egdr'
            schema = verifier['structured_schema' if structured else 'direct_schema']
            prompt = structured_prompt if structured else direct_prompt
            panels = []
            for pair in ('1-2', '3-4'):
                image = frozen / f'{shot}-{pair}.png'
                rows = call('verify-' + pair, image, lambda: api['image_reply'](
                    args.ollama, model, image, prompt, schema), lambda raw: parse_panels(raw, schema))
                if rows is not None:
                    panels.extend(rows)
            state = structured_state(panels, case) if structured else direct_state(panels)
        results[shot] = dict(decision=final_decision(first, state), first=first, verification=state,
                             calls=calls, parse_failures=sum('parse_error' in row for row in calls),
                             request_failures=sum('request_error' in row for row in calls))
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
                decisions={shot: {k: item[k] for k in ('decision', 'first', 'verification')}
                           for shot, item in shots.items()})


def run_policy(args, api, root, seed_db, source_archive, case, verifier, refs, backend, policy,
               *, frozen_evidence=None, resume=False):
    if resume and frozen_evidence is None:
        raise ValueError('resume requires frozen evidence')
    root.mkdir(exist_ok=resume)
    journal = root / 'events.jsonl'
    write = api['write_json']
    visual = policy in VISUAL_POLICIES
    method = 'dair' if visual else {'full': 'full', 'declared-worklist': 'attr_dag'}[policy]

    class Runtime(api['AssetRuntime']):
        @staticmethod
        def _plan(assets, specs, edges, event):
            # Use the same evidence ledger while the direct arm executes attribute worklist closure.
            plan = api['AssetRuntime']._plan(assets, specs, edges, event)
            if policy == 'naive-vlm':
                baseline = api['AssetRuntime']._plan(assets, specs, edges, dict(event, method='attr_dag'))
                check(baseline.regenerate == plan.regenerate, 'paired closure mismatch')
                return baseline
            return plan

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

    database = root / 'project.db'
    if not resume or not database.is_file():
        check(not journal.exists(), 'cannot restart an arm with a journal but no database')
        with closing(sqlite3.connect(seed_db)) as source_db, closing(sqlite3.connect(database)) as dest_db:
            source_db.backup(dest_db)
    with closing(Runtime(database)) as runtime:
        source = runtime.db.execute('SELECT entries FROM vf_timelines WHERE revision=1').fetchone()
        check(source is not None and api['fingerprint'](json.loads(source[0])) ==
              source_archive['timeline_hash'], 'arm database does not match archived Rev.1')
        initial = {row['asset']: row for row in json.loads(source[0])}
        event = dict(id='edit-2', expected_revision=1, target='S1', key=KEY,
                     value='scarlet red', method=method)
        if visual:
            event['evidence_candidates'] = list(CANDIDATES)

        def observe():
            started = time.monotonic()
            frozen = root / 'frozen'
            stage_inputs(api, frozen, source_archive, root.parent / 'source_media')
            evidence = acquire(args, api, frozen, verifier, case, 2, root / 'vlm.jsonl', policy)
            evidence['started_with_frame_extraction_s'] = started
            return evidence

        def apply_evidence(evidence, records):
            write(root / 'evidence.json', evidence)
            for shot, item in evidence['shots'].items():
                proof = api['fingerprint'](dict(revision=2, anchor='S1', key=KEY, shot=shot,
                    source_input_hash=initial[shot]['input_hash'],
                    source_clip_sha256=records[shot]['clip_sha256'], evidence=item))
                result = runtime.resolve_evidence(2, shot, initial[shot]['input_hash'],
                                                  proof, item['decision'])
                log(journal, 'evidence_applied', **result)
            return time.time()

        with ThreadPoolExecutor(max_workers=1) as pool:
            deadline = time.monotonic() + args.timeout
            edit_at = time.time()
            prior_edit = runtime.db.execute(
                'SELECT applied_at FROM vf_events WHERE id=?', (event['id'],)).fetchone()
            plan = runtime.edit(event)
            if prior_edit is None:
                log(journal, 'edit', revision=2, plan=plan)
            else:
                check(resume, 'arm already contains an edit')
                edit_at = prior_edit[0]
                log(journal, 'resume', revision=2)
            future = pool.submit(observe) if visual and frozen_evidence is None else None
            evidence = frozen_evidence
            evidence_applied = None
            if evidence is not None:
                records = {row['asset']: {'clip_sha256': row['sha256']}
                           for row in source_archive['assets']}
                evidence_applied = apply_evidence(evidence, records)
            transport = Transport()
            while time.monotonic() < deadline:
                if runtime.snapshot()['timeline']:
                    check(not visual or evidence is not None, 'published before resolving evidence')
                    break
                if future is not None and future.done() and evidence is None:
                    evidence = future.result()
                    records = load_json(root / 'frozen/index.json')
                    evidence_applied = apply_evidence(evidence, records)

                step(api, runtime, transport, refs, journal)
                timeline = runtime.snapshot()['timeline']
                if timeline:
                    check(not visual or evidence is not None, 'published before resolving evidence')
                    break
                time.sleep(args.poll)
            else:
                jobs = [dict(shot=j['asset'], status=j['status'], prompt_id=j['external_id'],
                             error=j['error']) for j in runtime.snapshot()['jobs'] if j['revision'] == 2]
                raise TimeoutError(f'edit reached the publication deadline; Rev.2 jobs: {jobs}')

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
                    resumed_after_edit=prior_edit is not None,
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
                                            if evidence and frozen_evidence is None else None),
                    edit_to_evidence_applied_s=(evidence_applied - edit_at
                                                if evidence and prior_edit is None else None),
                    vlm_calls=sum(len(item['calls']) for item in evidence['shots'].values()) if evidence else 0,
                    vlm_parse_failures=sum(item['parse_failures'] for item in evidence['shots'].values()) if evidence else 0,
                    vlm_request_failures=sum(item['request_failures'] for item in evidence['shots'].values()) if evidence else 0,
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
          source_probes_only=args.source_probes_only,
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

    if args.source_probes_only:
        probe_root = root / 'egdr'
        probe_root.mkdir()
        frozen = probe_root / 'frozen'
        stage_inputs(api, frozen, source_archive, root / 'source_media')
        evidence = acquire(args, api, frozen, verifier, case, 2, probe_root / 'vlm.jsonl', 'egdr')
        check(model_identity(args, verifier['model']) == model, 'Ollama model changed mid-experiment')
        write(probe_root / 'evidence.json', evidence)
        result.update(status='completed', source_probes_only=True,
                      unresolved=[shot for shot, item in evidence['shots'].items()
                                  if item['verification'] == 'unknown'])
        return

    order = POLICIES[index % len(POLICIES):] + POLICIES[:index % len(POLICIES)]
    for policy in order:
        print(f'{root.name}: running {policy}', flush=True)
        api['check_backend'](backend)
        policy_root = root / policy
        # Fresh reference staging per arm avoids reusing staged prompt images across policies.
        refs = api['ReferenceFrames'](root / (policy + '-references'), backend)
        result['active_policy'] = policy
        summary = run_policy(args, api, policy_root, source_db, source_archive,
                             case, verifier, refs, backend, policy)
        if policy in VISUAL_POLICIES:
            check(model_identity(args, verifier['model']) == model, 'Ollama model changed mid-experiment')
        write(policy_root / 'report.json', summary)
        result['policies'].append(summary)
    def image_schedule(policy):
        return {shot: [(row['phase'], row['image_sha256']) for row in item['calls']]
                for shot, item in load_json(root / policy / 'evidence.json')['shots'].items()}
    direct, ablated, structured = map(image_schedule, VISUAL_POLICIES)
    check(direct == structured and all(len(rows) == 3 for rows in direct.values())
          and all(ablated[shot] == rows[:1] for shot, rows in direct.items()),
          'the visual arms did not receive their paired image schedule')
    result['matched_visual_image_schedule'] = direct
    result.pop('active_policy')
    result['status'] = 'completed'


if __name__ == '__main__':
    main()
