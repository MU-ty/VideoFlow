"""Synthetic decision-only benchmark; never invokes a video model."""
import argparse
import csv
import json
from pathlib import Path
import sys
from time import perf_counter
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dair import plan, baseline

def run(cases):
    rows = []
    for case in cases:
        story, edit = case['story'], case['edit']
        gold = set(case['expected'])  # Evaluation only; never sent to planner.
        for method in ('full', 'suffix', 'dag', 'dair'):
            begin = perf_counter()
            selected = set(plan(story, edit).regenerate) if method == 'dair' else baseline(story, edit, method)
            ms = (perf_counter() - begin) * 1000
            tp = len(selected & gold)
            precision = tp / len(selected) if selected else float(not gold)
            recall = tp / len(gold) if gold else 1.0
            rows.append(dict(case=case['id'], method=method, precision=precision,
                recall=recall, exact=selected == gold,
                estimated_cost=sum(s['cost'] for s in story['scenes'] if s['id'] in selected),
                planner_ms=ms, regenerated=sorted(selected), evidence='synthetic_annotations'))
    return rows

if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--fixtures', default='fixtures/edits.json'); p.add_argument('--output', default='results/offline.json'); a = p.parse_args()
    rows = run(json.loads(Path(a.fixtures).read_text()))
    out = Path(a.output); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({'scope':'synthetic decision benchmark; costs are arbitrary units; planner_ms is local CPU time; no video quality, GPU latency or API cost measured', 'rows':rows}, indent=2)+'\n')
    with out.with_suffix('.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=rows[0]); w.writeheader(); w.writerows(rows)
    for method in ('full', 'suffix', 'dag', 'dair'):
        group = [r for r in rows if r['method'] == method]
        print(method, 'exact', sum(r['exact'] for r in group), '/', len(group), 'cost', sum(r['estimated_cost'] for r in group))
