"""Isolate the archived lamp fallback with an oracle-assisted SQLite replay."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from run_egdr_conditioned_replay import load, restore_source
from artifacts import checksum
from asset_runtime import AssetRuntime, Conflict, fingerprint


def verified_media(root, manifest):
    media = {}
    for entry in manifest['assets']:
        asset = entry['asset']
        if checksum(root / entry['file']) != entry['sha256']:
            raise ValueError(f'archived media bytes differ for {asset}')
        media[asset] = entry
    return media


def read_trace(root):
    protocol = load(root / 'protocol.json')
    oracle = load(root / 'visual_oracle.json')
    probe_archive = load(root / 'evidence.json')
    responses = [json.loads(line) for line in (root / 'responses.jsonl').read_text().splitlines()]
    if fingerprint(responses) != probe_archive['responses_sha256']:
        raise ValueError('archived VLM responses do not match the evidence report')
    probes = probe_archive['audit']['probes']
    if (protocol['story'], protocol['seed'], oracle['target_shots'], probes['S2']['decision']) != (
            'hexagon-lamp', 88101, ['S1', 'S3'], 'unknown'):
        raise ValueError('expected the predeclared lamp case and its archived S2 unknown')
    arm = root / 'online_egdr'
    db = arm / 'project.db'
    with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        edits = [json.loads(row['payload']) for row in conn.execute(
            'SELECT payload FROM vf_events ORDER BY revision')]
        jobs = {row['id']: dict(row) for row in conn.execute('SELECT * FROM vf_jobs')}
        evidence = [dict(row) for row in conn.execute(
            'SELECT child,source_input_hash,evidence_sha256,decision FROM vf_evidence '
            'WHERE revision=3 ORDER BY child')]
    if {r['child']: r['decision'] for r in evidence} != {
            'S2': 'fallback', 'S3': 'match', 'S4': 'distinct'}:
        raise ValueError('archived decisions do not match this ablation')
    events = [e for line in (arm / 'worker.jsonl').read_text().splitlines()
              for e in json.loads(line)['events']]
    if len(edits) != 2 or len(events) < 2:
        raise ValueError('incomplete continuous edit trace')
    for i, event in enumerate(events[:2], start=2):
        job = jobs[event['job']]
        if (event['action'], job['asset'], job['revision']) != ('submitted', 'S1', i):
            raise ValueError('missing two-edit acknowledgement barrier')
    source = load(root / 'source' / 'revision-1.json')
    final = load(arm / 'media' / 'revision-3.json')
    source_media = verified_media(root / 'source', source)
    final_media = verified_media(arm / 'media', final)
    return protocol, oracle, source, source_media, final, final_media, edits, jobs, evidence, events


def replay(trace, db_path, *, adjudicate_s2):
    protocol, oracle, source, old_media, final, final_media, edits, jobs, evidence, events = trace
    backend = protocol['server']
    scheduled = {}
    stale = 0

    def deliver_event(runtime, event):
        old = jobs[event['job']]
        if adjudicate_s2 and old['asset'] == 'S2':
            return
        if event['action'] == 'submitted':
            job = next((j for j in runtime.pending()
                        if (j['asset'], j['revision']) == (old['asset'], old['revision'])), None)
            if job is None:
                raise RuntimeError(f"archived submission is not ready: {old['asset']}")
            graph = json.loads(old['execution_json'])
            new = runtime.prepare(job['id'], backend, lambda _inputs, _id: graph)
            if new['input_hash'] != old['input_hash'] or not runtime.record_submission_intent(new['id']):
                raise RuntimeError('replayed execution provenance does not match')
            runtime.mark(new['id'], 'submitted')
            scheduled[event['job']] = new
        elif event['action'] == 'complete':
            new = scheduled[event['job']]
            result = runtime.complete(new['id'], new['external_id'], old['remote_output'])
            if result != event['result']:
                raise RuntimeError('replayed completion disagrees with the archived trace')
        else:
            raise ValueError(f"unexpected archived event: {event['action']}")

    with closing(AssetRuntime(db_path)) as runtime:
        restore_source(runtime, protocol['manifest'], source,
                       {(r['asset'], r['revision']): r for r in jobs.values()}, backend)
        runtime.edit(edits[0])
        deliver_event(runtime, events[0])
    with closing(AssetRuntime(db_path)) as runtime:
        runtime.edit(edits[1])
        deliver_event(runtime, events[1])
        for index, archived in enumerate(evidence):
            row = dict(archived)
            if adjudicate_s2 and row['child'] == 'S2':
                row['decision'] = 'distinct'
                row['evidence_sha256'] = fingerprint(dict(
                    kind='predeclared_human_oracle', source_sha256=old_media['S2']['sha256'],
                    evidence_sha256=archived['evidence_sha256'], annotation=oracle['source']['S2'],
                ))
            if index == 0:
                try:
                    runtime.resolve_evidence(2, **row)
                except Conflict:
                    pass
                else:
                    raise RuntimeError('superseded evidence was accepted')
            runtime.resolve_evidence(3, **row)
        for event in events[2:]:
            deliver_event(runtime, event)
            stale += event.get('result') == 'stale'
        snapshot = runtime.snapshot()
        publications = [r[0] for r in runtime.db.execute(
            'SELECT revision FROM vf_timelines ORDER BY revision')]
        if publications != [1, 3] or stale != 1:
            raise RuntimeError('publication history differs from the archived trace')
        if not adjudicate_s2 and fingerprint(snapshot['timeline']) != final['timeline_hash']:
            raise RuntimeError('baseline fails to reconstruct the exact archived timeline')

        reused, hashes = [], {}
        for entry in snapshot['timeline']:
            shot = entry['asset']
            original = old_media[shot]
            expected = original if shot == 'S2' and adjudicate_s2 else final_media[shot]
            if any(entry[k] != expected[k] for k in ('revision', 'input_hash', 'output')):
                raise RuntimeError(f'published entry does not match its archived media: {shot}')
            hashes[shot] = expected['sha256']
            if hashes[shot] == original['sha256']:
                reused.append(shot)
        intents = sum(j['submission_intents'] for j in snapshot['jobs'] if j['revision'] > 1)
    return dict(replayed_submission_intents=intents, byte_reused=sorted(reused),
                final_asset_sha256=hashes, publications=publications, stale_results=stale)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path, help='extracted lamp GPU run directory')
    args = parser.parse_args()
    trace = read_trace(args.run)
    with TemporaryDirectory() as directory:
        root = Path(directory)
        baseline = replay(trace, root / 'baseline.db', adjudicate_s2=False)
        intervention = replay(trace, root / 'intervention.db', adjudicate_s2=True)
    print(json.dumps(dict(
        scope='oracle_assisted_archive_replay_no_provider_calls',
        changed_decision='S2: fallback to distinct using the predeclared human label',
        archived_policy=baseline, oracle_adjudicated=intervention,
    ), indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
