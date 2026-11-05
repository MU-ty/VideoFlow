"""Research-only DAIR ablations and a new matched ComfyUI editing trace.

Run "ablation" before spending GPU time on "comfy". Never use the unguarded
runtime outside the in-memory synthetic fault-injection fixture.
"""
import argparse
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asset_runtime
from artifacts import archive_current, write_json
from asset_runtime import AssetRuntime, encode
from asset_worker import pass_once
from comfyui import ComfyUI
from planning import execution_plan
from provider_audit import capture_environment, save_inspection, summarize_inspections
from recovery_comfy import reconcile
from recovery_matrix import SubmissionView


def asset(shot, template, values, seed):
    return dict(id=shot, scene=shot, kind='video',
                spec=dict(template=template, reads=list(values), values=values,
                          cost=1, config=dict(seed=seed)))


MANIFEST = {
    'assets': [
        asset('S1', 'Macro shot of a {{lamp.color}} paper lantern with a plain '
              'paper shade as a craftsperson assembles it at a wooden bench. '
              'The lantern color is visible, neutral light.', {'lamp.color': 'amber'}, 2027),
        asset('S2', 'A static close-up of an empty gray spool rack in a workshop. '
              'No lantern in this shot.', {}, 2028),
        asset('S3', 'Close-up of the same {{lamp.color}} paper lantern, with a '
              '{{shade.pattern}} paper shade, hanging in a night-market stall. '
              'The lantern color and shade pattern are clearly visible.',
              {'lamp.color': 'amber', 'shade.pattern': 'plain'}, 2029),
    ],
    'edges': [
        dict(parent='S1', child='S2', kind='order'),
        dict(parent='S1', child='S3', kind='state', keys=['lamp.color']),
    ],
}

EDITS = [
    dict(id='lantern-color', expected_revision=1, target='S1', key='lamp.color', value='cobalt-blue'),
    dict(id='shade-pattern', expected_revision=2, target='S3', key='shade.pattern', value='white-striped'),
]
EXPECTED = {
    'lantern-color': {'S1', 'S3'},
    'shade-pattern': {'S3'},
}
EXPECTED_JOB_KEYS = {('S1', 1), ('S2', 1), ('S3', 1), ('S1', 2), ('S3', 2), ('S3', 3)}
REVIEW = [
    {'shot': 'S1', 'criterion': 'Lantern changes from amber to cobalt blue.'},
    {'shot': 'S2', 'criterion': 'The initial spool-rack artifact is reused byte-for-byte.'},
    {'shot': 'S3', 'criterion': 'Lantern is cobalt blue and its shade has visible white stripes.'},
]


def selection(plan, expected):
    actual = set(plan['regenerate'])
    return dict(selected=sorted(actual), expected=sorted(expected),
                missed=sorted(expected - actual), excess=sorted(actual - expected))


def planner_variant(story, edit, method, relaxed_kind):
    # A planner-only projection makes the selected edge unconditional. The
    # runtime still sees the original graph for dependency readiness and values.
    edges = [dict(e, kind='reference') if e['kind'] == relaxed_kind else e
             for e in story['edges']]
    return execution_plan(dict(story, edges=edges), edit, method)


def planner_ablation():
    # The same source has a state edge and a pure order edge. Only one
    # propagation rule changes in each variant.
    cases = (
        ('matching_key', dict(EDITS[0]), {'S1', 'S3'}),
        ('unrelated_key', dict(EDITS[0], id='local-bench', key='bench.finish',
                               value='matte'), {'S1'}),
    )
    results = []
    for case, event, expected in cases:
        for label, relaxed_kind in (('full', None), ('no_key_filter', 'state'),
                                    ('no_edge_typing', 'order')):
            runtime = AssetRuntime(':memory:')
            try:
                fixture = dict(MANIFEST, assets=[
                    asset('S1', 'A {{lamp.color}} lantern on a {{bench.finish}} bench.',
                          {'lamp.color': 'amber', 'bench.finish': 'glossy'}, 2027),
                    *MANIFEST['assets'][1:],
                ])
                runtime.initialize(fixture)
                if relaxed_kind is None:
                    plan = runtime.edit(event)
                else:
                    def projected(story, edit, method):
                        return planner_variant(story, edit, method, relaxed_kind)
                    with patch.object(asset_runtime, 'execution_plan', projected):
                        plan = runtime.edit(event)
                results.append(dict(case=case, variant=label, **selection(plan, expected)))
            finally:
                runtime.close()
    return results


