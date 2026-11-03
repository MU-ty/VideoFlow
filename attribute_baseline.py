"""Independent attribute-aware worklist baseline under the shared declared graph.

Uses no DAIR selection routine, labels, uncertainty scores, or generated outputs.
It is expected to tie zero-risk DAIR on complete explicit dependency declarations.
"""
from collections import deque
import math
from dair import Plan  # Shared result data type only, not the DAIR planner.


def attribute_plan(story, edit):
    nodes = story['scenes']; ids = [n['id'] for n in nodes]
    if not ids or len(set(ids)) != len(ids) or edit['target'] not in ids:
        raise ValueError('empty, duplicate or unknown asset')
    positions = {k: i for i, k in enumerate(ids)}
    if edit.get('scope', 'local') not in ('local', 'onward', 'global'):
        raise ValueError('unknown scope')
    for n in nodes:
        if not math.isfinite(n['cost']) or n['cost'] < 0:
            raise ValueError('invalid cost')
        if n.get('uncertain'):
            raise ValueError('attribute baseline requires explicit dependencies')
    outgoing = {k: [] for k in ids}
    for e in story.get('edges', []):
        if e['parent'] not in positions or e['child'] not in positions:
            raise ValueError('unknown dependency endpoint')
        if positions[e['parent']] >= positions[e['child']]:
            raise ValueError('dependencies must follow scene order')
        if e['kind'] not in ('state', 'reference', 'order'):
            raise ValueError('unknown dependency type')
        outgoing[e['parent']].append(e)
    key, target = edit['key'], edit['target']
    scope = edit.get('scope', 'local')
    selected = {target}
    for n in nodes:
        eligible = scope == 'global' or (scope == 'onward' and positions[n['id']] >= positions[target]) or n['id'] == target
        if eligible and key in n.get('reads', []):
            selected.add(n['id'])
    queue = deque(k for k in ids if k in selected)
    while queue:
        parent = queue.popleft()
        for edge in outgoing[parent]:
            invalidates = edge['kind'] == 'reference' or (edge['kind'] == 'state' and key in edge.get('keys', []))
            child = edge['child']
            if invalidates and child not in selected:
                selected.add(child); queue.append(child)
    return Plan(tuple(k for k in ids if k in selected), tuple(k for k in ids if k not in selected), (),
                sum(n['cost'] for n in nodes if n['id'] in selected), 0.0)
