"""Deterministic research planner. Scores are not calibrated probabilities.

Input annotations describe generation inputs, not benchmark labels. Exact search
is intentionally limited to 20 uncertain scenes; no global video-quality claim.
"""
from dataclasses import dataclass
from itertools import combinations
import math

@dataclass(frozen=True)
class Plan:
    regenerate: tuple
    reuse: tuple
    validation: tuple
    estimated_cost: float
    residual_score: float


def plan(story, edit, risk_budget=0.0):
    scenes = story['scenes']
    ids = [s['id'] for s in scenes]
    if len(ids) != len(set(ids)) or edit['target'] not in ids:
        raise ValueError('duplicate or unknown scene')
    if not math.isfinite(risk_budget) or risk_budget < 0:
        raise ValueError('invalid risk budget')
    by_id = {s['id']: s for s in scenes}
    for s in scenes:
        if not math.isfinite(s['cost']) or s['cost'] < 0:
            raise ValueError('invalid cost')
    edges = story.get('edges', [])
    for e in edges:
        if e['parent'] not in by_id or e['child'] not in by_id:
            raise ValueError('unknown dependency endpoint')
        if ids.index(e['parent']) >= ids.index(e['child']):
            raise ValueError('dependencies must follow scene order')
        if e['kind'] not in ('state', 'reference', 'order'):
            raise ValueError('unknown dependency type')
    key = edit['key']
    start = ids.index(edit['target'])
    scope = edit.get('scope', 'local')
    if scope not in ('local', 'onward', 'global'):
        raise ValueError('unknown scope')
    eligible = set(ids if scope == 'global' else ids[start:] if scope == 'onward' else [edit['target']])
    required = {edit['target']}
    # Reads are explicit entity.attribute annotations, e.g. hero.costume.
    required |= {s['id'] for s in scenes if s['id'] in eligible and key in s.get('reads', [])}
    def close(selected):
        selected = set(selected)
        while True:
            more = {e['child'] for e in edges if e['parent'] in selected and
                    (e['kind'] == 'reference' or
                     (e['kind'] == 'state' and key in e.get('keys', [])))}
            if more <= selected:
                return selected
            selected |= more
    required = close(required)
    scores = {}
    for s in scenes:
        score = s.get('uncertain', {}).get(key, 0.0)
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError('invalid uncertainty score')
        if s['id'] in eligible and s['id'] not in required and score > 0:
            scores[s['id']] = score
    if len(scores) > 20:
        raise ValueError('exact prototype supports at most 20 uncertain scenes')
    best = None
    for count in range(len(scores) + 1):
        for subset in combinations(scores, count):
            selected = close(required | set(subset))
            residual = sum(v for k, v in scores.items() if k not in selected)
            if residual > risk_budget + 1e-12:
                continue
            cost = sum(by_id[k]['cost'] for k in selected)
            ordered = tuple(k for k in ids if k in selected)
            candidate = (cost, len(selected), ordered, residual)
            if best is None or candidate < best:
                best = candidate
    cost, _, selected, residual = best
    return Plan(selected, tuple(k for k in ids if k not in selected),
                tuple(k for k in scores if k not in selected), cost, residual)


def baseline(story, edit, method):
    ids = [s['id'] for s in story['scenes']]
    if method == 'full': return set(ids)
    if method == 'suffix': return set(ids[ids.index(edit['target']):])
    if method != 'dag': raise ValueError('unknown baseline')
    selected = {edit['target']}
    while True:
        more = {e['child'] for e in story.get('edges', []) if e['parent'] in selected}
        if more <= selected: return selected
        selected |= more

if __name__ == '__main__':
    import argparse
    from dataclasses import asdict
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', help='JSON object with story and edit fields')
    parser.add_argument('--risk-budget', type=float, default=0.0)
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text())
    print(json.dumps(asdict(plan(payload['story'], payload['edit'], args.risk_budget)), indent=2))
