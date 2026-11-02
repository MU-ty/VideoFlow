"""Replay ordered edits at durable submission boundaries while jobs are in flight."""
import argparse
import json
import math
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from artifacts import archive_current, write_json
from asset_runtime import AssetRuntime, Conflict, fingerprint
from asset_worker import pass_once
from comfyui import ComfyUI
from research.run_experiment import ROOT, runner_lock


def validate_schedule(manifest, schedule, method):
    if method not in ('dair','full'):
        raise ValueError('method must be dair or full')
    if not isinstance(schedule, list) or not schedule:
        raise ValueError('schedule must be a nonempty ordered list')
    assets = {a['id']:a for a in manifest['assets']}
    seen = set()
    events = []
    for i, item in enumerate(schedule):
        trigger = item['trigger']
        if set(trigger)-{'after_submission_intents','asset','revision'}:
            raise ValueError('unknown trigger field')
        count = trigger.get('after_submission_intents',0)
        if type(count) is not int or count<0:
            raise ValueError('nonnegative integer submission threshold required')
        if ('asset' in trigger)!=('revision' in trigger):
            raise ValueError('inflight trigger requires both asset and revision')
        if 'asset' in trigger:
            if trigger['asset'] not in assets or assets[trigger['asset']]['kind']!='video':
                raise ValueError('inflight trigger must name a video asset')
            if type(trigger['revision']) is not int or trigger['revision']<1:
                raise ValueError('positive inflight asset revision required')
        edit = dict(item['edit'])
        if not isinstance(edit['id'],str) or not edit['id']:
            raise ValueError('nonempty string event ID required')
        if edit['id'] in seen:
            raise ValueError('duplicate event ID')
        seen.add(edit['id'])
        if type(edit.get('expected_revision',i+1)) is not int or edit.get('expected_revision',i+1)!=i+1:
            raise ValueError('ordered edit revisions must be 1,2,...')
        edit.update(expected_revision=i+1,method=method)
        if edit['target'] not in assets or edit['key'] not in assets[edit['target']]['spec']['reads']:
            raise ValueError('event must edit a declared input')
        if not isinstance(edit['value'],str) or edit.get('scope','local') not in ('local','onward','global'):
            raise ValueError('invalid edit value/scope')
        events.append(dict(trigger=dict(trigger), edit=edit))
    return events


def applied_prefix(runtime, events):
    rows = runtime.db.execute('SELECT * FROM vf_events ORDER BY revision').fetchall()
    if len(rows)>len(events):
        raise Conflict('unscheduled edit in experiment database')
    for i,row in enumerate(rows):
        if row['payload']!=json.dumps(events[i]['edit'],sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False):
            raise Conflict('recorded edits do not match the schedule prefix')
        if row['revision']!=i+2:
            raise Conflict('unexpected project revision history')
    if runtime.snapshot()['revision']!=len(rows)+1:
        raise Conflict('project revision does not match recorded edits')
    return len(rows)


def trigger_ready(runtime, trigger):
    count = runtime.db.execute('SELECT COALESCE(SUM(submission_intents),0) FROM vf_jobs').fetchone()[0]
    if count<trigger.get('after_submission_intents',0):
        return False
    if 'asset' not in trigger:
        return True
    return any(j['asset']==trigger['asset'] and j['revision']==trigger['revision'] and
               j['status'] in ('submitting','submitted','unknown') and j['submission_intents']>0
               for j in runtime.unresolved_remote())


