"""Replay frozen visual selections through SQLite and the synthetic worker."""
import argparse
import json
from pathlib import Path

from run_egdr_runtime import AssetRuntime, KEY, POLICIES, apply_policy, asset
from asset_worker import pass_once
from research.mock_provider import MockProvider


def drain(runtime, backend, count):
    for _ in range(count + 1):
        events = pass_once(runtime, backend, max_submit=count)
        if any(event['action'] not in ('complete', 'submitted') for event in events):
            raise RuntimeError(f'synthetic worker error: {events}')
        if (timeline := runtime.snapshot()['timeline']) is not None:
            return {item['asset']: item for item in timeline}
    raise RuntimeError('synthetic publication did not finish')


def replay(report):
    if not report['visual_evaluation_complete'] or not report['verification_evaluation_complete']:
        raise ValueError('replay requires a complete verification report')
    rows = []
    for row in report['cases']:
        predictions = {item['method']: set(item['selected']) for item in row['results']}
        # The old probe scores only the candidates. An explicit synthetic anchor
        # supplies the runtime's mandatory direct edit and is excluded from metrics.
        anchor = 'anchor'
        candidates = sorted(predictions['full'])
        manifest = dict(assets=[asset(shot, shot + ' {{target.color}}', {KEY: 'before'}, index)
                                for index, shot in enumerate([anchor, *candidates])])
        case = dict(id=row['case'], target=anchor, edit='after')
        for policy, method in POLICIES.items():
            if method not in predictions:
                continue
            runtime, backend = AssetRuntime(':memory:'), MockProvider()
            try:
                runtime.initialize(manifest)
                count = len(manifest['assets'])
                before = drain(runtime, backend, count)
                plan = apply_policy(runtime, case, predictions[method], before,
                                    {'frozen_method': method, 'selected': sorted(predictions[method])}, policy)
                after = drain(runtime, backend, count)
                changed = {shot for shot in candidates if after[shot] != before[shot]}
                if changed != predictions[method] or after[anchor]['revision'] != 2:
                    raise RuntimeError('published lineage does not match the frozen selection')
                gold = set(row['gold'])
                rows.append(dict(case=row['case'], policy=policy, changed=sorted(changed),
                                 missed=len(gold - changed), excess=len(changed - gold),
                                 repairs=len(plan.get('state_repairs', [])), published=True))
            finally:
                runtime.close()
    totals = {policy: {metric: sum(row[metric] for row in rows if row['policy'] == policy)
                       for metric in ('published', 'missed', 'excess', 'repairs')}
              for policy in dict.fromkeys(row['policy'] for row in rows)}
    return dict(evidence='synthetic_execution_of_frozen_selections', totals=totals, rows=rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    print(json.dumps(replay(json.loads(args.report.read_text(encoding='utf-8'))), indent=2))
