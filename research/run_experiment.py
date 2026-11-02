"""Resumable initial-generation -> structured-edit -> artifact-archive experiment."""
import argparse
from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from artifacts import archive_current, write_json
from asset_runtime import AssetRuntime, Conflict, fingerprint
from asset_worker import pass_once
from comfyui import ComfyUI
from planning import METHODS
from provider_audit import summarize_inspections, capture_environment

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def runner_lock(directory):
    # An OS-released SQLite lock survives abrupt runner termination safely.
    connection = sqlite3.connect(str(directory/'runner-lock.db'), timeout=0, isolation_level=None)
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS runner(id INTEGER PRIMARY KEY)')
        connection.execute('BEGIN IMMEDIATE')
        yield
    finally:
        connection.rollback(); connection.close()


def summarize(runtime, state, status, evidence, error=None):
    if not runtime.db.execute('SELECT 1 FROM vf_project').fetchone():
        return dict(status='error', evidence=evidence, method=state['method'],
                    configuration_hash=state['configuration_hash'], phase=state['phase'],
                    error=error or 'project initialization failed', metrics={}, project_revision=None)
    snapshot = runtime.snapshot()
    jobs = snapshot['jobs']
    edited = [j for j in jobs if j['revision']>1]
    event = runtime.db.execute('SELECT * FROM vf_events WHERE id=?', (state['event']['id'],)).fetchone()
    timeline = runtime.db.execute('SELECT published_at FROM vf_timelines WHERE revision=?', (snapshot['revision'],)).fetchone()
    p = None if event is None else json.loads(event['plan'])
    latency = None
    if event is not None and timeline is not None and event['applied_at'] is not None and timeline['published_at'] is not None:
        latency = timeline['published_at'] - event['applied_at']
    durations = [dict(job=j['id'], asset=j['asset'], revision=j['revision'],
                      reserve_to_completion_observed_seconds=j['completed_at']-j['reserved_at'])
                 for j in jobs if j['completed_at'] is not None and j['reserved_at'] is not None]
    return dict(status=status, evidence=evidence, method=state['method'], configuration_hash=state['configuration_hash'],
                phase=state['phase'], error=error, project_revision=snapshot['revision'],
                worker_passes=state['passes'], sessions=state['sessions'],
                provider_observations=summarize_inspections(runtime),
        metrics=dict(total_submission_intents=sum(j['submission_intents'] for j in jobs),
                    edit_submission_intents=sum(j['submission_intents'] for j in edited),
                    edit_completion_observed_seconds=latency,
                    planned_regenerate=None if p is None else p['regenerate'],
                    planned_reuse=None if p is None else p['reuse'],
                    estimated_edit_cost=None if p is None else p['estimated_cost'],
                    active_unknown_jobs=sum(j['status']=='unknown' for j in jobs),
                    failed_jobs=sum(j['status']=='failed' for j in jobs),
                    gpu_seconds=None, api_cost=None, token_usage=None, edit_success_rate=None,
                    cross_scene_consistency=None, external_duplicate_executions=None),
                task_observations=durations,
                definitions=dict(submission_intents='Durable intent before HTTP; a crash can occur before POST. Not billed/executed task count.',
                    observed_time='Client wall clock, including queue/polling/downtime. Not GPU duration. System clock changes can affect it.',
                    complete='Current artifacts were archived; this does not establish edit fidelity or video quality.'))


