"""Offline semantic-reuse research scaffold. No VLM, GPU or runtime rebinding.

Conformal correction is borrowed from Angelopoulos et al., Conformal Risk
Control (ICLR 2024). Closure-aware ranking is a heuristic, not a novelty claim.
Loss measures missed invalid OLD assets, not correctness of generated repairs.
"""
import math

ORDERS=('score','score_per_cost','closure_gain')
GRID=tuple(i/20 for i in range(21))


def validate(features):
    if set(features)!={'story_id','nodes','hard','reference_edges'}:
        raise ValueError('features schema excludes labels and hidden oracle state')
    if not isinstance(features['story_id'],str) or not features['story_id']:
        raise ValueError('story id required')
    nodes=features['nodes'];ids=[n['id'] for n in nodes]
    if not ids or len(ids)!=len(set(ids)):raise ValueError('unique nonempty nodes required')
    for n in nodes:
        if set(n)!={'id','cost','score'} or not isinstance(n['id'],str):raise ValueError('node schema')
        if not math.isfinite(n['cost']) or n['cost']<=0:raise ValueError('positive declared costs required')
        if not math.isfinite(n['score']) or not 0<=n['score']<=1:raise ValueError('bounded scores required')
    if not set(features['hard'])<=set(ids):raise ValueError('unknown mandatory asset')
    for edge in features['reference_edges']:
        if len(edge)!=2 or any(a not in ids for a in edge) or ids.index(edge[0])>=ids.index(edge[1]):
            raise ValueError('reference DAG must follow node order')
    return ids


def close(features, selected):
    selected=set(selected)
    while True:
        extra={child for parent,child in features['reference_edges'] if parent in selected}
        if extra<=selected:return selected
        selected|=extra


def ladder(features,order='closure_gain'):
    """A nested repair ladder based solely on frozen observable features."""
    ids=validate(features)
    if order not in ORDERS:raise ValueError('unknown ordering')
    by={n['id']:n for n in features['nodes']};selected=close(features,features['hard'])
    sets=[frozenset(selected)]
    while len(selected)<len(ids):
        candidates=[]
        for pos,a in enumerate(ids):
            if a in selected:continue
            extra=close(features,selected|{a})-selected
            if order=='score':utility=by[a]['score']
            elif order=='score_per_cost':utility=by[a]['score']/by[a]['cost']
            else:
                utility=sum(by[b]['score'] for b in extra)/sum(by[b]['cost'] for b in extra)
            candidates.append((-utility,pos,extra))
        extra=min(candidates,key=lambda item:item[:2])[2]
        selected|=extra;sets.append(frozenset(selected))
    initial=sum(by[a]['cost'] for a in sets[0]);remaining=sum(n['cost'] for n in by.values())-initial
    progress=[(sum(by[a]['cost'] for a in s)-initial)/remaining if remaining else 0. for s in sets]
    progress[0]=0.
    if remaining:progress[-1]=1.
    return list(zip(progress,sets))


def select(features,conservativeness,order='closure_gain'):
    if not math.isfinite(conservativeness) or not 0<=conservativeness<=1:raise ValueError('parameter must be 0..1')
    chain=ladder(features,order)
    selected=next(s for fraction,s in reversed(chain) if fraction<=conservativeness)
    ids=[n['id'] for n in features['nodes']]
    return dict(regenerate=[a for a in ids if a in selected],reuse=[a for a in ids if a not in selected],
                declared_repair_cost=sum(n['cost'] for n in features['nodes'] if n['id'] in selected))


def loss(features,labels,selected):
    ids=validate(features)
    if set(labels)!=set(ids) or any(type(v)!=bool for v in labels.values()):raise ValueError('complete boolean labels required')
    return int(any(labels[a] and a not in selected['regenerate'] for a in ids))


def calibrate(examples,alpha=.1,order='closure_gain',grid=GRID):
    """One example per independent story; order/scorer/grid frozen beforehand.

    Uses (sum of binary episode losses + 1)/(n+1), not an error-confidence
    bound conditional on this calibration sample. The all-repair endpoint is
    a deterministic fallback for old-asset omission only.
    """
    if not examples:raise ValueError('calibration data required')
    if not math.isfinite(alpha) or not 0<alpha<1:raise ValueError('alpha must be 0..1')
    if tuple(sorted(set(grid)))!=tuple(grid) or not grid or grid[0]!=0 or grid[-1]!=1:raise ValueError('ordered fixed grid including 0 and 1 required')
    if any(not math.isfinite(t) or not 0<=t<=1 for t in grid):raise ValueError('invalid grid')
    stories=[f['story_id'] for f,_ in examples]
    if len(stories)!=len(set(stories)):raise ValueError('split by independent story, not repeated edits')
    n=len(examples);curve=[]
    for t in grid:
        losses=[loss(f,y,select(f,t,order)) for f,y in examples]
        corrected=(sum(losses)+1)/(n+1);curve.append(dict(parameter=t,empirical_loss=sum(losses)/n,corrected_risk=corrected))
        if corrected<=alpha:
            return dict(parameter=t,alpha=alpha,order=order,calibration_stories=n,curve=curve,
                selection='borrowed CRC correction; exchangeability and frozen-policy assumptions required')
    return dict(parameter=1.,alpha=alpha,order=order,calibration_stories=n,curve=curve,
        selection='deterministic all-repair fallback; insufficient calibration support',
        scope='No guarantee for newly generated clips, verifier errors, adaptive future edits or distribution shifts.')

