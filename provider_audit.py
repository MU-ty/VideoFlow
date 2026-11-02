"""Persist original provider inspections; cache/node coverage is not GPU timing."""
import json
import time


def save_inspection(runtime, backend, job):
    observation = getattr(backend, 'last_inspection', None)
    if observation is None:
        return
    if observation.get('prompt_id') != job['external_id']:
        raise ValueError('provider observation/task identity mismatch')
    payload = json.dumps(observation,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    with runtime.transaction():
        runtime.db.execute('INSERT OR IGNORE INTO vf_provider_inspections(job,backend,payload,first_seen_at) VALUES (?,?,?,?)',
                           (job['id'],backend.base,payload,time.time()))


def summarize_inspections(runtime):
    rows = runtime.db.execute('SELECT job,payload FROM vf_provider_inspections ORDER BY id').fetchall()
    grouped = {}
    for row in rows:
        observation = json.loads(row['payload'])
        record = grouped.setdefault(row['job'],dict(job=row['job'],cached_nodes=[],started_nodes=[],execution_span_seconds=None,history_seen=False))
        entry = observation.get('history_entry')
        if entry is None:
            continue
        record['history_seen']=True
        start=end=None
        for message in entry.get('status',{}).get('messages',[]):
            if not isinstance(message,list) or len(message)!=2 or not isinstance(message[1],dict):
                continue
            kind,body=message
            if kind=='execution_cached':
                record['cached_nodes']=sorted(set(record['cached_nodes'])|{str(n) for n in body.get('nodes',[])})
            if kind=='executing' and body.get('node') is not None:
                record['started_nodes']=sorted(set(record['started_nodes'])|{str(body['node'])})
            if kind=='execution_start':start=body.get('timestamp')
            if kind in ('execution_success','execution_error'):end=body.get('timestamp')
        if isinstance(start,(int,float)) and isinstance(end,(int,float)) and end>=start:
            record['execution_span_seconds']=(end-start)/1000
    return dict(jobs_with_observations=len(grouped),jobs=list(grouped.values()),gpu_seconds=None,
        environments=[dict(backend=r['backend'],observed_at=r['observed_at'],payload=None if r['payload'] is None else json.loads(r['payload']),error=r['error']) for r in runtime.db.execute('SELECT * FROM vf_provider_environment ORDER BY id')],
        limitation='Missing messages mean unknown, not zero work. Server start/end includes loading/CPU/I/O; cached node IDs do not establish GPU savings.')


def capture_environment(runtime,backend):
    getter=getattr(backend,'environment',None)
    if getter is None:
        return
    payload=None;error=None
    try:
        payload=json.dumps(getter(),sort_keys=True,ensure_ascii=False,allow_nan=False)
    except (OSError,ValueError) as exc:
        error=str(exc)
    with runtime.transaction():
        runtime.db.execute('INSERT INTO vf_provider_environment(backend,observed_at,payload,error) VALUES (?,?,?,?)',
                           (backend.base,time.time(),payload,error))
