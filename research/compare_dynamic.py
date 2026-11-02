"""Compare completed dynamic experiments under matched state-trigger schedules."""
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
    if config.get('protocol')!='inflight_edit_sequence_v1':
        raise ValueError('expected dynamic sequence protocol')
    if report['configuration_hash']!=fingerprint(config) or report['method']!=config['method']:
        raise ValueError('configuration/report mismatch')
    if report['status']!='complete' or not report['project_ready'] or report['metrics']['remote_unresolved']:
        raise ValueError('both dynamic runs must be complete with no unresolved remote jobs')
    archive=json.loads((root/'artifacts'/('revision-'+str(report['current_revision'])+'.json')).read_text(encoding='utf-8'))
    for item in archive['assets']:
        path=(root/'artifacts'/item['file']).resolve()
        if not path.is_relative_to(root/'artifacts') or checksum(path)!=item['sha256']:
            raise ValueError('artifact missing, changed or outside archive')
    normalized=copy.deepcopy(config);normalized.pop('method')
    for event in normalized['events']:event['edit'].pop('method')
    return normalized,report


def compare(full_directory,dair_directory):
    fc,full=load_run(full_directory);dc,dair=load_run(dair_directory)
    if full['method']!='full' or dair['method']!='dair' or fc!=dc:
        raise ValueError('methods or manifest/schedule/backend/source configuration do not match')
    keys=('submission_intents','stale_results_observed','max_remote_unresolved_observed','remote_unresolved')
    return dict(evidence=full['evidence'],matched_configuration_hash=fingerprint(fc),
        rows=[dict(method=r['method'],**{k:r['metrics'][k] for k in keys}) for r in (full,dair)],
        gpu_seconds=None,api_cost=None,quality_comparison=None,
        interpretation='Matched client-state triggers, not a fixed wall-clock arrival trace. Single designed case with no quality or cost conclusion.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('full');p.add_argument('dair');p.add_argument('--output',required=True);a=p.parse_args()
    result=compare(a.full,a.dair);write_json(a.output,result);print(json.dumps(result,indent=2))
