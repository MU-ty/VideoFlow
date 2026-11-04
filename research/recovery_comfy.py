"""Run paired client-SIGKILL recovery trials against MiniMax H3 on ComfyUI."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from artifacts import archive_current, write_json
from asset_runtime import AssetRuntime, fingerprint
from asset_worker import pass_once
from comfyui import ComfyUI
from provider_audit import capture_environment, save_inspection, summarize_inspections
from recovery_matrix import POLICIES, SubmissionView, reject_old_project_work


TRACES = {
    'repeat_local': [('S2', 'blue'), ('S2', 'red')],
    'staggered_shots': [('S2', 'blue'), ('S4', 'green')],
}
TEMPLATES = {
    'S2': 'Static close-up of a {{color}} ceramic mug on a wooden table, natural light.',
    'S4': 'Static close-up of a {{color}} fabric backpack on a bench, natural light.',
}
SEEDS = {'S2': 42, 'S4': 314}


def log_pass(root, events):
    with (root / 'worker.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'at': time.time(), 'events': events}) + '\n')


def prepare(root, backend, policy, trace):
    runtime = AssetRuntime(str(root / 'project.db'))
    try:
        runtime.initialize({'assets': [
            dict(id=shot, scene=shot, kind='video', spec=dict(
                template=template, reads=['color'], values={'color': 'white'},
                cost=1, config={'seed': SEEDS[shot]}))
            for shot, template in TEMPLATES.items()
        ]})
        capture_environment(runtime, backend)
        log_pass(root, pass_once(runtime, backend, max_submit=2))
        jobs = runtime.active()
        if len(jobs) != 2:
            raise RuntimeError('initial submissions did not both reach the worker ledger')
        for job in jobs:
            state, _ = backend.inspect(job['external_id'])
            save_inspection(runtime, backend, job)
            if state != 'running':
                raise RuntimeError(f"initial job {job['asset']} is not in flight: {state}")
        for index, (shot, color) in enumerate(TRACES[trace], start=1):
            plan = runtime.edit(dict(id=f'edit-{index}', expected_revision=index,
                                     method='attr_dag', target=shot, key='color', value=color))
            if plan['regenerate'] != [shot]:
                raise RuntimeError(f'unexpected edit plan: {plan}')
            if policy == 'project_version':
                reject_old_project_work(runtime)
        for job in jobs:
            state, _ = backend.inspect(job['external_id'])
            save_inspection(runtime, backend, job)
            if state != 'running':
                raise RuntimeError(f"job {job['asset']} finished before the restart barrier")
        write_json(root / 'boundary.json', {
            'jobs': [{'asset': job['asset'], 'id': job['external_id']} for job in jobs],
            'pid': os.getpid(),
            'revision': runtime.snapshot()['revision'],
        })
        while True:
            time.sleep(1)
    finally:
        runtime.close()


def reconcile(runtime, backend):
    events = []
    for job in runtime.active(include_superseded=True):
        state, value = backend.inspect(job['external_id'])
        save_inspection(runtime, backend, job)
        if state == 'succeeded':
            digest = fingerprint(dict(inputs=runtime._inputs(job['asset']), backend=backend.base,
                                      execution=json.loads(job['execution_json'])))
            if job['status'] != 'superseded' and digest != job['input_hash']:
                raise RuntimeError('live job has outdated input fingerprint')
            result = runtime.complete(job['id'], job['external_id'], value)
        else:
            result = {'running': 'submitted', 'failed': 'failed'}.get(state, 'unknown')
            runtime.mark(job['id'], result, str(value) if state == 'failed' else None)
        events.append(dict(job=job['id'], action='reconcile', result=result))
    return events


def run_trial(root, backend, policy, trace, poll, timeout):
    root.mkdir()
    queue = backend._request("/queue")
    if any(queue.get(field) for field in ("queue_running", "queue_pending")):
        raise RuntimeError('ComfyUI queue must be idle before each trial')
    with (root / 'client.log').open('w', encoding='utf-8') as log:
        with subprocess.Popen([
            sys.executable, __file__, '--server', backend.base, '--output', str(root),
            '--prepare', policy, trace,
        ], stdout=log, stderr=subprocess.STDOUT) as child:
            deadline = time.monotonic() + timeout
            try:
                while not (root / 'boundary.json').exists():
                    if child.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError(f'restart barrier not reached; see {root}/client.log')
                    time.sleep(0.1)
            finally:
                if child.poll() is None:
                    child.kill()
            child.wait()
            if child.returncode != -9:
                raise RuntimeError('expected client SIGKILL')
    restarted = time.time()
    runtime = AssetRuntime(str(root / 'project.db'))
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            capacity = max(0, 2 - len(runtime.unresolved_remote()))
            view = runtime
            if policy == 'fingerprint':
                log_pass(root, reconcile(runtime, backend))
                view = SubmissionView(runtime)
            log_pass(root, pass_once(view, backend, max_submit=capacity))
            snapshot = runtime.snapshot()
            unresolved = runtime.unresolved_remote()
            if not unresolved and (snapshot['timeline'] or any(
                    job['status'] == 'failed' for job in snapshot['jobs'])):
                break
            time.sleep(poll)
        snapshot = runtime.snapshot()
        unresolved = runtime.unresolved_remote()
        expected = dict.fromkeys(TEMPLATES, 'white')
        expected.update(TRACES[trace])
        jobs = {(job['asset'], job['revision']): job for job in snapshot['jobs']}
        stale = []
        for published in runtime.db.execute('SELECT revision,entries FROM vf_timelines'):
            for item in json.loads(published['entries']):
                job = jobs[(item['asset'], item['revision'])]
                actual = json.loads(job['input_json'])['spec']['values']['color']
                if published['revision'] != 3 or actual != expected[item['asset']]:
                    stale.append({'revision': published['revision'], 'asset': item['asset']})
        row = runtime.db.execute('SELECT published_at FROM vf_timelines WHERE revision=3').fetchone()
        edited = runtime.db.execute('SELECT MAX(applied_at) FROM vf_events').fetchone()[0]
        published = row[0] if row and not stale else None
        inspections = summarize_inspections(runtime)
        history_ids = {item['job'] for item in inspections['jobs'] if item['history_seen']}
        accepted = [job for job in snapshot['jobs']
                    if job['accepted_observed_at'] is not None or job['id'] in history_ids]
        report = dict(
            trace=trace, policy=policy, final_revision=snapshot['revision'],
            ready=published is not None, stale_publications=stale,
            unresolved_ids=[job['external_id'] for job in unresolved],
            observed_acceptances=len(accepted),
            observed_acceptances_by_shot={shot: sum(job['asset'] == shot for job in accepted)
                                          for shot in TEMPLATES},
            submission_intents=sum(job['submission_intents'] for job in snapshot['jobs']),
            edit_to_publish_s=None if published is None else published - edited,
            restart_to_publish_s=None if published is None else published - restarted,
            inspections=inspections,
        )
        write_json(root / 'report.json', report)
        if published is not None:
            archive_current(runtime, root / 'artifacts', backend.base)
        return report
    finally:
        runtime.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--poll', type=float, default=2)
    parser.add_argument('--timeout', type=float, default=3600)
    parser.add_argument('--prepare', nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args()
    backend = ComfyUI(args.server)
    if args.prepare:
        prepare(args.output, backend, *args.prepare)
        return
    if min(args.repeat, args.poll, args.timeout) <= 0:
        parser.error('repeat, poll and timeout must be positive')
    backend.environment()
    args.output.mkdir(parents=True)
    rows = []
    for repeat in range(args.repeat):
        for trace_index, trace in enumerate(TRACES):
            offset = (repeat + trace_index) % len(POLICIES)
            for policy in POLICIES[offset:] + POLICIES[:offset]:
                print(f'Trial {repeat + 1}: {trace} / {policy}', flush=True)
                root = args.output / f'{repeat + 1}-{trace}-{policy}'
                row = run_trial(root, backend, policy, trace, args.poll, args.timeout)
                rows.append(dict(repeat=repeat + 1, **row))
                write_json(args.output / 'report.json', {'rows': rows, 'complete': False})
                if not row['ready'] or row['unresolved_ids'] or row['stale_publications']:
                    raise SystemExit(f'Trial failed readiness or drainage; see {root}/report.json')
    write_json(args.output / 'report.json', {'rows': rows, 'complete': True})


if __name__ == '__main__':
    main()
