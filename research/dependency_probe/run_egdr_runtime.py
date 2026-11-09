"""Run a matched real-render EGDR pilot from one published source revision."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from artifacts import archive_current, write_json
from asset_runtime import AssetRuntime, fingerprint
from comfyui import ComfyUI
from provider_audit import capture_environment, summarize_inspections
from vision_probe import collect_selections, make_sheet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from video_edit_ablation import asset, wait_for


POLICIES = {
    'declared': 'attr_dag',
    'checklist': 'checklist_union',
    'raw_union': 'union_plus_vision',
    'egdr': 'union_plus_verified',
    'egdr_conservative': 'union_plus_unresolved',
    'full': 'full',
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
    # All arms share the same base prompt, including the input slot; only its
    # value changes where the edit is applied.
    manifest = dict(assets=[
        asset(shot, prompt + ' Apply the following appearance instruction where it applies. {{target.color}}',
              {KEY: 'Preserve the described original appearance.'}, seed + index)
        for index, (shot, prompt) in enumerate(shots.items())
    ])
    question = ('Do any of the four original panels show a ribbed amber handwheel '
                'belonging to the cream press with two teal stripes? The orange-diamond '
                'press does not count.')
    return manifest, case, question


def apply_policy(runtime, case, selected, source, evidence, policy):
    target = case.get('target', 'S1')
    repairs = [
        dict(child=shot, source_input_hash=source[shot]['input_hash'],
             evidence_sha256=fingerprint(dict(shot=shot, policy=policy, evidence=evidence)))
        for shot in sorted(set(selected) - {target})
    ]
    return runtime.edit(dict(id=case['id'], expected_revision=1, target=target, key=KEY,
                             value=case['edit'], scope='local', state_repairs=repairs))


def run(args):
    backend = ComfyUI(args.server)
    queue = backend._request('/queue')
    if any(queue.get(key) for key in ('queue_running', 'queue_pending')):
        raise RuntimeError('the experiment requires an idle, dedicated ComfyUI queue')
    manifest, case, question = pilot_inputs(args.seed)
    if args.frame_notes:
        notes = json.loads(args.frame_notes.read_text(encoding='utf-8'))
        if set(notes) != set(case['shots']) or any(not isinstance(note, str) or not note.strip()
                                                 for note in notes.values()):
            raise ValueError('provide one nonempty frame note per shot')
        case['shots'] = {shot: prompt + ' Frame observation: ' + notes[shot]
                         for shot, prompt in case['shots'].items()}
    args.output.mkdir(parents=True)
    write_json(args.output / 'protocol.json', dict(seed=args.seed, manifest=manifest, case=case,
               question=question, policies=args.policies, text_model=args.text_model,
               vision_model=args.vision_model, frame_notes=bool(args.frame_notes), timeout=args.timeout,
               server=backend.base))
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
        with (args.output / 'responses.jsonl').open('x', encoding='utf-8') as responses:
            def log(row):
                raw.append(row)
                responses.write(json.dumps(row, ensure_ascii=False) + '\n')
                responses.flush()

            started = time.monotonic()
            choices, audit = collect_selections(
                case, sheets, args.ollama, args.text_model, args.vision_model, log,
                visibility_question=question, verify_visual=True,
            )
            evidence_seconds = time.monotonic() - started
        if 'union_plus_verified' in choices:
            choices['union_plus_unresolved'] = (choices['union_plus_verified']
                                                | set(audit['visual_unknown'])
                                                | set(audit['verification_unknown']))
        write_json(args.output / 'evidence.json', dict(frames=frames, audit=audit,
                   paired_probe_wall_seconds=evidence_seconds, responses_sha256=fingerprint(raw),
                   selections={key: sorted(value) for key, value in choices.items()}))
        if audit['visual_invalid'] or audit['verification_invalid']:
            raise RuntimeError('invalid evidence replies; inspect responses.jsonl before rendering arms')
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
                plan = apply_policy(runtime, case, selected, source, evidence, policy)
                wait_for(folder, runtime, backend, 'videoflow', lambda: runtime.snapshot()['timeline'],
                         time.monotonic() + args.timeout, args.poll)
                after = archive_current(runtime, folder / 'media', backend.base, probe=True)
                reused = sorted(row['asset'] for row in after['assets']
                                if all(row[field] == source[row['asset']][field]
                                       for field in ('input_hash', 'sha256', 'output')))
                if set(reused) != set(source) - set(plan['regenerate']):
                    raise RuntimeError('published reuse disagrees with the committed plan')
                snapshot = runtime.snapshot()
                jobs = [job for job in snapshot['jobs'] if job['revision'] > 1]
                history = summarize_inspections(runtime)
                job_ids = {job['id'] for job in jobs}
                edited_history = [item for item in history['jobs'] if item['job'] in job_ids]
                spans = [item['execution_span_seconds'] for item in edited_history
                         if item['execution_span_seconds'] is not None]
                edited_at = runtime.db.execute('SELECT applied_at FROM vf_events').fetchone()[0]
                published_at = runtime.db.execute(
                    'SELECT published_at FROM vf_timelines WHERE revision=2').fetchone()[0]
                row = dict(policy=policy, plan=plan, reused=reused, video_outputs=len(jobs),
                           submission_intents=sum(job['submission_intents'] for job in jobs),
                           publication_seconds_after_edit=published_at - edited_at,
                           execution_spans_observed=len(spans),
                           provider_execution_span_seconds=sum(spans) if len(spans) == len(jobs) else None,
                           provider_history=history, quality='requires_independent_review')
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
    if len(set(args.policies)) != len(args.policies):
        parser.error('policies must be unique')
    if not all(shutil.which(command) for command in ('ffmpeg', 'ffprobe')):
        parser.error('ffmpeg and ffprobe must be available')
    run(args)


if __name__ == '__main__':
    main()
