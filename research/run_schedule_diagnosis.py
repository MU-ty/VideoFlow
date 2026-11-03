"""Fresh-seed falsification with context-tuned fixed baselines; no GPU calls."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import statistics
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from artifacts import write_json
from asset_runtime import fingerprint
from research.schedule_sim_v2 import Policy,simulate
from research.schedule_traces import make_trace
from research.run_schedule_benchmark import candidates,describe

FAMILIES=('single','burst','spaced','mixed','undo')
CONTEXTS=[(r,c) for r in ('independent','runtime_edges') for c in ('semantic','revision')]

def context(t):return t['readiness']+'/'+t['cache_mode']

def suite(n=8):
    if n<4 or n%4 or n>40:raise ValueError('per family must be a multiple of 4, 4..40')
    result={}
    for split,offset in [('development',61000),('heldout',81000)]:
        traces=[]
        for f,family in enumerate(FAMILIES):
            for i in range(n):
                readiness,cache=CONTEXTS[i%4]
                t=make_trace(family,offset+f*100+i,split,readiness)
                t['cache_mode']=cache;traces.append(t)
        result[split]=traces
    result['suite_hash']=fingerprint(result)
    return result

def tune(traces,kind):
    records=[]
    for p in candidates(kind):
        records.append(dict(policy=asdict(p),objective=statistics.mean(simulate(t,p)['objective'] for t in traces)))
    return min(records,key=lambda r:r['objective']),records

def run(out,n=8):
    out=Path(out)
    if out.exists():raise ValueError('fresh directory required')
    out.mkdir(parents=True);work=suite(n);write_json(out/'traces.json',work)
    gate='Continue candidate only if context-adaptive improves objective overall and in runtime_edges/revision, with no worse mean latency or unused service in that runtime context.'
    write_json(out/'protocol.json',dict(gate=gate,contexts='Only deployment readiness/cache capabilities; no future arrival-family labels.',
        candidates_per_kind=32,oracle='Hindsight minimum over 64 declared candidates; not an online policy or a universal lower bound.',
        weights=dict(latency=1,unused=.25,exposure=.1),synthetic_only=True))
    selections={};tuning={}
    for group in ['global']+[r+'/'+c for r,c in CONTEXTS]:
        dev=[t for t in work['development'] if group=='global' or context(t)==group]
        for kind in ('fixed','adaptive'):
            print('TUNING',group,kind,flush=True)
            best,records=tune(dev,kind);selections[group+'/'+kind]=best['policy'];tuning[group+'/'+kind]=records
    write_json(out/'selection.json',dict(suite_hash=work['suite_hash'],selections=selections,tuning=tuning,
        source_hashes={f:fingerprint((Path(__file__).parent/f).read_text()) for f in
          ('schedule_sim_v2.py','schedule_traces.py','schedule_sim.py','run_schedule_benchmark.py','run_schedule_diagnosis.py','../asset_runtime.py','../attribute_baseline.py')}))
    rows={k:[] for k in ('immediate_fifo','latest_first','shortest_first','global_fixed','global_adaptive','context_fixed','context_adaptive','hindsight_64')}
    for t in work['heldout']:
        policies=dict(immediate_fifo=Policy('immediate',order='fifo'),latest_first=Policy('immediate',order='latest'),shortest_first=Policy('immediate',order='shortest'))
        for group in ('global','context'):
            for kind in ('fixed','adaptive'):
                policies[group+'_'+kind]=Policy(**selections[('global' if group=='global' else context(t))+'/'+kind])
        results={name:simulate(t,p,audit=True) for name,p in policies.items()}
        # Deliberately future-informed diagnosis, computed only after selection lock.
        oracle=min((simulate(t,p) for kind in ('fixed','adaptive') for p in candidates(kind)),key=lambda r:r['objective'])
        results['hindsight_64']=oracle
        assert all(r['final_correct'] and not r['remote_unresolved'] for r in results.values())
        assert len({json.dumps(r['final_input_keys'],sort_keys=True) for r in results.values()})==1
        for name,r in results.items():
            write_json(out/'audit'/name/(t['id']+'.json'),r)
            rows[name].append({k:v for k,v in r.items() if k not in ('jobs','events','publications')})
    means={name:describe(rs) for name,rs in rows.items()}
    slices={r+'/'+c:{name:describe([x for x in rs if x['readiness']==r and x['cache_mode']==c]) for name,rs in rows.items()} for r,c in CONTEXTS}
    a=slices['runtime_edges/revision']['context_adaptive'];b=slices['runtime_edges/revision']['context_fixed']
    passed=(means['context_adaptive']['objective']<means['context_fixed']['objective'] and a['objective']<b['objective'] and
            a['final_edit_latency_seconds']<=b['final_edit_latency_seconds'] and a['unconsumed_service_seconds']<=b['unconsumed_service_seconds'])
    result=dict(suite_hash=work['suite_hash'],development_traces=len(work['development']),heldout_traces=len(work['heldout']),means=means,
        by_context=slices,by_family={f:{name:describe([r for r in rs if r['family']==f]) for name,rs in rows.items()} for f in FAMILIES},
        selections=selections,gate=gate,gate_passed=passed,diagnosis='pause current scheduling candidate' if not passed else 'collect external traces before any GPU campaign',
        limitations=['Fresh seeds from the same synthetic generator, not independent user behavior.',
          'Direct-parent readiness and revision rejection corrected; no 5s polling, startup/offload, failures, concurrency or real service calibration.',
          'Semantic cache is a hypothetical shared capability; runtime_edges/revision is closer to current runtime, not a full replica.',
          'Context tuning cannot use future workload family or future edit times.',
          'Hindsight64 is a finite candidate diagnostic with future access; not an implementable baseline or global optimum.'],gpu_seconds=None,video_quality=None,rows=rows)
    write_json(out/'report.json',result)
    lines=['# 连续修改调度诊断','','40 development / 40 held-out traces at default settings; synthetic only.','',
        '| 策略 | 目标值↓ | 最后修改延迟↓ | 未使用服务量↓ |','|---|---:|---:|---:|']
    for name,m in means.items():lines.append(f"| {name} | {m['objective']:.3f} | {m['final_edit_latency_seconds']:.3f} | {m['unconsumed_service_seconds']:.3f} |")
    lines+=['',f'Gate passed: {passed}. Diagnosis: {result["diagnosis"]}.','','## 已知部署条件分组','']
    for ctx,m in slices.items():
        lines.append(f"- {ctx}: fixed={m['context_fixed']['objective']:.3f}; adaptive={m['context_adaptive']['objective']:.3f}; hindsight64={m['hindsight_64']['objective']:.3f}.")
    lines+=['','## 限制','']+['- '+v for v in result['limitations']]
    (out/'report.md').write_text('\n'.join(lines)+'\n')
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True);p.add_argument('--per-family',type=int,default=8);a=p.parse_args()
    r=run(a.output,a.per_family);print(json.dumps(dict(means=r['means'],gate_passed=r['gate_passed'],diagnosis=r['diagnosis']),indent=2))