def report_dynamic(runtime, state, status, evidence, events, error):
    if not runtime.db.execute('SELECT 1 FROM vf_project').fetchone():
        return dict(status='error',evidence=evidence,error=error,metrics={},phase=state['phase'])
    snapshot = runtime.snapshot()
    event_rows = runtime.db.execute('SELECT * FROM vf_events ORDER BY revision').fetchall()
    timelines = {r['revision']:r['published_at'] for r in runtime.db.execute('SELECT * FROM vf_timelines')}
    final_time = timelines.get(snapshot['revision'])
    event_reports = []
    for row in event_rows:
        record = json.loads(row['plan'])
        own_time = timelines.get(row['revision'])
        event_reports.append(dict(id=row['id'],revision=row['revision'],applied_at=row['applied_at'],plan=record,
            own_revision_published_at=own_time,
            own_revision_latency_seconds=None if own_time is None else own_time-row['applied_at'],
            event_to_final_revision_observed_seconds=None if final_time is None else final_time-row['applied_at']))
    jobs = snapshot['jobs']
    unresolved = runtime.unresolved_remote()
    stale = [j for j in jobs if j['status']=='superseded' and j['remote_state']=='succeeded']
    return dict(status=status,phase=state['phase'],evidence=evidence,error=error,
        configuration_hash=state['configuration_hash'],method=state['method'],sessions=state['sessions'],
        current_revision=snapshot['revision'],project_ready=snapshot['timeline'] is not None,
        applied_events=len(event_rows),scheduled_events=len(events),events=event_reports,
        metrics=dict(submission_intents=sum(j['submission_intents'] for j in jobs),
            stale_results_observed=len(stale),superseded_remote_unresolved=sum(j['status']=='superseded' for j in unresolved),
            remote_unresolved=len(unresolved),max_remote_unresolved_observed=state['max_remote_unresolved'],
            worker_passes=state['passes'],gpu_seconds=None,api_cost=None,edit_success_rate=None,
            consistency=None,external_duplicate_executions=None),
        stale_observations=[dict(job=j['id'],asset=j['asset'],revision=j['revision'],output=j['remote_output']) for j in stale],
        definitions=dict(protocol='Events fire at client durable-intent boundaries, optionally requiring an exact asset revision still in flight; not a fixed wall-clock trace.',
            completed='All scheduled events applied, current artifacts archived and every tracked remote job terminal. No semantic-quality guarantee.',
            own_revision_latency='Null if that project revision never became available before a later edit. Final-revision latency includes all later edits.',
            reuse='Plan reuse includes retained pending/running work; it is not a count of already generated reusable videos.'))


