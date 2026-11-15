"""Controlled comparison of recovery policies using the production worker."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from asset_runtime import AssetRuntime, fingerprint
from asset_worker import pass_once


TRACES = {
    'single_local': [(1, 'S2', 'blue')],
    'repeat_local': [(1, 'S2', 'blue'), (3, 'S2', 'red')],
    'staggered_shots': [(1, 'S2', 'blue'), (4, 'S4', 'green')],
}
FAULTS = ('before_edit', 'after_edit', 'lost_ack_queryable', 'lost_ack_opaque')
POLICIES = ('project_version', 'fingerprint', 'videoflow')
HORIZON = 30


class LostResponse(Exception):
    pass


class Provider:
    base = 'http://controlled.invalid'

    def __init__(self, fault):
        self.fault = fault
        self.tick = 0
        self.jobs = {}

    def submit(self, execution, requested_id):
        lost = self.tick == 0 and execution['shot'] == 'S4' and self.fault.startswith('lost_ack')
        actual_id = 'opaque-' + requested_id if lost and self.fault == 'lost_ack_opaque' else requested_id
        assert actual_id not in self.jobs
        self.jobs[actual_id] = dict(execution, accepted=self.tick,
                                    done=self.tick + (5 if execution['shot'] == 'S4' else 3))
        if lost:
            raise LostResponse
        return actual_id

    def inspect(self, identifier):
        job = self.jobs.get(identifier)
        if job is None:
            return 'unknown', None
        if self.tick < job['done']:
            return 'running', None
        return 'succeeded', identifier


def build(inputs, _job_id):
    return dict(shot=inputs['asset'], color=inputs['spec']['values']['color'])


def reject_old_project_work(runtime):
    with runtime.transaction():
        for job in runtime.active():
            runtime.db.execute("UPDATE vf_jobs SET status='superseded' WHERE id=?", (job['id'],))
            runtime.db.execute("UPDATE vf_assets SET revision=revision+1 WHERE id=?", (job['asset'],))
            runtime._job(job['asset'], job['revision'] + 1)


class SubmissionView:
    def __init__(self, runtime):
        self.runtime = runtime

    def __getattr__(self, name):
        return getattr(self.runtime, name)

    def active(self, include_superseded=False):
        return []


def reconcile_by_fingerprint(runtime, provider):
    for job in runtime.active(include_superseded=True):
        status, output = provider.inspect(job['external_id'])
        if status == 'succeeded':
            digest = fingerprint(dict(inputs=runtime._inputs(job['asset']), backend=provider.base,
                                      execution=json.loads(job['execution_json'])))
            if job['status'] != 'superseded' and digest != job['input_hash']:
                raise AssertionError('live job with outdated input fingerprint')
            runtime.complete(job['id'], job['external_id'], output)
        else:
            runtime.mark(job['id'], 'submitted' if status == 'running' else 'unknown')


def run(path, policy, fault, edits):
    provider = Provider(fault)
    runtime = AssetRuntime(str(path))
    expected = {'S2': 'white', 'S4': 'white'}
    runtime.initialize({'assets': [
        dict(id=shot, scene=shot, kind='video',
             spec=dict(template='{{color}}', reads=['color'], values={'color': color}, cost=1))
        for shot, color in expected.items()
    ]})
    try:
        try:
            pass_once(runtime, provider, build=build, max_submit=2)
        except LostResponse:
            if not fault.startswith('lost_ack'):
                raise
        assert len(provider.jobs) == 2
        revision = 1
        wrong = set()
        ready = None
        for tick in range(1, HORIZON + 1):
            provider.tick = tick
            if tick == 1 and fault != 'after_edit':
                runtime.close()
                runtime = AssetRuntime(str(path))
            for at, shot, color in edits:
                if at != tick:
                    continue
                plan = runtime.edit(dict(id=f'edit-{tick}', expected_revision=revision,
                                         method='attr_dag', target=shot, key='color', value=color))
                assert plan['regenerate'] == [shot]
                expected[shot] = color
                revision += 1
                if policy == 'project_version':
                    reject_old_project_work(runtime)
            if tick == 1 and fault == 'after_edit':
                runtime.close()
                runtime = AssetRuntime(str(path))
            capacity = max(0, 2 - len(runtime.unresolved_remote()))
            view = runtime
            if policy == 'fingerprint':
                reconcile_by_fingerprint(runtime, provider)
                view = SubmissionView(runtime)
            pass_once(view, provider, build=build, max_submit=capacity)
            timeline = runtime.snapshot()['timeline']
            if timeline is None:
                continue
            bad = {entry['asset'] for entry in timeline
                   if provider.jobs[entry['output']]['color'] != expected[entry['asset']]}
            wrong.update((revision, shot) for shot in bad)
            if not bad and tick >= edits[-1][0] and ready is None:
                ready = tick - edits[-1][0]
        return dict(ready_ticks=ready, stale_publications=len(wrong),
                    unresolved=len(runtime.unresolved_remote()),
                    acceptances=len(provider.jobs), provider_ledger=list(provider.jobs.values()))
    finally:
        runtime.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    rows = []
    with tempfile.TemporaryDirectory() as directory:
        for trace, edits in TRACES.items():
            for fault in FAULTS:
                for policy in POLICIES:
                    path = Path(directory) / f'{trace}-{fault}-{policy}.db'
                    rows.append(dict(trace=trace, fault=fault, policy=policy,
                                     **run(path, policy, fault, edits)))
    report = dict(horizon=HORIZON, traces=TRACES, rows=rows,
                  scope='Synthetic provider and clock. Restart reopens SQLite within the same process.')
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')


if __name__ == '__main__':
    main()