class WithoutCommitGuard(AssetRuntime):
    """Synthetic negative control: trust the returning job for its asset."""

    def complete(self, jid, external_id, output):
        with self.transaction():
            job = self.db.execute('SELECT * FROM vf_jobs WHERE id=? AND external_id=?',
                                  (jid, external_id)).fetchone()
            if job is None:
                raise ValueError('unknown task/external ID')
            self._observe_remote(job, 'succeeded', output)
            self.db.execute(
                "UPDATE vf_jobs SET status='succeeded',output=?,completed_at=? WHERE id=?",
                (output, time.time(), jid),
            )
            # Remove the whole revision/provenance barrier as one treatment:
            # accept a superseded result and publish without revalidation.
            self.db.execute('UPDATE vf_assets SET output=?,committed_hash=? WHERE id=?',
                            (output, job['input_hash'], job['asset']))
            assets = self._assets()
            if all(a['output'] is not None for a in assets):
                entries = [dict(asset=a['id'], scene=a['scene'], kind=a['kind'],
                                revision=a['revision'], input_hash=a['committed_hash'],
                                output=a['output']) for a in assets]
                self.db.execute('INSERT OR IGNORE INTO vf_timelines VALUES (?,?,?)',
                                (self._project()['revision'], encode(entries), time.time()))
            return 'applied'


def guard_ablation():
    results = []
    for label, cls in (('full', AssetRuntime), ('no_commit_guard', WithoutCommitGuard)):
        runtime = cls(':memory:')
        try:
            runtime.initialize(dict(assets=[MANIFEST['assets'][0]]))
            job = runtime.pending()[0]
            backend = 'http://synthetic.invalid'
            reserved = runtime.prepare(job['id'], backend,
                                       lambda inputs, _: inputs['spec']['values'])
            assert runtime.record_submission_intent(job['id'])
            runtime.mark(job['id'], 'submitted')
            runtime.edit(EDITS[0])
            assert runtime.snapshot()['timeline'] is None
            # The fake remote task deterministically completes after the edit.
            # Invoke the same public completion entry point as the worker.
            result = runtime.complete(job['id'], reserved['external_id'], 'synthetic:amber')
            snapshot = runtime.snapshot()
            observed = next(j for j in snapshot['jobs'] if j['id'] == job['id'])
            stale = [a['asset'] for a in (snapshot['timeline'] or [])
                     if a['output'] == 'synthetic:amber']
            assert observed['remote_state'] == 'succeeded'
            results.append(dict(variant=label, stale_arrivals=1, commit_result=result,
                                stale_publications=stale, revision=snapshot['revision']))
        finally:
            runtime.close()
    return results


def run_ablation():
    planner = planner_ablation()
    guard = guard_ablation()
    expected_excess = {
        ('matching_key', 'no_edge_typing'): ['S2'],
        ('unrelated_key', 'no_key_filter'): ['S3'],
        ('unrelated_key', 'no_edge_typing'): ['S2'],
    }
    passed = (all(not row['missed'] and row['excess'] ==
                  expected_excess.get((row['case'], row['variant']), []) for row in planner)
              and guard[0]['commit_result'] == 'stale' and not guard[0]['stale_publications']
              and guard[1]['stale_publications'] == ['S1'])
    return dict(complete=passed, provider='deterministic_synthetic', planner=planner, guard=guard)


