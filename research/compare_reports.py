"""Compare matched Full/DAIR runs without treating submission intents as GPU cost."""
import argparse
import copy
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from artifacts import checksum, write_json
from asset_runtime import fingerprint


def load_run(directory):
    root=Path(directory).resolve()
    config=json.loads((root/'configuration.json').read_text(encoding='utf-8'))
    report=json.loads((root/'report.json').read_text(encoding='utf-8'))
    if report['configuration_hash']!=fingerprint(config) or report['method']!=config['method']:
        raise ValueError('configuration/report mismatch')
    if report['status']!='complete' or report['phase']!='complete':
        raise ValueError('comparison requires completed runs')
    archive=json.loads((root/'artifacts'/('revision-'+str(report['project_revision'])+'.json')).read_text(encoding='utf-8'))
    for artifact in archive['assets']:
        path=(root/'artifacts'/artifact['file']).resolve()
        if not path.is_relative_to(root/'artifacts') or checksum(path)!=artifact['sha256']:
            raise ValueError('archived bytes are missing or changed')
    normalized=copy.deepcopy(config)
    normalized.pop('method')
    normalized['event'].pop('method')
    return normalized, report


def compare(full_directory,dair_directory):
    full_config,full=load_run(full_directory)
    dair_config,dair=load_run(dair_directory)
    if full['method']!='full' or dair['method']!='dair':
        raise ValueError('expected full then dair')
    if full_config!=dair_config:
        raise ValueError('unmatched manifest/edit/backend/source/configuration')
    keys=('edit_submission_intents','total_submission_intents','edit_completion_observed_seconds','estimated_edit_cost')
    return dict(evidence=full['evidence'], matched_configuration_hash=fingerprint(full_config),
                rows=[dict(method=r['method'], **{k:r['metrics'][k] for k in keys}) for r in (full,dair)],
                edit_submission_intents_difference=full['metrics']['edit_submission_intents']-dair['metrics']['edit_submission_intents'],
                gpu_seconds=None,api_cost=None,quality_comparison=None,
                interpretation='One matched case. Intent difference is not measured compute/cost savings. Observed latency includes queue/poll/downtime. Quality requires independent evaluation.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('full');p.add_argument('dair');p.add_argument('--output',required=True);a=p.parse_args()
    result=compare(a.full,a.dair);write_json(a.output,result)
    print(json.dumps(result,indent=2))
