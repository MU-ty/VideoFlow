"""Development-only tuning and held-out symbolic scheduling evaluation."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import statistics
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from artifacts import write_json
from asset_runtime import fingerprint
from research.schedule_sim import Policy,simulate
from research.schedule_traces import suite

ORDERS=('fifo','latest','shortest','stable')


def candidates(kind):
    if kind=='fixed':
        return [Policy('fixed',wait=w,order=o) for w in (0,2,5,10,20,40,80,120) for o in ORDERS]
    if kind=='adaptive':
        return [Policy('adaptive',wait=w,factor=f,order=o) for w in (0,5,20,40) for f in (.5,1.5) for o in ORDERS]
    raise ValueError('unknown candidate family')


def tune(traces,kind):
    records=[]
    for policy in candidates(kind):
        results=[simulate(t,policy) for t in traces]
        records.append(dict(policy=asdict(policy),development_mean_objective=statistics.mean(r['objective'] for r in results)))
    best=min(enumerate(records),key=lambda item:(item[1]['development_mean_objective'],item[0]))[1]
    return Policy(**best['policy']),records


def describe(rows):
    keys=('objective','submission_intents','simulated_service_seconds','unconsumed_service_seconds',
          'superseded_on_completion_service_seconds','final_edit_latency_seconds','unavailable_asset_exposure_seconds')
    return {k:statistics.mean(r[k] for r in rows) for k in keys}


def run(directory,per_family=4):
    directory=Path(directory)
    if directory.exists():raise ValueError('use a fresh output directory')
    directory.mkdir(parents=True);workloads=suite(per_family);write_json(directory/'traces.json',workloads)
    selected={};tuning={}
    for kind in ('fixed','adaptive'):
        print('TUNING '+kind,flush=True)
        selected[kind],tuning[kind]=tune(workloads['development'],kind)
    # Persist selection before the first held-out simulation; no held-out retuning.
    lock=dict(suite_hash=workloads['suite_hash'],policies={k:asdict(v) for k,v in selected.items()},
              tuning=tuning,source_hashes={f:fingerprint((Path(__file__).parent/f).read_text()) for f in
                  ('schedule_sim.py','schedule_traces.py','run_schedule_benchmark.py','../attribute_baseline.py','../dair.py','../asset_runtime.py','../artifacts.py')})
    write_json(directory/'selection.json',lock)
    policies=dict(immediate_fifo=Policy('immediate',order='fifo'),latest_first=Policy('immediate',order='latest'),
                  shortest_first=Policy('immediate',order='shortest'),fixed_tuned=selected['fixed'],adaptive_tuned=selected['adaptive'])
    rows={name:[] for name in policies}
    for t in workloads['heldout']:
        results={name:simulate(t,p,audit=True) for name,p in policies.items()}
        assert all(r['final_correct'] and r['remote_unresolved']==0 for r in results.values())
        assert len({json.dumps(r['final_input_keys'],sort_keys=True) for r in results.values()})==1
        for name,r in results.items():
            write_json(directory/'audit'/name/(t['id']+'.json'),r)
            compact={k:v for k,v in r.items() if k not in ('jobs','events','publications')};rows[name].append(compact)
    means={name:describe(rs) for name,rs in rows.items()}
    paired=[]
    for a,b in zip(rows['adaptive_tuned'],rows['fixed_tuned']):
        assert a['trace_id']==b['trace_id']
        paired.append(dict(trace_id=a['trace_id'],family=a['family'],readiness=a['readiness'],
                           objective_difference=a['objective']-b['objective'],
                           latency_difference=a['final_edit_latency_seconds']-b['final_edit_latency_seconds'],
                           unconsumed_difference=a['unconsumed_service_seconds']-b['unconsumed_service_seconds']))
    slices={}
    for family in ('single','burst','spaced','mixed','undo'):
        slices[family]={name:describe([r for r in rs if r['family']==family]) for name,rs in rows.items()}
    profile_slices={p:{name:describe([r for r in rs if r['readiness']==p]) for name,rs in rows.items() if any(r['readiness']==p for r in rs)} for p in ('independent','ordered')}
    result=dict(evidence=workloads['evidence'],suite_hash=workloads['suite_hash'],development_traces=len(workloads['development']),
                heldout_traces=len(workloads['heldout']),selected_policies={k:asdict(v) for k,v in policies.items()},
                means=means,by_family=slices,by_readiness=profile_slices,paired_adaptive_vs_fixed=paired,rows=rows,
                gpu_seconds=None,video_quality=None,
                limitations=['Synthetic same-family split, not independent human edit traces.',
                 'Objective weights are frozen design choices, not elicited user utility.',
                 'The independent readiness profile differs from the current production runtime order gating; no drop-in real-GPU speed claim.',
                 'Exact symbolic caching cannot establish real visual equivalence, quality or identity preservation.',
                 'Cross-revision exact-key rebinding after undo is a shared simulated capability, not the current production runtime behavior.',
                 'No interruption, server failure, queue deletion or provider cancellation is simulated.',
                 'Any winner is exploratory evidence; tuning/evaluation code was authored by the method developer.'])
    write_json(directory/'report.json',result)
    lines=['# Synthetic scheduling probe','',f"Development traces: {result['development_traces']}; held-out traces: {result['heldout_traces']}.",'',
           '| Policy | Objective | Final edit latency | Unconsumed service | Submissions |','|---|---:|---:|---:|---:|']
    for name,m in means.items():lines.append(f"| {name} | {m['objective']:.3f} | {m['final_edit_latency_seconds']:.3f} | {m['unconsumed_service_seconds']:.3f} | {m['submission_intents']:.2f} |")
    lines+=['','All times are simulation units calibrated only by a declared service range, not measured GPU seconds.','',
            '## Limitations','']+['- '+x for x in result['limitations']]
    (directory/'report.md').write_text('\n'.join(lines)+'\n')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True);p.add_argument('--per-family',type=int,default=4);a=p.parse_args()
    r=run(a.output,a.per_family);print(json.dumps(dict(means=r['means'],selected_policies=r['selected_policies']),indent=2))
