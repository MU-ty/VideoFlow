"""Replay the three archived H3 policies with matched, corrected target references."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time

sys.path[:0] = [str(Path(__file__).resolve().parents[i]) for i in (2, 1)]
from artifacts import archive_current, checksum, write_json
from asset_runtime import AssetRuntime, Conflict, fingerprint
from asset_worker import build_h3, pass_once
from comfyui import ComfyUI
from provider_audit import capture_environment, summarize_inspections
from recovery_reference_comfy import upload_frame
from run_egdr_reference_pair import check_backend, condition


ARMS = {
    'worklist': (['S1'], 2),
    'online_egdr': (['S1', 'S2'], 3),
    'full': (['S1', 'S2', 'S3', 'S4'], 5),
}


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def verified_row(root, report, shot, style, reference):
    rows = [r for r in report['rows'] if (r['shot'], r['style']) == (shot, style)]
    if len(rows) != 1:
        raise ValueError(f'expected one archived render for {shot}/{style}')
    row = rows[0]
    graph = row['workflow']
    image = root / reference['file']
    if (row['state'] != 'succeeded' or checksum(image) != reference['sha256']
            or graph != row['inspection']['history_entry']['prompt'][2]
            or graph['5']['inputs'].get('first_frame') != ['15', 0]
            or not graph['15']['inputs']['image'].endswith('/' + reference['sha256'] + '.png')):
        raise ValueError(f'unverified archived reference or workflow for {shot}')
    return image, reference['sha256'], graph


def load_inputs(args):
    old = args.online_run
    protocol = load(old / 'protocol.json')
    source = load(old / 'source' / 'revision-1.json')
    pair = load(args.pair_run / 'report.json')
    followup = load(args.followup_run / 'report.json')
    fp = followup['protocol']
    prior = next(row for row in pair['rows'] if row['prompt_id'] == fp['previous_prompt_id'])
    if ((prior['shot'], prior['style'], prior['sha256'], prior['conditioning_image'])
            != ('S1', 'red_wheel_frame', fp['previous_output_sha256'], fp['previous_reference'])):
        raise ValueError('S1 follow-up is not linked to the supplied reference pair')
    for entry in source['assets']:
        shot = entry['asset']
        if checksum(old / 'source' / entry['file']) != entry['sha256']:
            raise ValueError(f'source artifact changed for {shot}')
        if shot in ('S1', 'S2') and pair['protocol']['frames'][shot]['source_video_sha256'] != entry['sha256']:
            raise ValueError(f'reference belongs to a different source video for {shot}')
    refs = {
        'S1': verified_row(args.followup_run, followup, 'S1', 'inner_rim_corrected',
                           fp['corrected_reference']),
        'S2': verified_row(args.pair_run, pair, 'S2', 'red_wheel_frame',
                           pair['protocol']['frames']['S2']['images']['red_wheel_frame']),
    }
    plans = {}
    for arm in ARMS:
        path = (old / arm / 'project.db').resolve()
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            jobs = {(row['asset'], row['revision']): dict(row) for row in db.execute(
                'SELECT asset,revision,execution_json,input_hash,output,backend FROM vf_jobs')}
            edits = [json.loads(row['payload']) for row in db.execute(
                'SELECT payload FROM vf_events ORDER BY revision')]
            evidence = [dict(row) for row in db.execute(
                'SELECT child,source_input_hash,evidence_sha256,decision FROM vf_evidence '
                'WHERE revision=3 ORDER BY child')]
            if len(edits) != 2 or (arm == 'online_egdr') != bool(evidence):
                raise ValueError(f'incomplete archived event trace for {arm}')
            plans[arm] = (jobs, edits, evidence)
    return protocol, source, refs, plans


def restore_source(runtime, manifest, source, jobs, backend):
    runtime.initialize(manifest)
    pending = {row['asset']: row for row in runtime.pending()}
    for entry in source['assets']:
        shot = entry['asset']
        old = jobs[(shot, 1)]
        graph = json.loads(old['execution_json'])
        if (old['input_hash'], old['output'], old['backend']) != (
                entry['input_hash'], entry['output'], backend):
            raise ValueError(f'archived source ledger mismatch for {shot}')
        # Rehydrate a verified, already published artifact without a remote POST.
        reserved = runtime.prepare(pending[shot]['id'], backend, lambda _inputs, _id: graph)
        if reserved['input_hash'] != entry['input_hash']:
            raise ValueError(f'restored source provenance mismatch for {shot}')
        if runtime.complete(reserved['id'], reserved['external_id'], entry['output']) != 'applied':
            raise RuntimeError(f'could not restore source {shot}')
    if fingerprint(runtime.snapshot()['timeline']) != source['timeline_hash']:
        raise RuntimeError('restored source timeline differs from the archive')


def render_settings(graph):
    """Settings independent of prompt, first frame and output destination."""
    return {
        node: dict(spec, inputs={k: v for k, v in spec['inputs'].items()
                                if (node, k) not in {('5', 'prompt'), ('5', 'first_frame'),
                                                     ('14', 'filename_prefix')}})
        for node, spec in graph.items() if node != '15'
    }


def builder(jobs, refs):
    def build(inputs, job_id):
        shot = inputs['asset']
        graph = build_h3(inputs, job_id)
        old = json.loads(jobs[(shot, inputs['revision'])]['execution_json'])
        if (render_settings(graph) != render_settings(old)
                or graph['5']['inputs']['prompt'] != old['5']['inputs']['prompt']):
            raise ValueError(f'base workflow differs from the archived trace for {shot}')
        if shot in refs:
            name, diagnostic = refs[shot]
            if render_settings(graph) != render_settings(diagnostic):
                raise ValueError(f'archived reference render settings differ for {shot}')
            graph['5']['inputs']['prompt'] = diagnostic['5']['inputs']['prompt']
            graph = condition(graph, name)
        return graph
    return build


def step(folder, runtime, backend, build, limit):
    events = pass_once(runtime, backend, build=build, max_submit=limit)
    with (folder / 'worker.jsonl').open('a', encoding='utf-8') as log:
        log.write(json.dumps(dict(at=time.time(), events=events)) + '\n')
    if any(e['action'] in ('failed', 'rejected', 'validation_error') for e in events):
        raise RuntimeError(f'worker rejected the replay: {events}')
    return events


def anchor(folder, runtime, backend, build, epoch):
    events = step(folder, runtime, backend, build, 1)
    if not any(j['asset'] == 'S1' and j['revision'] == epoch and j['status'] == 'submitted'
               for j in runtime.active()):
        raise RuntimeError(f'S1 epoch {epoch} did not reach the acknowledgement barrier')
    return events


def replay_arm(args, backend, protocol, source, jobs, edits, evidence, refs, arm):
    folder = args.output / arm
    folder.mkdir()
    db_path = folder / 'project.db'
    with closing(AssetRuntime(db_path)) as runtime:
        restore_source(runtime, protocol['manifest'], source, jobs, backend.base)
        capture_environment(runtime, backend)
        first = runtime.edit(edits[0])
        anchor(folder, runtime, backend, builder(jobs, {}), 2)
    with closing(AssetRuntime(db_path)) as runtime:
        second = runtime.edit(edits[1])
        final_builder = builder(jobs, refs)
        barrier = anchor(folder, runtime, backend, final_builder, 3)
        stale_evidence_rejected = False
        if evidence:
            try:
                runtime.resolve_evidence(2, **evidence[0])
            except Conflict:
                stale_evidence_rejected = True
            else:
                raise RuntimeError('superseded evidence was accepted')
        repairs = [runtime.resolve_evidence(3, **row) for row in evidence]
        write_json(folder / 'repair_trace.json', dict(first=first, second=second, repairs=repairs,
                   obsolete_evidence_rejected=stale_evidence_rejected))

        deadline = time.monotonic() + args.timeout
        stale = [e['job'] for e in barrier if e.get('result') == 'stale']
        while time.monotonic() < deadline:
            capacity = max(0, 2 - len(runtime.unresolved_remote()))
            events = step(folder, runtime, backend, final_builder, capacity)
            stale.extend(e['job'] for e in events if e.get('result') == 'stale')
            if runtime.snapshot()['timeline'] and not runtime.unresolved_remote():
                break
            time.sleep(args.poll)
        else:
            raise TimeoutError(f'{arm} did not publish and drain by the deadline')

        # Serve the frozen source bytes locally for reuse. Fetch all new outputs from ComfyUI.
        media = folder / 'media'
        media.mkdir()
        for entry in source['assets']:
            shutil.copyfile(args.online_run / 'source' / entry['file'], media / entry['file'])
        write_json(media / 'revision-1.json', source)
        after = archive_current(runtime, media, backend.base, probe=True)
        old = {entry['asset']: entry for entry in source['assets']}
        reused = sorted(entry['asset'] for entry in after['assets']
                        if all(entry[k] == old[entry['asset']][k] for k in ('input_hash', 'output', 'sha256')))
        regenerated = sorted(set(old) - set(reused))
        snapshot = runtime.snapshot()
        new = [j for j in snapshot['jobs'] if j['revision'] > 1]
        intents = sum(j['submission_intents'] for j in new)
        publications = [r[0] for r in runtime.db.execute('SELECT revision FROM vf_timelines ORDER BY revision')]
        if (regenerated, intents) != ARMS[arm] or publications != [1, 3] or len(stale) != 1:
            raise RuntimeError(f'{arm} does not match the archived policy schedule')
        histories = {}
        for inspection in runtime.db.execute('SELECT job,payload FROM vf_provider_inspections ORDER BY id'):
            observation = json.loads(inspection['payload'])
            entry = observation.get('history_entry')
            if entry and entry.get('status', {}).get('status_str') == 'success':
                histories[inspection['job']] = entry
        bound = {}
        for j in new:
            if j['status'] != 'succeeded':
                continue
            shot = j['asset']
            actual = json.loads(j['execution_json'])
            if histories.get(j['id'], {}).get('prompt', [None, None, None])[2] != actual:
                raise RuntimeError(f'provider history does not match the published workflow for {shot}')
            if shot in refs:
                expected = refs[shot][0]
                if actual['15']['inputs']['image'] != expected:
                    raise RuntimeError(f'wrong published reference for {shot}')
                bound[shot] = expected
            elif 'first_frame' in actual['5']['inputs']:
                raise RuntimeError(f'unexpected reference for {shot}')
        return dict(policy=arm, regenerated=regenerated, byte_reused=reused,
                    submission_intents=intents, publications=publications,
                    stale_results=stale, obsolete_evidence_rejected=stale_evidence_rejected,
                    reference_bindings=bound, provider_history=summarize_inspections(runtime),
                    visual_acceptance='pending_manual_review')


def run(args, backend):
    protocol, source, refs, plans = load_inputs(args)
    if protocol['server'] != backend.base:
        raise ValueError('use the archived experiment backend URL')
    check_backend(backend)
    args.output.mkdir(parents=True)
    bindings = {}
    for shot, (image, digest, graph) in refs.items():
        shutil.copyfile(image, args.output / f'{shot}-reference.png')
        bindings[shot] = (upload_frame(backend, image, digest), graph)
    summary = dict(experiment='conditioned_closed_loop_replay', source_timeline_hash=source['timeline_hash'],
                   source_run=args.online_run.name, reference_pair=args.pair_run.name,
                   followup=args.followup_run.name, evidence_mode='replay_frozen_verdicts',
                   visual_oracle={
                       'S1': 'Target wheel red from first visibility through last visibility',
                       'S2': 'Target wheel red from first visibility through last visibility',
                       'S3': 'Orange-diamond press and its amber wheel remain unchanged',
                       'S4': 'Toolbox remains unchanged, with no press or handwheel added',
                   },
                   references={shot: dict(sha256=ref[1], file=f'{shot}-reference.png')
                               for shot, ref in refs.items()}, rows=[])
    write_json(args.output / 'report.json', summary)
    for arm in ARMS:
        print(f'START {arm}', flush=True)
        row = replay_arm(args, backend, protocol, source, *plans[arm], bindings, arm)
        write_json(args.output / arm / 'report.json', row)
        summary['rows'].append(row)
        write_json(args.output / 'report.json', summary)
        print(f"DONE {arm}: {row['submission_intents']} intents, reused {row['byte_reused']}", flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('online-run', 'pair-run', 'followup-run', 'output'):
        parser.add_argument('--' + option, required=True, type=Path)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--timeout', default=1800, type=float)
    parser.add_argument('--poll', default=3, type=float)
    args = parser.parse_args()
    if not all(0 < value < float('inf') for value in (args.timeout, args.poll)):
        parser.error('timeout and poll must be finite and positive')
    run(args, ComfyUI(args.server))
