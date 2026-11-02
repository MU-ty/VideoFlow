"""Compare two or more complete dynamic runs with matched inputs and source."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from artifacts import write_json
from asset_runtime import fingerprint
from research.compare_dynamic import load_run


def compare(directories):
    if len(directories)<2:raise ValueError('at least two runs required')
    normalized=None;seen=set();rows=[]
    for directory in directories:
        config,report=load_run(directory)
        if normalized is None:normalized=config
        if config!=normalized:raise ValueError('manifest/schedule/backend/source/configuration mismatch')
        if report['method'] in seen:raise ValueError('duplicate method in comparison')
        seen.add(report['method'])
        rows.append(dict(method=report['method'],metrics=report['metrics'],
                         provider_observations=report.get('provider_observations')))
    return dict(evidence=normalized['evidence'],configuration_hash=fingerprint(normalized),rows=rows,
                gpu_seconds=None,api_cost=None,quality_comparison=None,
                limitation='Matched state triggers are not fixed-time arrival traces. Provider execution spans and cache node lists do not measure GPU compute.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('runs',nargs='+');p.add_argument('--output',required=True);a=p.parse_args()
    result=compare(a.runs);write_json(a.output,result);print(json.dumps(result,indent=2))
