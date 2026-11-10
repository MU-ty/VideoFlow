"""Run a matched real-render EGDR pilot from one published source revision."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from artifacts import archive_current, write_json
from asset_runtime import AssetRuntime, Conflict, fingerprint
from asset_worker import pass_once
from comfyui import ComfyUI
from provider_audit import capture_environment, summarize_inspections
from vision_probe import collect_selections, make_sheet
from vision_evidence_reuse import capture_probe, ffmpeg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from video_edit_ablation import asset, wait_for


POLICIES = {
    'declared': 'attr_dag',
    'checklist': 'checklist_union',
    'raw_union': 'union_plus_vision',
    'egdr': 'union_plus_verified',
    'egdr_conservative': 'union_plus_unresolved',
    'full': 'full',
    'worklist': 'attr_dag',
    'online_egdr': 'online_egdr',
}
KEY = 'target.color'
ENTITY = 'ribbed amber handwheel on the cream press with two teal stripes'


def pilot_inputs(seed):
    base = 'a cream bench press with two teal stripes and a large ribbed amber handwheel'
    shots = {
        'S1': f'Steady side close-up of {base}. A technician turns the handwheel.',
        'S2': f'Medium shot of {base} on a workbench. A technician lowers the press ram.',
        'S3': 'Front close-up of a different cream press with an orange diamond decal, '
              'no teal stripes, and an amber handwheel.',
        'S4': 'A static shot of a closed gray toolbox on another bench. No press is visible.',
    }
    edit = ('Recolor only the ribbed amber handwheel of the cream press with two teal stripes '
            'to cobalt blue wherever visible. Keep the orange-diamond press and all other '
            'objects unchanged. Do not add an absent press or handwheel.')
    case = dict(id='striped-press', entity=ENTITY, attribute='color', edit=edit,
                shots=shots, declared=['S1'])
    question = ('Do any of the four original panels show a ribbed amber handwheel '
                'belonging to the cream press with two teal stripes? The orange-diamond '
                'press does not count.')
    return color_manifest(shots, seed), case, question


def color_manifest(shots, seed):
    # All arms share the same editable prompt bindings and initial values.
    return dict(assets=[
        asset(shot, prompt + ' Apply the following appearance instruction where it applies. {{target.color}}',
              {KEY: 'Preserve the described original appearance.'}, seed + index)
        for index, (shot, prompt) in enumerate(shots.items())
    ])


def lamp_inputs(seed):
    target = 'the cream desk lamp with one large black hexagon on its cream base'
    shots = {
        'S1': f'Steady frontal close-up of {target} and its solid silver metal dome shade. '
              'Both the shade and the black hexagon are fully visible on a wooden desk.',
        'S2': 'Steady close-up of a different cream desk lamp with one large orange triangle '
              'on its cream base and a solid silver metal dome shade. Both are visible.',
        'S3': f'Steady three-quarter view of {target} and its solid silver metal dome shade. '
              'Both the shade and the black hexagon remain visible on a wooden desk.',
        'S4': 'Steady close-up of a closed black tool case alone on a shelf. No lamp is visible.',
    }
    edit = (
        f'Recolor only the silver metal dome shade of {target} to cobalt blue wherever visible. '
        'Preserve the cream base and black hexagon. Keep the orange-triangle lamp and all other '
        'objects unchanged. Do not add a lamp where none is visible.'
    )
    case = dict(id='hexagon-lamp', entity=f'silver metal dome shade of {target}',
                attribute='color', edit=edit, shots=shots, declared=['S1'])
    question = (
        'In any single source panel, do you see a solid silver metal dome shade on a cream '
        'desk lamp whose own cream base has a large black hexagon? The orange-triangle lamp '
        'and a shelf without a lamp do not count.'
    )
    return color_manifest(shots, seed), case, question


STORIES = {'striped-press': pilot_inputs, 'hexagon-lamp': lamp_inputs}
LAMP_ORACLE = {
    'source': {
        'S1': 'Silver dome shade and black hexagon visibly belong to the same lamp',
        'S2': 'Different silver-shaded lamp marked by an orange triangle',
        'S3': 'Silver dome shade and black hexagon visibly belong to the same lamp',
        'S4': 'Closed black tool case with no lamp',
    },
    'final': {
        'S1': 'Scarlet dome shade, cream base and black hexagon retained',
        'S2': 'Silver shade and orange triangle retained',
        'S3': 'Scarlet dome shade, cream base and black hexagon retained',
        'S4': 'Closed tool case retained, no lamp added',
    },
    'target_shots': ['S1', 'S3'],
    'declared_shots': ['S1'],
}


def apply_policy(runtime, case, selected, source, evidence, policy, expected_revision=1, *, continuous=False):
    target = case.get('target', 'S1')
    event_id = case['id'] if expected_revision == 1 else f"{case['id']}-{expected_revision}"
    event = dict(id=event_id, expected_revision=expected_revision, target=target, key=KEY,
                 value=case['edit'], scope='local')
    if policy == 'online_egdr':
        event['evidence_candidates'] = sorted(set(source) - {target})
    elif policy == 'full' and continuous:
        # Assign the edit value to every authored binding without introducing
        # artificial readiness edges in the full-regeneration baseline.
        event.update(scope='global', method='full')
    else:
        repairs = [
            dict(child=shot, source_input_hash=source[shot]['input_hash'],
                 evidence_sha256=fingerprint(dict(shot=shot, policy=policy, evidence=evidence)))
            for shot in sorted(set(selected) - {target})
        ]
        event.update(state_repairs=repairs, method='attr_dag' if policy == 'worklist' else 'dair')
    return runtime.edit(event)


def submit_anchor(folder, runtime, backend, epoch):
    events = pass_once(runtime, backend, max_submit=1)
    with (folder / 'worker.jsonl').open('a', encoding='utf-8') as out:
        out.write(json.dumps(dict(at=time.time(), events=events)) + '\n')
    if not any(j['asset'] == 'S1' and j['revision'] == epoch and j['status'] == 'submitted'
               for j in runtime.active()):
        raise RuntimeError(f'S1 epoch {epoch} did not reach the acknowledgement barrier')


def source_probes(root, sheets, case, question, args, log):
    probes = {}
    for shot, sheet in sheets.items():
        if shot in case['declared']:
            continue
        panels = []
        for i, (x, y) in enumerate(((0, 0), (768, 0), (0, 432), (768, 432)), 1):
            tile = root / f'{shot}-panel-{i}.png'
            ffmpeg('-i', sheet, '-vf', f'crop=768:432:{x}:{y}', '-frames:v', 1, '-threads', 1, tile)
            panels.append(tile)
        probes[shot] = capture_probe(args.ollama, args.vision_model, question, panels, root / f'probe-{shot}', log)
    return probes


def run(args):
    backend = ComfyUI(args.server)
    queue = backend._request('/queue')
    if any(queue.get(key) for key in ('queue_running', 'queue_pending')):
        raise RuntimeError('the experiment requires an idle, dedicated ComfyUI queue')
    manifest, case, question = STORIES[args.story](args.seed)
    if args.frame_notes:
        notes = json.loads(args.frame_notes.read_text(encoding='utf-8'))
        if set(notes) != set(case['shots']) or any(not isinstance(note, str) or not note.strip()
                                                 for note in notes.values()):
            raise ValueError('provide one nonempty frame note per shot')
        case['shots'] = {shot: prompt + ' Frame observation: ' + notes[shot]
                         for shot, prompt in case['shots'].items()}
    args.output.mkdir(parents=True)
    if args.story == 'hexagon-lamp':
        # Keep the predeclared evaluation labels out of all model inputs.
        write_json(args.output / 'visual_oracle.json', LAMP_ORACLE)
    write_json(args.output / 'protocol.json', dict(story=args.story, seed=args.seed, manifest=manifest, case=case,
               question=question, policies=args.policies, text_model=args.text_model,
               vision_model=args.vision_model, frame_notes=bool(args.frame_notes), timeout=args.timeout,
               server=backend.base, continuous=args.continuous))
    if args.source_run:
        source_protocol = json.loads((args.source_run / 'protocol.json').read_text(encoding='utf-8'))
        if source_protocol['manifest'] != manifest or source_protocol['server'] != backend.base:
            raise ValueError('source run must use the same seed, manifest and backend')
        if not (args.source_run / 'source.db').is_file():
            raise ValueError('source database is missing')
    original = AssetRuntime((args.source_run or args.output) / 'source.db')
    try:
        if not args.source_run:
            original.initialize(manifest)
            capture_environment(original, backend)
            wait_for(args.output, original, backend, 'videoflow', lambda: original.snapshot()['timeline'],
                     time.monotonic() + args.timeout, args.poll)
        source_snapshot = original.snapshot()
        if source_snapshot['revision'] != 1 or not source_snapshot['timeline']:
            raise ValueError('source run must have published revision 1')
        before = archive_current(original, args.output / 'source', backend.base, probe=True)
        if args.source_run:
            frozen = json.loads((args.source_run / 'source' / 'revision-1.json').read_text())
            if (before['timeline_hash'] != frozen['timeline_hash']
                    or {row['asset']: row['sha256'] for row in before['assets']}
                    != {row['asset']: row['sha256'] for row in frozen['assets']}):
                raise RuntimeError('provider source bytes differ from the frozen run')
        source = {row['asset']: row for row in before['assets']}
        sheets = {shot: args.output / 'source' / f'{shot}.jpg' for shot in source}
        frames = {shot: make_sheet(args.output / 'source' / row['file'], sheets[shot])
                  for shot, row in source.items()}
        if any(frames[shot]['clip_sha256'] != row['sha256'] for shot, row in source.items()):
            raise RuntimeError('evidence frames do not match the frozen source clips')
        if args.source_only:
            return
        raw = []
        started = time.monotonic()
        if args.continuous:
            with (args.output / 'responses.jsonl').open('x', encoding='utf-8') as responses:
                probes = source_probes(args.output, sheets, case, question, args, responses)
                responses.flush()
            raw = [json.loads(line) for line in (args.output / 'responses.jsonl').read_text().splitlines()]
            choices = dict(attr_dag=set(case['declared']), full=set(source), online_egdr=set(case['declared']))
            audit = dict(probes=probes)
        else:
            with (args.output / 'responses.jsonl').open('x', encoding='utf-8') as responses:
                def log(row):
                    raw.append(row)
                    responses.write(json.dumps(row, ensure_ascii=False) + '\n')
                    responses.flush()

                choices, audit = collect_selections(
                    case, sheets, args.ollama, args.text_model, args.vision_model, log,
                    visibility_question=question, verify_visual=True,
                )
            if 'union_plus_verified' in choices:
                choices['union_plus_unresolved'] = (choices['union_plus_verified']
                                                    | set(audit['visual_unknown'])
                                                    | set(audit['verification_unknown']))
            if audit['visual_invalid'] or audit['verification_invalid']:
                raise RuntimeError('invalid evidence replies; inspect responses.jsonl before rendering arms')
        evidence_seconds = time.monotonic() - started
        write_json(args.output / 'evidence.json', dict(frames=frames, audit=audit,
                   paired_probe_wall_seconds=evidence_seconds, responses_sha256=fingerprint(raw),
                   selections={key: sorted(value) for key, value in choices.items()}))
        evidence = dict(frames=frames, responses=raw, case=case, question=question,
                        text_model=args.text_model, vision_model=args.vision_model)
        rows = []
        for policy in args.policies:
            folder = args.output / policy
            folder.mkdir()
            runtime = AssetRuntime(folder / 'project.db')
            try:
                original.db.backup(runtime.db)
                selected = choices[POLICIES[policy]]
                first = plan = apply_policy(runtime, case, selected, source, evidence, policy,
                                            continuous=args.continuous)
                repairs, obsolete_evidence_rejected = [], False
                if args.continuous:
                    submit_anchor(folder, runtime, backend, 2)
                    runtime.close()
                    runtime = AssetRuntime(folder / 'project.db')
                    red = dict(case, edit=case['edit'].replace('cobalt blue', 'scarlet red'))
                    plan = apply_policy(runtime, red, selected, source, evidence, policy,
                                        expected_revision=2, continuous=True)
                    submit_anchor(folder, runtime, backend, 3)
                    if policy == 'online_egdr':
                        for shot, probe in probes.items():
                            decision = {'yes': 'match', 'no': 'distinct', 'unknown': 'fallback'}[probe['decision']]
                            proof = fingerprint(dict(shot=shot, probe=probe, frame=frames[shot], evidence=evidence))
                            if not obsolete_evidence_rejected:
                                try:
                                    runtime.resolve_evidence(2, shot, source[shot]['input_hash'], proof, decision)
                                except Conflict:
                                    obsolete_evidence_rejected = True
                                else:
                                    raise RuntimeError('superseded evidence was accepted')
                            repairs.append(runtime.resolve_evidence(
                                3, shot, source[shot]['input_hash'], proof, decision))
                    write_json(folder / 'repair_trace.json', dict(first=first, second=plan, repairs=repairs,
                               obsolete_evidence_rejected=obsolete_evidence_rejected))
                def published():
                    if args.continuous and runtime.unresolved_remote():
                        return None
                    return runtime.snapshot()['timeline']

                wait_for(folder, runtime, backend, 'videoflow', published,
                         time.monotonic() + args.timeout, args.poll)
                after = archive_current(runtime, folder / 'media', backend.base, probe=True)
                reused = sorted(row['asset'] for row in after['assets']
                                if all(row[field] == source[row['asset']][field]
                                       for field in ('input_hash', 'sha256', 'output')))
                final_selection = set(plan['regenerate']).union(*(r['regenerate'] for r in repairs))
                if set(reused) != set(source) - final_selection:
                    raise RuntimeError('published reuse disagrees with the committed plan')
                snapshot = runtime.snapshot()
                jobs = [job for job in snapshot['jobs'] if job['revision'] > 1]
                history = summarize_inspections(runtime)
                measured_jobs = [job for job in jobs if job['submission_intents']] if args.continuous else jobs
                job_ids = {job['id'] for job in measured_jobs}
                edited_history = [item for item in history['jobs'] if item['job'] in job_ids]
                spans = [item['execution_span_seconds'] for item in edited_history
                         if item['execution_span_seconds'] is not None]
                revision = plan['revision']
                edited_at = runtime.db.execute(
                    'SELECT applied_at FROM vf_events WHERE revision=?', (revision,)).fetchone()[0]
                published_at = runtime.db.execute(
                    'SELECT published_at FROM vf_timelines WHERE revision=?', (revision,)).fetchone()[0]
                row = dict(policy=policy, plan=plan, reused=reused, video_outputs=len(jobs),
                           submission_intents=sum(job['submission_intents'] for job in jobs),
                           publication_seconds_after_edit=published_at - edited_at,
                           execution_spans_observed=len(spans),
                           provider_execution_span_seconds=sum(spans) if len(spans) == len(measured_jobs) else None,
                           provider_history=history, quality='requires_independent_review')
                if args.continuous:
                    publications = [r[0] for r in runtime.db.execute('SELECT revision FROM vf_timelines ORDER BY revision')]
                    events = [event for line in (folder / 'worker.jsonl').read_text().splitlines()
                              for event in json.loads(line)['events']]
                    stale = [event['job'] for event in events if event.get('result') == 'stale']
                    if publications != [1, 3] or not stale:
                        raise RuntimeError('continuous arm did not exercise obsolete completion and revision-3 publication')
                    row.update(repairs=repairs, final_selection=sorted(final_selection), publications=publications,
                               scheduled_jobs=len(jobs), video_outputs=sum(j['remote_state'] == 'succeeded'
                                                                          for j in measured_jobs),
                               stale_jobs=stale, obsolete_evidence_rejected=obsolete_evidence_rejected)
                write_json(folder / 'report.json', row)
                rows.append(row)
            finally:
                runtime.close()
        write_json(args.output / 'report.json', dict(evidence='real_H3_unreviewed_quality',
                   source_timeline_hash=before['timeline_hash'], rows=rows))
    finally:
        original.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--ollama', default='http://127.0.0.1:11434')
    parser.add_argument('--text-model', default='qwen2.5:14b')
    parser.add_argument('--vision-model', default='qwen2.5vl:7b')
    parser.add_argument('--policies', nargs='+', choices=tuple(POLICIES),
                        default=['declared', 'checklist', 'raw_union', 'egdr', 'full'])
    parser.add_argument('--continuous', action='store_true', help='run the two-edit deferred-repair experiment')
    parser.add_argument('--story', choices=tuple(STORIES), default='striped-press')
    parser.add_argument('--seed', type=int, default=88001)
    parser.add_argument('--frame-notes', type=Path,
                        help='optional independent observations from this seed, keyed by shot')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--source-only', action='store_true',
                        help='generate and archive only the shared pre-edit source')
    source.add_argument('--source-run', type=Path, help='reuse a completed --source-only run')
    parser.add_argument('--timeout', type=float, default=1800)
    parser.add_argument('--poll', type=float, default=3)
    args = parser.parse_args()
    if not all(0 < value < float('inf') for value in (args.poll, args.timeout)):
        parser.error('timeout and poll must be finite and positive')
    continuous_policies = {'worklist', 'online_egdr', 'full'}
    if args.continuous and not set(args.policies) <= continuous_policies:
        parser.error('--continuous requires --policies worklist online_egdr full (or a subset)')
    if not args.continuous and 'online_egdr' in args.policies:
        parser.error('online_egdr requires --continuous')
    if len(set(args.policies)) != len(args.policies):
        parser.error('policies must be unique')
    if not all(shutil.which(command) for command in ('ffmpeg', 'ffprobe')):
        parser.error('ffmpeg and ffprobe must be available')
    run(args)


if __name__ == '__main__':
    main()
