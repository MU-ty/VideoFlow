"""Discrete-event scheduling probe. Symbolic outputs, no GPU/model calls."""
from dataclasses import dataclass
import math
import statistics
from attribute_baseline import attribute_plan
from asset_runtime import fingerprint


@dataclass(frozen=True)
class Policy:
    kind: str
    wait: float = 0
    factor: float = 1
    order: str = 'latest'
    max_wait: float = 120


def simulate(trace, policy, audit=False):
    """The engine sees future arrivals; policy decisions receive past per-asset gaps only.

    Single nonpreemptive server, FIFO remote queue, bounded inflight jobs. All
    policies coalesce unsubmitted versions, retain obsolete submitted jobs until
    completion, cache exact symbolic input keys, and share the same planner.
    """
    if policy.kind not in ('immediate','fixed','adaptive'): raise ValueError('unknown policy')
    if policy.order not in ('fifo','latest','shortest','stable'): raise ValueError('unknown ordering')
    if not all(math.isfinite(x) and x>=0 for x in (policy.wait,policy.factor,policy.max_wait)):
        raise ValueError('invalid policy parameter')
    assets=trace['story']['scenes']; ids=[x['id'] for x in assets]
    if not ids or len(set(ids))!=len(ids):raise ValueError('asset identities')
    if any(e['kind']=='reference' for e in trace['story'].get('edges',[])):
        raise ValueError('reference-conditioned generation is outside this probe')
    events=trace['events']
    if not events or any(not math.isfinite(e['at']) or e['at']<0 for e in events):raise ValueError('invalid arrival')
    if [e['at'] for e in events]!=sorted(e['at'] for e in events):raise ValueError('arrivals must be ordered')
    if trace['readiness'] not in ('independent','ordered'):raise ValueError('unknown readiness')
    limit=trace['max_inflight']
    if type(limit)!=int or limit<1:raise ValueError('positive inflight limit required')
    nodes={x['id']:dict(pos=i,cost=x['cost'],values={k:'initial' for k in x['reads']},version=0,
                       committed=None,deadline=0.,pending_since=0.,last_change=0.,gaps=[],last_edit=None)
           for i,x in enumerate(assets)}
    if any(not math.isfinite(n['cost']) or n['cost']<=0 for n in nodes.values()):raise ValueError('positive costs required')
    cache={};jobs=[];queue=[];running=None;now=0.;event_index=0
    exposure=0.;publications=[];log=[];seq=0
    for a in ids:nodes[a]['input_key']=fingerprint(dict(asset=a,values=nodes[a]['values'],seed=trace['seed']))
    def key(a):return nodes[a]['input_key']
    def ready(a):
        if trace['readiness']=='independent':return True
        return all(nodes[b]['committed']==key(b) for b in ids[:nodes[a]['pos']])
    def current(a):return nodes[a]['committed']==key(a)
    def rebind_cache():
        for a in ids:
            n=nodes[a];k=key(a)
            if k in cache:
                n['committed']=k;n['pending_since']=None;jobs[cache[k]]['used']=True
    def delay(n):
        if policy.kind=='immediate':return 0.
        if policy.kind=='fixed':return min(policy.max_wait,policy.wait)
        # Only intervals ending at the edit just observed are available here.
        if not n['gaps']:return min(policy.max_wait,policy.wait)
        gaps=n['gaps'][-4:]
        # A long silence makes the prior burst evidence stale.
        if gaps[-1]>=n['cost']:return 0.
        return min(policy.max_wait,n['cost']*.5,policy.factor*statistics.median(gaps))
    def active_current(a):return any(jobs[j]['asset']==a and jobs[j]['key']==key(a) for j in queue+([] if running is None else [running]))
    def priority(a):
        n=nodes[a];anchor=n['pending_since'] if n['pending_since'] is not None else now
        if policy.order=='fifo':return (anchor,n['pos'])
        if policy.order=='latest':return (-n['last_change'],n['pos'])
        if policy.order=='shortest':return (n['cost'],anchor,n['pos'])
        risk=int(bool(n['gaps']) and n['gaps'][-1]<n['cost'])
        return (risk,n['cost'],anchor,n['pos'])
    def start_server():
        nonlocal running
        if running is None and queue:
            running=queue.pop(0);job=jobs[running];job['started']=now
            job['finish']=now+job['service']
    for iteration in range(100000):
        # Tie convention is fixed for every policy: apply edits before same-time
        # completions, then dispatch. No zero-time interim revision is exposed.
        while event_index<len(events) and events[event_index]['at']<=now+1e-9:
            event=events[event_index];selected=attribute_plan(trace['story'],event['edit']).regenerate
            for a in selected:
                n=nodes[a]
                if n['last_edit'] is not None:n['gaps'].append(now-n['last_edit'])
                n['last_edit']=now;n['last_change']=now;n['version']+=1
                if event['edit']['key'] in n['values']:n['values'][event['edit']['key']]=event['edit']['value']
                else:raise ValueError('this probe requires each selected asset to read the edited key')
                n['input_key']=fingerprint(dict(asset=a,values=n['values'],seed=trace['seed']))
                if n['pending_since'] is None:n['pending_since']=now
                n['deadline']=max(now,min(now+delay(n),n['pending_since']+policy.max_wait))
            if audit:log.append(dict(at=now,event=event_index,affected=list(selected)))
            event_index+=1
        if running is not None and jobs[running]['finish']<=now+1e-9:
            job=jobs[running];a=job['asset'];cache[job['key']]=running
            job['superseded_at_finish']=job['key']!=key(a);job['done']=True
            if audit:log.append(dict(at=now,completed=running,current=not job['superseded_at_finish']))
            running=None
        rebind_cache();start_server()
        if all(current(a) for a in ids):
            revision=event_index
            if not publications or publications[-1]['revision']!=revision:
                publications.append(dict(revision=revision,at=now))
        if event_index==len(events) and all(current(a) for a in ids) and running is None and not queue:break
        # No queued-task deletion for any policy: once submitted, it consumes a
        # remote slot until completion, even if it later becomes obsolete.
        while len(queue)+(running is not None)<limit:
            candidates=[a for a in ids if not current(a) and not active_current(a) and ready(a) and nodes[a]['deadline']<=now+1e-9]
            if not candidates:break
            a=min(candidates,key=priority);n=nodes[a];k=key(a)
            # Fixed keyed jitter is identical for identical generation inputs,
            # irrespective of policy, submission order or wall-clock time.
            unit=int(fingerprint(dict(trace=trace['id'],key=k))[:8],16)/(2**32-1)
            service=n['cost']*(.85+.3*unit)
            jobs.append(dict(asset=a,key=k,version=n['version'],submitted=now,started=None,
                             finish=None,service=service,done=False,used=False,superseded_at_finish=False))
            j=len(jobs)-1;queue.append(j);n['pending_since']=None
            if audit:log.append(dict(at=now,submitted=j,asset=a,version=n['version']))
            seq+=1;start_server()
        clocks=[]
        if event_index<len(events):clocks.append(events[event_index]['at'])
        if running is not None:clocks.append(jobs[running]['finish'])
        if len(queue)+(running is not None)<limit:
            clocks += [nodes[a]['deadline'] for a in ids if not current(a) and not active_current(a) and ready(a) and nodes[a]['deadline']>now+1e-9]
        if not clocks:raise RuntimeError('deadlock')
        nxt=min(clocks)
        if nxt<=now+1e-9:raise RuntimeError('clock did not advance')
        exposure+=(nxt-now)*sum(not current(a) for a in ids)/len(ids)
        now=nxt
    else:raise RuntimeError('simulation iteration limit')
    if not all(j['done'] for j in jobs):raise AssertionError('unresolved remote work')
    final=next(p['at'] for p in publications if p['revision']==len(events))
    unused=sum(j['service'] for j in jobs if not j['used'])
    superseded=sum(j['service'] for j in jobs if j['superseded_at_finish'])
    # Frozen scalar objective; report its components and tradeoffs separately.
    tail=final-events[-1]['at'];score=tail+.25*unused+.1*exposure
    return dict(trace_id=trace['id'],family=trace['family'],readiness=trace['readiness'],
                submission_intents=len(jobs),simulated_service_seconds=sum(j['service'] for j in jobs),
                unconsumed_service_seconds=unused,superseded_on_completion_service_seconds=superseded,
                final_edit_latency_seconds=tail,unavailable_asset_exposure_seconds=exposure,
                final_visible_at=final,remote_drained_at=now,objective=score,
                final_correct=all(current(a) for a in ids),remote_unresolved=0,
                final_input_keys={a:key(a) for a in ids},jobs=jobs if audit else None,events=log if audit else None,
                publications=publications if audit else None,
                evidence='symbolic discrete-event simulation; no generated video or measured GPU time')
