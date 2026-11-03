"""Deterministic synthetic wall-clock workloads, not recorded human behavior."""
import random
from asset_runtime import fingerprint


def make_trace(family, seed, split='test', readiness='independent'):
    rng=random.Random(seed);nodes=[]
    for i in range(1,7):
        reads=['scene.light']+(['hero.color'] if i in (2,4,6) else [])
        nodes.append(dict(id=f'S{i}',cost=rng.uniform(55,120),reads=reads))
    edges=[dict(parent='S2',child='S4',kind='state',keys=['hero.color']),
           dict(parent='S4',child='S6',kind='state',keys=['hero.color'])]
    edges += [dict(parent=f'S{i}',child=f'S{i+1}',kind='order') for i in range(1,6)]
    start=rng.uniform(5,160)
    if family=='single':times=[start]
    elif family=='burst':
        times=[start]
        for _ in range(rng.randint(3,8)):times.append(times[-1]+rng.uniform(1,10))
    elif family=='spaced':
        times=[start]
        for _ in range(rng.randint(2,5)):times.append(times[-1]+rng.uniform(130,320))
    elif family=='mixed':
        times=[];t=start
        for _ in range(3):
            for _ in range(rng.randint(2,5)):
                times.append(t);t+=rng.uniform(2,14)
            t+=rng.uniform(160,340)
    elif family=='undo':
        times=[start,start+rng.uniform(3,9),start+rng.uniform(18,35),start+rng.uniform(180,250)]
    else:raise ValueError('unknown family')
    events=[]
    for i,t in enumerate(times):
        global_edit=(family=='mixed' and i%4==3)
        value=('initial' if family=='undo' and i==2 else f'edit-{i}')
        events.append(dict(at=round(t,6),edit=dict(target='S1' if global_edit else 'S2',
                      key='scene.light' if global_edit else 'hero.color',scope='global' if global_edit else 'local',value=value)))
    return dict(id=f'{split}-{family}-{seed}-{readiness}',family=family,seed=seed,
                readiness=readiness,max_inflight=2,story=dict(scenes=nodes,edges=edges),events=events)


def suite(per_family=4):
    if not 1<=per_family<=50:raise ValueError('per_family must be 1..50')
    families=['single','burst','spaced','mixed','undo'];rows={}
    for split,offset in [('development',11000),('heldout',31000)]:
        traces=[]
        for f,family in enumerate(families):
            for i in range(per_family):
                seed=offset+f*100+i
                # Each family includes both readiness profiles, shared by all methods.
                traces.append(make_trace(family,seed,split,'independent' if i%2==0 else 'ordered'))
        rows[split]=traces
    payload=dict(schema=1,evidence='synthetic generated arrivals and service assumptions; not real user traces',
                 objective_weights=dict(final_edit_latency_seconds=1,unconsumed_service_seconds=.25,unavailable_asset_exposure_seconds=.1),
                 initial_state='all six assets initially absent',
                 cache='all methods reuse exact symbolic completed input keys, including late outputs after undo',
                 tie_order='edits, then completions, then dispatch at identical timestamps',
                 readiness_profiles='independent: resolved prompts allow independent generation; ordered: all previous assets must be current before submit',
                 **rows)
    payload['suite_hash']=fingerprint(payload)
    return payload
