"""Executable baselines share edit semantics and hard provenance constraints."""
from dair import Plan, plan

METHODS = ('dair', 'full', 'suffix_safe', 'dag', 'attr_dag')


def execution_plan(story, edit, method='dair'):
    if method == 'attr_dag':
        from attribute_baseline import attribute_plan
        return attribute_plan(story, edit)
    typed = plan(story, edit)  # Zero-risk execution; no oracle labels are read.
    ids = [s['id'] for s in story['scenes']]
    if method not in METHODS:
        raise ValueError('unknown execution method')
    if method == 'dair':
        return typed
    required = set(typed.regenerate)
    if method == 'full':
        selected = set(ids)
    elif method == 'suffix_safe':
        # Suffix alone misses global/earlier dependencies. Keep hard constraints.
        selected = required | set(ids[ids.index(edit['target']):])
    else:
        selected = required.copy()
    # Untyped DAG propagation for both conservative baselines, including order.
    if method != 'full':
        while True:
            more = {e['child'] for e in story.get('edges', []) if e['parent'] in selected}
            if more <= selected:
                break
            selected |= more
    return Plan(tuple(i for i in ids if i in selected),
                tuple(i for i in ids if i not in selected), (),
                sum(s['cost'] for s in story['scenes'] if s['id'] in selected), 0.0)
