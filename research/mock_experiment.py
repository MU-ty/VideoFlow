"""Reproducible synthetic Full/DAIR execution comparison; not video research results."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from artifacts import write_json
from research.mock_provider import MockProvider
from research.run_experiment import run


def compare(directory):
    root=Path(__file__).resolve().parents[1]
    manifest=json.loads((root/'fixtures/assets.json').read_text())
    edit=json.loads((root/'fixtures/edit_costume.json').read_text())
    rows=[]
    for method in ('full','dair'):
        backend=MockProvider()
        result=run(manifest,edit,Path(directory)/method,method=method,poll_seconds=0,
                   timeout_seconds=60,max_inflight=2,backend=backend,downloader=backend.download)
        if result['status']!='complete':
            raise RuntimeError(result['error'] or result['status'])
        rows.append(dict(method=method, edit_submission_intents=result['metrics']['edit_submission_intents'],
                         total_submission_intents=result['metrics']['total_submission_intents'],
                         planned_regenerate=result['metrics']['planned_regenerate'],
                         planned_reuse=result['metrics']['planned_reuse'],
                         gpu_seconds=None,api_cost=None,edit_success_rate=None))
    return dict(evidence='synthetic provider, non-video bytes, real local SQLite/archive execution',
                protocol='separate initial generation for each method, then the same costume edit',
                results=rows, warning='Submission intents are not real model executions or monetary savings. No playable video or quality evaluation.')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',default='runs/mock');p.add_argument('--summary',default='results/paired_mock.json');a=p.parse_args()
    result=compare(a.output);write_json(a.summary,result)
    print(json.dumps(result,indent=2))
