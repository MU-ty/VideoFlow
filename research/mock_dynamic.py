"""Synthetic inflight edits: white -> black -> blue; no playable video produced."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from artifacts import write_json
from research.compare_dynamic import compare
from research.mock_provider import MockProvider
from research.run_dynamic import run_dynamic


def demonstrate(directory):
    root=Path(__file__).resolve().parents[1]
    manifest=json.loads((root/'fixtures/assets.json').read_text())
    schedule=json.loads((root/'fixtures/dynamic_edits.json').read_text())
    for method in ('full','dair'):
        backend=MockProvider()
        result=run_dynamic(manifest,schedule,Path(directory)/method,method=method,max_inflight=2,
            poll_seconds=0,timeout_seconds=60,backend=backend,downloader=backend.download)
        if result['status']!='complete':raise RuntimeError(result['error'] or result['status'])
    result=compare(Path(directory)/'full',Path(directory)/'dair')
    result['warning']='Synthetic provider returns prompt bytes, not videos. Inflight means unresolved client state, not evidence of physical GPU execution.'
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',default='runs/dynamic-mock');p.add_argument('--summary',default='results/dynamic_mock.json');a=p.parse_args()
    result=demonstrate(a.output);write_json(a.summary,result);print(json.dumps(result,indent=2))
