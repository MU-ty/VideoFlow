"""Restricted data operations, no eval/exec; transactional application."""
from copy import deepcopy
import json


class OperationError(ValueError):
    pass


def validate_state(state, initial):
    if not isinstance(state, dict) or set(state) != set(initial):
        raise OperationError('scene schema')
    for attrs in state.values():
        if not isinstance(attrs, dict) or set(attrs) != {'color', 'light', 'subtitle'}:
            raise OperationError('field schema')
        if attrs['color'] not in ('white','black','blue','red') or attrs['light'] not in ('day','sunset') or not isinstance(attrs['subtitle'], str):
            raise OperationError('value schema')
    return deepcopy(state)


def execute(ops, state, history, patch_only=False):
    if not isinstance(ops, list) or len(ops) > 30:
        raise OperationError('ops must be list of at most 30 operations')
    result=deepcopy(state); written=set()
    for op in ops:
        if not isinstance(op, dict): raise OperationError('operation object required')
        kind=op.get('op')
        schemas={'set':{'op','shot','field','value'},
                 'restore_before':{'op','shot','field','turn'},
                 'copy_current':{'op','shot','field','source'}}
        if kind not in schemas or set(op)!=schemas[kind] or (patch_only and kind!='set'):
            raise OperationError('operation schema')
        shot,field=op['shot'],op['field']
        if not isinstance(shot,str) or not isinstance(field,str) or shot not in state or field not in state[shot]:
            raise OperationError('unknown target')
        if (shot,field) in written: raise OperationError('duplicate target in transaction')
        written.add((shot,field))
        if kind=='set': value=op['value']
        elif kind=='copy_current':
            if not isinstance(op['source'],str) or op['source'] not in state: raise OperationError('unknown source')
            value=state[op['source']][field]  # all reads use turn-start snapshot
        else:
            turn=op['turn']
            if type(turn)!=int or not 1<=turn<=len(history): raise OperationError('unknown prior turn')
            value=history[turn-1]['before'][shot][field]
        result[shot][field]=value
    return validate_state(result,state)


def decode(raw,arm,state,history):
    def unique(pairs):
        obj={}
        for k,v in pairs:
            if k in obj: raise ValueError('duplicate JSON key')
            obj[k]=v
        return obj
    x=json.loads(raw,object_pairs_hook=unique)
    key='state' if arm=='state' else 'ops'
    if not isinstance(x,dict) or set(x)!={key}: raise ValueError('top-level schema')
    if arm=='state': return validate_state(x['state'],state)
    return execute(x['ops'],state,history,patch_only=arm=='patch')