def step(runtime, backend, policy):
    capacity = max(0, 2 - len(runtime.unresolved_remote()))
    if policy == 'strong_baseline':
        return (reconcile(runtime, backend)
                + pass_once(SubmissionView(runtime), backend, max_submit=capacity))
    return pass_once(runtime, backend, max_submit=capacity)


def save_step(root, runtime, backend, policy):
    events = step(runtime, backend, policy)
    with (root / 'worker.jsonl').open('a', encoding='utf-8') as out:
        out.write(json.dumps(dict(at=time.time(), events=events)) + '\n')
    failed = [e for e in events if e['action'] in ('failed', 'rejected', 'validation_error')
              or e.get('result') == 'failed']
    if failed:
        raise RuntimeError(f'worker reported a terminal failure: {failed}')


def wait_for(root, runtime, backend, policy, predicate, deadline, poll):
    while time.monotonic() < deadline:
        save_step(root, runtime, backend, policy)
        value = predicate()
        if value:
            return value
        time.sleep(poll)
    raise TimeoutError(f'timed out: {root}')


def inspect_publications(runtime, snapshot):
    jobs = {job['output']: job for job in snapshot['jobs'] if job['output']}
    expected_revisions = {
        1: {'S1': 1, 'S2': 1, 'S3': 1},
        2: {'S1': 2, 'S2': 1, 'S3': 2},
        3: {'S1': 2, 'S2': 1, 'S3': 3},
    }
    stale = []
    for pub in runtime.db.execute('SELECT * FROM vf_timelines ORDER BY revision'):
        for item in json.loads(pub['entries']):
            job = jobs.get(item['output'])
            expected = expected_revisions[pub['revision']][item['asset']]
            if job is None or job['asset'] != item['asset'] or job['revision'] != expected:
                stale.append(dict(project_revision=pub['revision'], asset=item['asset']))
    return stale


