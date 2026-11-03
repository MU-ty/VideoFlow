"""Run frozen story/seed blocks with method order recorded before execution."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from artifacts import write_json
from asset_runtime import fingerprint
from research.mock_provider import MockProvider
from research.run_experiment import run
from research.compare_post import compare, markdown


def execute(plan_file, output, remote=False, server='http://127.0.0.1:8188', stories=None, seeds=None, methods=None, resume=False):
    plan_file=Path(plan_file).resolve();base=plan_file.parent
    plan=json.loads(plan_file.read_text());expected=plan.pop('plan_hash')
    if fingerprint(plan)!=expected:raise ValueError('plan hash mismatch')
    plan['plan_hash']=expected
    if stories and set(stories)-{b['story'] for b in plan['blocks']}:raise ValueError('unknown story')
    if seeds and set(seeds)-{b['seed'] for b in plan['blocks']}:raise ValueError('unknown seed')
    if methods and set(methods)-set(plan['methods']):raise ValueError('unknown method')
    output=Path(output).resolve()
    selection=dict(plan_hash=expected,stories=stories,seeds=seeds,methods=methods,remote=remote,server=server,
                   runner_hash=fingerprint(Path(__file__).read_text()))
    if output.exists():
        if not resume or not (output/'matrix_selection.json').exists():raise ValueError('use a fresh output directory or --resume with unchanged selection')
        if json.loads((output/'matrix_selection.json').read_text())!=selection:raise ValueError('resume selection mismatch')
    else:output.mkdir(parents=True)
    write_json(output/'matrix_selection.json',selection)
    write_json(output/'plan.json',plan)
    summary=[]
    for block in plan['blocks']:
        if stories and block['story'] not in stories:continue
        if seeds and block['seed'] not in seeds:continue
        order=[m for m in block['method_order'] if not methods or m in methods]
        manifest=json.loads((base/block['manifest']).read_text());edit=json.loads((base/block['edit']).read_text())
        if fingerprint(manifest)!=block['manifest_hash'] or fingerprint(edit)!=block['edit_hash']:
            raise ValueError('manifest/edit differs from frozen plan')
        for method in order:
            destination=output/block['id']/method
            config=destination/'configuration.json'
            if not remote and config.exists():
                raise ValueError('mock providers do not persist across process restarts; use a fresh mock directory')
            options=dict(method=method,server=server,max_inflight=plan['max_inflight'],
                         poll_seconds=plan['poll_seconds'] if remote else 0,timeout_seconds=21600 if remote else 30)
            if not remote:
                backend=MockProvider();options.update(backend=backend,downloader=backend.download)
            print(f"START {block['id']}/{method}",flush=True)
            report=run(manifest,edit,destination,**options)
            summary.append(dict(block=block['id'],story=block['story'],seed=block['seed'],method=method,
                                status=report['status'],evidence=report['evidence'],metrics=report['metrics']))
            write_json(output/'matrix_status.json',summary)
            if report['status']!='complete':raise RuntimeError('condition failed or paused; inspect its report and resume using identical arguments/source')
        if len(order)>1:
            result=compare([output/block['id']/m for m in order],decode=False)
            write_json(output/block['id']/'comparison.json',result)
            (output/block['id']/'comparison.md').write_text(markdown(result))
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('plan');p.add_argument('--output',required=True)
    p.add_argument('--remote',action='store_true');p.add_argument('--resume',action='store_true')
    p.add_argument('--server',default='http://127.0.0.1:8188');p.add_argument('--stories',nargs='+')
    p.add_argument('--seeds',type=int,nargs='+');p.add_argument('--methods',nargs='+');a=p.parse_args()
    result=execute(a.plan,a.output,a.remote,a.server,a.stories,a.seeds,a.methods,a.resume)
    print(json.dumps(dict(conditions=len(result),complete=sum(r['status']=='complete' for r in result)),indent=2))