def run(manifest, edit, directory, *, method='dair', server='http://127.0.0.1:8188',
        poll_seconds=5, timeout_seconds=7200, max_inflight=1, max_passes=None,
        probe=False, backend=None, downloader=None):
    if method not in METHODS:
        raise ValueError('unknown execution method')
    if not math.isfinite(poll_seconds) or not 0<=poll_seconds<=60 or not math.isfinite(timeout_seconds) or timeout_seconds<0:
        raise ValueError('poll_seconds must be 0..60; timeout_seconds must be nonnegative')
    if max_inflight<1 or (max_passes is not None and max_passes<0):
        raise ValueError('invalid execution limits')
    root = Path(directory).resolve(); root.mkdir(parents=True, exist_ok=True)
    evidence = 'synthetic_injected_backend' if backend is not None else 'remote_comfyui_unvalidated_quality'
    backend = ComfyUI(server) if backend is None else backend
    event = dict(edit, method=method)
    configuration = dict(manifest=manifest, event=event, method=method, backend=backend.base,
                         max_inflight=max_inflight, probe=probe, evidence=evidence,
                         protocol='initial_complete_then_edit_v1',
                         source_hashes={p:fingerprint((ROOT/p).read_text(encoding='utf-8')) for p in
                                        ('asset_runtime.py','asset_worker.py','comfyui.py','dair.py','planning.py','provider_audit.py','artifacts.py','research/run_experiment.py')})
    configuration_hash = fingerprint(configuration)
    with runner_lock(root):
        state_path = root/'state.json'
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding='utf-8'))
            if state['configuration_hash']!=configuration_hash:
                raise Conflict('resume configuration/source changed; use a new output directory')
        else:
            if (root/'project.db').exists():
                raise Conflict('unowned project.db exists; use a new output directory')
            state = dict(configuration_hash=configuration_hash, phase='initial', passes=0,
                         method=method, event=event, sessions=[])
            write_json(root/'configuration.json',configuration)
            write_json(state_path,state)
        state['sessions'].append(dict(started_at=time.time(), poll_seconds=poll_seconds,
                                      timeout_seconds=timeout_seconds, max_passes=max_passes))
        write_json(state_path,state)
        runtime = AssetRuntime(str(root/'project.db'))
        started = time.monotonic(); passes = 0
        status = 'paused'; error = None
        try:
            if not runtime.db.execute('SELECT 1 FROM vf_project').fetchone():
                runtime.initialize(manifest)
            capture_environment(runtime,backend)
            while True:
                snapshot = runtime.snapshot()
                if snapshot['timeline'] is not None:
                    options = dict(probe=probe)
                    if downloader is not None:
                        options['downloader'] = downloader
                    archive_current(runtime,root/'artifacts',backend.base,**options)
                    if state['phase']=='initial':
                        runtime.edit(event)  # Same event replays if a crash preceded state save.
                        state['phase']='edited'; write_json(state_path,state)
                        continue
                    state['phase']='complete'; write_json(state_path,state)
                    status='complete'; break
                # Crash after edit commit but before state save: recover the phase.
                if state['phase']=='initial' and runtime.db.execute('SELECT 1 FROM vf_events WHERE id=?',(event['id'],)).fetchone():
                    runtime.edit(event)
                    state['phase']='edited'; write_json(state_path,state)
                if time.monotonic()-started>=timeout_seconds or (max_passes is not None and passes>=max_passes):
                    break
                active = len(runtime.unresolved_remote(backend.base))
                log = pass_once(runtime,backend,max_submit=max(0,max_inflight-active))
                state['passes']+=1; passes+=1
                write_json(state_path,state)
                with (root/'observations.jsonl').open('a',encoding='utf-8') as f:
                    f.write(json.dumps(dict(observed_at=time.time(),phase=state['phase'],events=log))+'\n')
                    f.flush()
                snapshot = runtime.snapshot()
                current = {a['id']:a['revision'] for a in snapshot['assets']}
                failed = any(j['status']=='failed' and current[j['asset']]==j['revision'] for j in snapshot['jobs'])
                blocked = any(e['action'] in ('validation_error','backend_mismatch') for e in log)
                if failed or blocked:
                    status='blocked'; error='current task failed or adapter validation blocked execution'; break
                if snapshot['timeline'] is None and poll_seconds:
                    time.sleep(min(poll_seconds,max(0,timeout_seconds-(time.monotonic()-started))))
        except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
            status='error'; error=str(exc)
        finally:
            try:
                report=summarize(runtime,state,status,evidence,error)
                write_json(root/'report.json',report)
            finally:
                runtime.close()
        return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',default='fixtures/assets.json')
    p.add_argument('--edit',default='fixtures/edit_costume.json')
    p.add_argument('--method',choices=METHODS,default='dair')
    p.add_argument('--server',default='http://127.0.0.1:8188')
    p.add_argument('--output',required=True)
    p.add_argument('--poll-seconds',type=float,default=5)
    p.add_argument('--timeout-seconds',type=float,default=7200)
    p.add_argument('--max-inflight',type=int,default=1)
    p.add_argument('--max-passes',type=int)
    p.add_argument('--probe',action='store_true',help='require ffprobe video stream validation')
    a=p.parse_args()
    report=run(json.loads(Path(a.manifest).read_text(encoding='utf-8')),
               json.loads(Path(a.edit).read_text(encoding='utf-8')),a.output,
               method=a.method,server=a.server,poll_seconds=a.poll_seconds,timeout_seconds=a.timeout_seconds,
               max_inflight=a.max_inflight,max_passes=a.max_passes,probe=a.probe)
    print(json.dumps(dict(status=report['status'],phase=report['phase'],metrics=report['metrics'],error=report['error']),ensure_ascii=False,indent=2))
    return 0 if report['status']=='complete' else 2


if __name__=='__main__':
    raise SystemExit(main())