def run_dynamic(manifest, schedule, directory, *, method='dair',server='http://127.0.0.1:8188',
                poll_seconds=5,timeout_seconds=7200,max_inflight=1,max_passes=None,
                probe=False,backend=None,downloader=None):
    events=validate_schedule(manifest,schedule,method)
    if not math.isfinite(poll_seconds) or not 0<=poll_seconds<=60 or not math.isfinite(timeout_seconds) or timeout_seconds<0:
        raise ValueError('invalid polling/timeout limits')
    if type(max_inflight) is not int or max_inflight<1 or (max_passes is not None and (type(max_passes) is not int or max_passes<0)):
        raise ValueError('invalid worker limits')
    root=Path(directory).resolve();root.mkdir(parents=True,exist_ok=True)
    evidence='synthetic_injected_backend' if backend is not None else 'remote_comfyui_unvalidated_quality'
    backend=ComfyUI(server) if backend is None else backend
    configuration=dict(protocol='inflight_edit_sequence_v1',manifest=manifest,events=events,method=method,
        backend=backend.base,max_inflight=max_inflight,probe=probe,evidence=evidence,
        source_hashes={p:fingerprint((ROOT/p).read_text(encoding='utf-8')) for p in
            ('asset_runtime.py','asset_worker.py','comfyui.py','dair.py','artifacts.py','research/run_experiment.py','research/run_dynamic.py')})
    signature=fingerprint(configuration)
    with runner_lock(root):
        state_path=root/'state.json'
        if state_path.exists():
            state=json.loads(state_path.read_text(encoding='utf-8'))
            if state['configuration_hash']!=signature:
                raise Conflict('resume configuration/source changed')
        else:
            if (root/'project.db').exists():
                raise Conflict('unowned project.db exists')
            state=dict(configuration_hash=signature,phase='running',method=method,sessions=[],passes=0,max_remote_unresolved=0)
            write_json(root/'configuration.json',configuration);write_json(state_path,state)
        state['sessions'].append(dict(started_at=time.time(),poll_seconds=poll_seconds,timeout_seconds=timeout_seconds,max_passes=max_passes))
        write_json(state_path,state)
        runtime=AssetRuntime(str(root/'project.db'))
        status='paused';error=None;passes=0;started=time.monotonic()
        try:
            if not runtime.db.execute('SELECT 1 FROM vf_project').fetchone():
                runtime.initialize(manifest)
            while True:
                index=applied_prefix(runtime,events)
                snapshot=runtime.snapshot()
                if index<len(events) and trigger_ready(runtime,events[index]['trigger']):
                    plan=runtime.edit(events[index]['edit'])
                    with (root/'events.jsonl').open('a',encoding='utf-8') as f:
                        f.write(json.dumps(dict(index=index,observed_at=time.time(),plan=plan))+'\n')
                    continue  # Cursor is always recovered from transactional vf_events.
                unresolved=runtime.unresolved_remote(backend.base)
                state['max_remote_unresolved']=max(state['max_remote_unresolved'],len(unresolved))
                if snapshot['timeline'] is not None:
                    if index<len(events):
                        status='blocked';error='next inflight trigger was not reached before current project completed';break
                    options=dict(probe=probe)
                    if downloader is not None:options['downloader']=downloader
                    archive_current(runtime,root/'artifacts',backend.base,**options)
                    state['phase']='final_ready_waiting_remote' if unresolved else 'complete'
                    write_json(state_path,state)
                    if not unresolved:
                        status='complete';break
                if time.monotonic()-started>=timeout_seconds or (max_passes is not None and passes>=max_passes):
                    break
                log=pass_once(runtime,backend,max_submit=max(0,max_inflight-len(unresolved)))
                passes+=1;state['passes']+=1
                state['max_remote_unresolved']=max(state['max_remote_unresolved'],len(runtime.unresolved_remote(backend.base)))
                write_json(state_path,state)
                with (root/'observations.jsonl').open('a',encoding='utf-8') as f:
                    f.write(json.dumps(dict(observed_at=time.time(),events=log))+'\n')
                current={a['id']:a['revision'] for a in runtime.snapshot()['assets']}
                if any(j['status']=='failed' and current[j['asset']]==j['revision'] for j in runtime.snapshot()['jobs']) or any(e['action'] in ('validation_error','backend_mismatch') for e in log):
                    status='blocked';error='current task failure or adapter validation error';break
                if poll_seconds:
                    time.sleep(min(poll_seconds,max(0,timeout_seconds-(time.monotonic()-started))))
        except (OSError,ValueError,RuntimeError,sqlite3.Error,subprocess.SubprocessError) as exc:
            status='error';error=str(exc)
        finally:
            try:
                result=report_dynamic(runtime,state,status,evidence,events,error)
                write_json(root/'report.json',result)
            finally:runtime.close()
        return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',default='fixtures/assets.json');p.add_argument('--schedule',default='fixtures/dynamic_edits.json')
    p.add_argument('--method',choices=('dair','full'),default='dair');p.add_argument('--server',default='http://127.0.0.1:8188')
    p.add_argument('--output',required=True);p.add_argument('--poll-seconds',type=float,default=5);p.add_argument('--timeout-seconds',type=float,default=7200)
    p.add_argument('--max-inflight',type=int,default=1);p.add_argument('--max-passes',type=int);p.add_argument('--probe',action='store_true')
    a=p.parse_args()
    result=run_dynamic(json.loads(Path(a.manifest).read_text(encoding='utf-8')),json.loads(Path(a.schedule).read_text(encoding='utf-8')),a.output,
        method=a.method,server=a.server,poll_seconds=a.poll_seconds,timeout_seconds=a.timeout_seconds,max_inflight=a.max_inflight,max_passes=a.max_passes,probe=a.probe)
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['status']=='complete' else 2


if __name__=='__main__':raise SystemExit(main())