def run_comfy_trial(root, backend, policy, timeout, poll):
    root.mkdir()
    queue = backend._request('/queue')
    if any(queue.get(k) for k in ('queue_running', 'queue_pending')):
        raise RuntimeError('ComfyUI queue must be idle before each trial')
    db = root / 'project.db'
    runtime = AssetRuntime(str(db))
    deadline = time.monotonic() + timeout
    method = 'attr_dag' if policy == 'strong_baseline' else 'dair'
    wait = lambda predicate: wait_for(root, runtime, backend, policy, predicate, deadline, poll)

    def apply(event):
        plan = runtime.edit(dict(event, method=method))
        return selection(plan, EXPECTED[event['id']])

    def old_s3():
        for job in runtime.active():
            if job['asset'] == 'S3' and job['revision'] == 2:
                state, _ = backend.inspect(job['external_id'])
                save_inspection(runtime, backend, job)
                if state in ('succeeded', 'failed'):
                    raise RuntimeError('S3 revision 2 finished before the edit boundary')
                observation = (backend.last_inspection or {}).get('queue') or {}
                if state == 'running' and observation.get('queue_running'):
                    return job
                return None
        if runtime.snapshot()['timeline'] is not None:
            raise RuntimeError('revision 2 published before the in-flight edit boundary')
        return None

    try:
        runtime.initialize(MANIFEST)
        capture_environment(runtime, backend)
        wait(lambda: runtime.snapshot()['timeline'] and not runtime.unresolved_remote())
        before = archive_current(runtime, root / 'artifacts', backend.base)
        plans = {EDITS[0]['id']: apply(EDITS[0])}
        old = wait(old_s3)
        plans[EDITS[1]['id']] = apply(EDITS[1])
        write_json(root / 'boundary.json', dict(old_job=old['external_id'],
                                                provider_queue='queue_running', plans=plans))
        runtime.close()
        runtime = AssetRuntime(str(db))
        wait(lambda: runtime.snapshot()['timeline'] and not runtime.unresolved_remote())
        after = archive_current(runtime, root / 'artifacts', backend.base)
        snapshot = runtime.snapshot()
        stale = inspect_publications(runtime, snapshot)
        observed = summarize_inspections(runtime)
        history_ids = {j['job'] for j in observed['jobs'] if j['history_seen']}
        accepted = {(j['asset'], j['revision']) for j in snapshot['jobs']
                    if j['accepted_observed_at'] is not None or j['id'] in history_ids}
        old_job = next(j for j in snapshot['jobs'] if j['id'] == old['id'])
        pub = runtime.db.execute('SELECT published_at FROM vf_timelines WHERE revision=3').fetchone()
        edit = runtime.db.execute('SELECT applied_at FROM vf_events WHERE id=?',
                                  (EDITS[1]['id'],)).fetchone()
        initial = next(a for a in before['assets'] if a['asset'] == 'S2')
        final = next(a for a in after['assets'] if a['asset'] == 'S2')
        row = dict(
            policy=policy, planner=method, revision=snapshot['revision'], plans=plans,
            recovery='sqlite_reopen',
            accepted_jobs=sorted(accepted),
            missing_accepted_jobs=sorted(EXPECTED_JOB_KEYS - accepted),
            extra_accepted_jobs=sorted(accepted - EXPECTED_JOB_KEYS),
            stale_job_returned=old_job['remote_state'] == 'succeeded',
            stale_job_status=old_job['status'], stale_publications=stale,
            unresolved_jobs=len(runtime.unresolved_remote()),
            s2_reused=initial['sha256'] == final['sha256'] and initial['revision'] == final['revision'],
            seconds_from_local_edit_to_publication=pub[0] - edit[0],
            target_edit_visual_review='pending', inspections=observed,
        )
        row['runtime_checks_passed'] = (
            not stale and not row['unresolved_jobs'] and row['s2_reused']
            and row['stale_job_returned'] and row['stale_job_status'] == 'superseded'
            and not row['missing_accepted_jobs'] and not row['extra_accepted_jobs']
            and all(not p['missed'] and not p['excess'] for p in plans.values())
        )
        write_json(root / 'report.json', row)
        return row
    finally:
        runtime.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('ablation', 'comfy'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--timeout', type=float, default=3600)
    parser.add_argument('--poll', type=float, default=2)
    args = parser.parse_args()
    if min(args.repeat, args.timeout, args.poll) <= 0:
        parser.error('repeat, timeout and poll must be positive')
    args.output.mkdir(parents=True)
    if args.mode == 'ablation':
        report = run_ablation()
        write_json(args.output / 'report.json', report)
        print(json.dumps(report, indent=2))
        if not report['complete']:
            raise SystemExit(1)
        return

    backend = ComfyUI(args.server)
    policies = ('videoflow', 'strong_baseline')
    report = dict(complete=False, story='night_market_lantern', review=REVIEW,
                  evidence='remote_comfyui_unvalidated_quality', rows=[])
    write_json(args.output / 'protocol.json', dict(
        manifest=MANIFEST, edits=EDITS,
        expected={key: sorted(value) for key, value in EXPECTED.items()},
        expected_jobs=sorted(EXPECTED_JOB_KEYS), review=REVIEW,
        repeat=args.repeat, poll=args.poll, timeout=args.timeout,
    ))
    for rep in range(args.repeat):
        for policy in (policies if rep % 2 == 0 else reversed(policies)):
            print(f'Pair {rep + 1}: {policy}', flush=True)
            root = args.output / f'{rep + 1}-{policy}'
            try:
                row = run_comfy_trial(root, backend, policy, args.timeout, args.poll)
            except Exception as exc:
                report.update(error=dict(pair=rep + 1, policy=policy, detail=str(exc)))
                write_json(args.output / 'report.json', report)
                raise
            report['rows'].append(dict(pair=rep + 1, **row))
            write_json(args.output / 'report.json', report)
    report['complete'] = True
    report['runtime_checks_passed'] = all(row['runtime_checks_passed'] for row in report['rows'])
    write_json(args.output / 'report.json', report)
    if not report['runtime_checks_passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
