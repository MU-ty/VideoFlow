"""Run designed diagnostics with a synthetic provider by default; remote is explicit."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from artifacts import write_json
from planning import METHODS
from research.mock_provider import MockProvider
from research.run_experiment import run
from research.run_dynamic import run_dynamic


def run_suite(suite, output, remote=False, server='http://127.0.0.1:8188', selected=None):
    suite, output = Path(suite), Path(output)
    if output.exists():
        raise ValueError('use a fresh suite output directory')
    output.mkdir(parents=True)
    cases = json.loads((suite / 'suite.json').read_text())['cases']
    if selected and set(selected) - {c['id'] for c in cases}:
        raise ValueError('unknown case selection')
    summary = []
    for case in cases:
        if selected and case['id'] not in selected:
            continue
        if case['mode'] == 'reference_plan_only':
            summary.append(dict(case=case['id'], status='not_executed', reason='H3 adapter has no reference loader; planning tested separately'))
            continue
        manifest = json.loads((suite / case['manifest']).read_text())
        edit = json.loads((suite / case['input']).read_text())
        for method in METHODS:
            options = dict(method=method, server=server, poll_seconds=5 if remote else 0,
                           timeout_seconds=21600 if remote else 30, max_inflight=2)
            if not remote:
                backend = MockProvider(); options.update(backend=backend, downloader=backend.download)
            runner = run_dynamic if case['mode'] == 'dynamic' else run
            report = runner(manifest, edit, output / case['id'] / method, **options)
            row = dict(case=case['id'], method=method, status=report['status'], evidence=report['evidence'])
            summary.append(row); write_json(output / 'suite_status.json', summary)
            if report['status'] != 'complete':
                raise RuntimeError(f"case failed or paused: {case['id']}/{method}; inspect its report")
    write_json(output / 'suite_status.json', summary)
    return summary


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('suite'); p.add_argument('--output', required=True)
    p.add_argument('--remote', action='store_true'); p.add_argument('--server', default='http://127.0.0.1:8188')
    p.add_argument('--cases', nargs='+'); a = p.parse_args()
    print(json.dumps(run_suite(a.suite, a.output, a.remote, a.server, a.cases), indent=2))
