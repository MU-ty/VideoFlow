"""Measure publication with live visual evidence and a matched replay."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import time

from run_joint import CANDIDATES, KEY, ROOT, STORIES, acquire, check, load_json, log, run_seed

POLICIES = ('ungated-worklist', 'gated-worklist')


def run_arm(args, api, root, seed_db, frozen, case, verifier, refs, backend, policy):
    root.mkdir()
    journal = root / 'events.jsonl'
    shared_path = root.parent / 'natural_evidence.json'
    live = not shared_path.exists()
    shared = None if live else load_json(shared_path)
    gated = policy == 'gated-worklist'

    class Runtime(api['AssetRuntime']):
        @staticmethod
        def _plan(assets, specs, edges, event):
            # The runtime pins deferred candidates through its DAIR edit interface.
            # Both arms use the same Worklist implementation for the closure.
            return api['AssetRuntime']._plan(assets, specs, edges, dict(event, method='attr_dag'))

        def _publication_pending(self, revision):
            return gated and super()._publication_pending(revision)

    class Transport:
        base = backend.base

        def inspect(self, prompt_id):
            result = backend.inspect(prompt_id)
            self.last_inspection = backend.last_inspection
            return result

        def submit(self, workflow, prompt_id):
            result = backend.submit(workflow, prompt_id)
            log(journal, 'provider_acceptance', prompt_id=prompt_id)
            return result

    db = root / 'project.db'
    with closing(sqlite3.connect(seed_db)) as source, closing(sqlite3.connect(db)) as dest:
        source.backup(dest)
    write = api['write_json']
    with closing(Runtime(db)) as runtime, ThreadPoolExecutor(max_workers=1) as pool:
        initial = {row['asset']: row for row in runtime.snapshot()['timeline']}
        frame_index = load_json(frozen / 'index.json')
        expected = {'S1', 'S3', *(child for _, child in case['edges'])}
        start_wall, start = time.time(), time.monotonic()
        plan = runtime.edit(dict(id='edit-2', expected_revision=1, target='S1',
                                key=KEY, value='scarlet red', method='dair',
                                evidence_candidates=list(CANDIDATES)))
        check(plan['regenerate'] == ['S1'], 'unexpected declared plan')
        log(journal, 'edit', plan=plan)
        future = pool.submit(acquire, args, api, frozen, verifier, case, 2,
                             root.parent / 'natural_vlm.jsonl') if live else None
        transport = Transport()
        applied_wall = None
        closure_ready_wall = None
        while time.monotonic() < start + args.timeout:
            if applied_wall is None:
                if future is not None and future.done() and shared is None:
                    evidence = future.result()
                    shared = dict(evidence=evidence, arrival_s=evidence['finished_s'] - start,
                                  acquired_in=policy)
                    write(shared_path, shared)
                if shared is not None and time.monotonic() >= start + shared['arrival_s']:
                    log(journal, 'evidence_ready', arrival_s=shared['arrival_s'])
                    shots = shared['evidence']['shots']
                    # Invalidate before resolving no-op verdicts so both arms see the same batch order.
                    for shot in sorted(shots, key=lambda name: shots[name]['decision'] == 'distinct'):
                        item = shots[shot]
                        proof = api['fingerprint'](dict(revision=2, anchor='S1', key=KEY, shot=shot,
                            source_input_hash=initial[shot]['input_hash'],
                            source_clip_sha256=frame_index[shot]['clip_sha256'], evidence=item))
                        result = runtime.resolve_evidence(2, shot, initial[shot]['input_hash'],
                                                          proof, item['decision'])
                        log(journal, 'evidence_applied', **result)
                    applied_wall = time.time()

            events = api['pass_once'](runtime, transport, build=refs.build, max_submit=0)
            free = max(0, 2 - len(runtime.unresolved_remote()))
            for job in runtime.pending()[:free]:
                for dep in runtime._inputs(job['asset'])['dependencies']:
                    if dep['kind'] == 'reference':
                        refs.stage(dep['output'])
            if free:
                events += api['pass_once'](api['SubmissionView'](runtime), transport,
                                           refs.build, max_submit=free)
            log(journal, 'worker', events=events)
            check(not any(e['action'] in ('failed', 'rejected', 'validation_error') for e in events),
                  f'worker failure: {events}')
            snapshot = runtime.snapshot()
            if (applied_wall is not None and snapshot['timeline']
                    and all(asset['output'] for asset in snapshot['assets'])
                    and not runtime.unresolved_remote()):
                closure_ready_wall = time.time()
                break
            time.sleep(args.poll)
        check(closure_ready_wall is not None, 'edit reached the publication deadline')

        published_at, = runtime.db.execute(
            'SELECT published_at FROM vf_timelines WHERE revision=2').fetchone()
        anchor, = [j for j in snapshot['jobs'] if j['asset'] == 'S1' and j['revision'] == 2]
        anchor_at = anchor['completed_at']
        archive = api['archive_current'](runtime, root / 'media', backend.base, probe=True)
        first = {row['asset']: row for row in archive['assets']}
        regenerated = {shot for shot, row in first.items() if row['input_hash'] != initial[shot]['input_hash']}
        materialized = {asset['id'] for asset in snapshot['assets']
                        if asset['committed_hash'] != initial[asset['id']]['input_hash']}
        for parent, _ in case['edges']:
            refs.stage(first[parent]['output'])
        jobs = {(j['asset'], j['revision']): j for j in snapshot['jobs']}
        lineage = api['reference_evidence']([first], jobs, refs, case['edges'])
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        missing = sorted(expected - regenerated)
        check(not gated or published_at >= start_wall + shared['arrival_s'],
              'gated arm published before recorded evidence arrival')
        report = dict(policy=policy, evidence_origin='live' if live else 'timed-replay',
                      shared_evidence_sha256=api['checksum'](shared_path),
                      evidence_arrival_s=shared['arrival_s'],
                      anchor_commit_s=anchor_at-start_wall,
                      evidence_minus_anchor_s=shared['arrival_s']-(anchor_at-start_wall),
                      evidence_applied_s=applied_wall-start_wall,
                      published_before_evidence=published_at < start_wall+shared['arrival_s'],
                      published_before_evidence_application_finished=published_at < applied_wall,
                      first_publication_s=published_at-start_wall,
                      first_complete_publication_s=None if missing else published_at-start_wall,
                      first_missing_targets=missing,
                      first_excess_updates=sorted(regenerated-expected),
                      first_updated_shots=sorted(regenerated),
                      first_reference_lineage=lineage,
                      closure_ready_s=closure_ready_wall-start_wall,
                      final_materialized_missing_targets=sorted(expected-materialized),
                      provider_acceptances=sum(e['action'] == 'provider_acceptance' for e in events),
                      evidence_decisions={shot: item['decision'] for shot, item in shots.items()},
                      anchor_provider_observation=next((row for row in
                          api['summarize_inspections'](runtime)['jobs']
                          if row['job'] == anchor['id']), None))
        write(root / 'report.json', report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--ollama', default='http://127.0.0.1:11434')
    parser.add_argument('--repo', default=ROOT / 'runtime', type=Path)
    parser.add_argument('--reuse-source-from', type=Path)
    parser.add_argument('--poll', type=float, default=1)
    parser.add_argument('--timeout', type=float, default=2400)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if args.poll <= 0 or args.timeout <= 0:
        parser.error('poll and timeout must be positive')
    args.policy = list(POLICIES)
    root = args.output
    root.mkdir(parents=True)
    reports = {}
    index = 0
    for story, spec in STORIES.items():
        for seed in spec['seeds']:
            block = f'{story}-{seed}'
            args.output = root / block
            order = POLICIES if index % 2 == 0 else POLICIES[::-1]
            index += 1
            case = dict(spec, story=story, seed=seed)
            try:
                run_seed(args, case, index, run_arm=run_arm, policies=order)
                if not args.prepare_only:
                    result = load_json(args.output / 'report.json')
                    hashes = {arm['shared_evidence_sha256'] for arm in result['policies']}
                    check(len(hashes) == 1, 'paired arms used different evidence')
                    reports[block] = result
            except (OSError, RuntimeError, ValueError) as exc:
                partial = args.output / 'report.json'
                policies = load_json(partial)['policies'] if partial.is_file() else []
                reports[block] = dict(status='failed', error=str(exc), policies=policies)
                print(f'{block}: {exc}', flush=True)
            if reports:
                (root / 'report.json').write_text(json.dumps(reports, indent=2) + '\n', encoding='utf-8')
    if any(result['status'] == 'failed' for result in reports.values()):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
