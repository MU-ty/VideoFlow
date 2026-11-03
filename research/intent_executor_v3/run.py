"""Standard-library-only diagnostic. Never sends gold state or annotations to models."""
import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request
from cases import suite

from copy import deepcopy
from engine import decode, execute, OperationError

ARMS = ['state', 'patch', 'program']
RULES = """你管理多镜头视频的符号状态，不生成视频。每轮只做用户明确要求的修改。
color只能white/black/blue/red，light只能day/sunset，subtitle是字符串。
撤销某轮某字段=恢复该轮开始前的值，只覆盖点名字段，不重放后续操作。
复制读取本轮开始时的源值，不建立持续绑定。全局修改展开为各镜头。
history含实际执行的before/after和模型原始输出，各方法都有同样的记录机制；记录不是正确答案。
撤销先定位目标轮及目标字段，再查before；不要把初值或该轮after误当before。
严格只输出JSON，无markdown。无自动重试。"""
POLICIES = {
'state': '输出{"state":完整状态表}，必须包括全部镜头、全部字段。先查历史before，再更新指定字段，其余保持。',
'patch': '输出{"ops":[{"op":"set","shot":"镜头ID","field":"属性名","value":"新值"}]}。只列用户指定字段。复制和恢复的值需要你查历史并算出，仍使用set。无修改输出空数组。每个目标字段仅出现一次。',
'program': '输出{"ops":[操作对象]}。直接赋值用{"op":"set","shot":"镜头ID","field":"属性名","value":"新值"}；历史恢复用{"op":"restore_before","shot":"镜头ID","field":"属性名","turn":历史轮次整数}；本轮开始时复制用{"op":"copy_current","shot":"目标镜头ID","field":"属性名","source":"源镜头ID"}。程序计算复制/恢复值。每个对象只用所列键，每个目标字段仅出现一次；无修改输出空数组。'
}


def fingerprint(x):
    return hashlib.sha256(json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def messages(case, turn, arm, history, state):
    public={'initial':case['initial'], 'history':history, 'turn':turn+1,
            'instruction':case['turns'][turn]['instruction'], 'last_predicted_state':state}
    return [{'role':'system','content':RULES+'\n'+POLICIES[arm]},
            {'role':'user','content':json.dumps(public,ensure_ascii=False)}]


def source_hashes():
    root=Path(__file__).resolve().parent
    return {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in ['run.py','cases.py','engine.py']}


def prepare(out):
    out.mkdir(parents=True, exist_ok=True)
    protocol = {'version': 3, 'cases': suite(), 'rules': RULES, 'policies': POLICIES,
                'arms': ARMS, 'source_hashes': source_hashes(), 'evidence': 'authored_symbolic_diagnostic_not_video_quality',
                'gate': 'Exploratory only: program must exceed BOTH state and patch in eval sequence successes, with net paired wins in at least two non-control families against each, no control regression and complete records. Ties fail. Actual costs required; no novelty or significance claim.'}
    protocol = json.loads(json.dumps(protocol, ensure_ascii=False))
    path = out/'protocol.json'
    if path.exists() and json.loads(path.read_text()) != protocol:
        raise ValueError('Existing protocol differs; choose a new output directory')
    path.write_text(json.dumps(protocol, ensure_ascii=False, indent=2))
    (out/'protocol.sha256').write_text(fingerprint(protocol)+'\n')
    return protocol


def load_protocol(out):
    p = json.loads((out/'protocol.json').read_text())
    if fingerprint(p) != (out/'protocol.sha256').read_text().strip():
        raise ValueError('protocol fingerprint mismatch')
    if p.get('source_hashes') != source_hashes() or p['arms'] != ARMS or p['rules'] != RULES or p['policies'] != POLICIES or p['cases'] != json.loads(json.dumps(suite())):
        raise ValueError('Runner/fixture differs from frozen protocol')
    return p


def call_model(base, model, key, msgs, max_tokens, timeout):
    payload = {'model': model, 'messages': msgs, 'temperature': 0, 'max_tokens': max_tokens}
    request = urllib.request.Request(base.rstrip('/')+'/chat/completions',
        data=json.dumps(payload).encode(), headers={'Authorization': 'Bearer '+key, 'Content-Type':'application/json'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    return result['choices'][0]['message']['content'], result.get('usage'), result.get('model'), result.get('system_fingerprint')


def run(args):
    p = load_protocol(args.output)
    key = os.environ.get('INTENT_API_KEY', '')
    if not key:
        raise ValueError('Set INTENT_API_KEY locally; no request has been made')
    if not args.base_url.startswith('https://') and not args.base_url.startswith(('http://127.0.0.1:', 'http://localhost:')):
        raise ValueError('Use HTTPS or a local endpoint')
    config = {'protocol_hash': fingerprint(p), 'model': args.model, 'base_url': args.base_url,
              'split': args.split, 'repeats': args.repeats, 'max_tokens': args.max_tokens, 'temperature': 0,
              'evidence': 'real_model_symbolic_state_only', 'timeout': args.timeout}
    dest = args.output/(args.split+'-run'); dest.mkdir(exist_ok=True)
    if any(f.name != 'configuration.json' for f in dest.glob('*.json')):
        raise ValueError('Existing run records: no automatic resume/retry; preserve directory')
    lock = dest/'configuration.json'
    if lock.exists() and json.loads(lock.read_text()) != config:
        raise ValueError('Run configuration changed; use a fresh output directory')
    lock.write_text(json.dumps(config, indent=2))
    for ci, case in enumerate(c for c in p['cases'] if c['split'] == args.split):
        for repeat in range(args.repeats):
            # Rotate arm order, avoiding a single fixed order on every case.
            offset = (ci+repeat)%len(ARMS)
            for arm in ARMS[offset:]+ARMS[:offset]:
                path = dest/f"{case['id']}-{repeat}-{arm}.json"
                if path.exists():
                    raise ValueError(f'Existing result or pending intent: {path.name}; no automatic retry. Score completed records or use a new directory.')
                record = {'case':case['id'], 'repeat':repeat, 'arm':arm, 'status':'running', 'turns':[]}
                with path.open('x') as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, indent=2))
                history=[]; state=deepcopy(case['initial'])
                for t, step in enumerate(case['turns']):
                    msgs = messages(case,t,arm,history,state)
                    row={'turn':t+1, 'messages':msgs, 'status':'request_pending'}
                    record['turns'].append(row)
                    path.write_text(json.dumps(record,ensure_ascii=False,indent=2))
                    before=deepcopy(state)
                    started=time.monotonic()
                    try:
                        raw, usage, model, system_fp = call_model(args.base_url,args.model,key,msgs,args.max_tokens,args.timeout)
                        row.update(raw=raw,usage=usage,returned_model=model,system_fingerprint=system_fp,status='returned')
                        try:
                            state=decode(raw,arm,state,history); row['prediction']=deepcopy(state)
                        except (ValueError,KeyError,TypeError) as err:
                            row['format_error']=str(err)
                            row['rejection_kind']='operation' if isinstance(err,OperationError) else 'json_schema'
                            # Keep last valid state for the next input; mark this turn incorrect.
                        history.append({'turn':t+1,'instruction':step['instruction'],'assistant_raw':raw,
                                        'before':before,'after':deepcopy(state),'accepted':'prediction' in row})
                    except Exception as err:
                        row.update(status='transport_error',error_type=type(err).__name__)
                        record['status']='interrupted'
                        raise
                    finally:
                        row['latency_seconds']=time.monotonic()-started
                        path.write_text(json.dumps(record,ensure_ascii=False,indent=2))
                record['status']='complete'; path.write_text(json.dumps(record,ensure_ascii=False,indent=2))
                print(path.name,flush=True)


def score(args):
    p=load_protocol(args.output); cases={c['id']:c for c in p['cases']}
    dest=args.output/(args.split+'-run'); config=json.loads((dest/'configuration.json').read_text())
    if config['protocol_hash'] != fingerprint(p) or config['split'] != args.split:
        raise ValueError('run/protocol mismatch')
    records=[json.loads(f.read_text()) for f in sorted(dest.glob('*.json')) if f.name!='configuration.json']
    seen=set()
    for r in records:
        key=(r['case'],r['repeat'],r['arm'])
        if key in seen or r['case'] not in cases or cases[r['case']]['split']!=args.split or r['arm'] not in ARMS or type(r['repeat'])!=int or not 0<=r['repeat']<config['repeats']:
            raise ValueError('duplicate or unexpected record')
        seen.add(key)
    byarm={}; pairs={}
    for arm in ARMS:
        totals=collections.Counter(); family=collections.defaultdict(list)
        for r in records:
            if r['arm']!=arm:continue
            c=cases[r['case']]; totals['records']+=1
            success = r['status']=='complete' and len(r['turns'])==len(c['turns'])
            for t,step in enumerate(c['turns']):
                row=r['turns'][t] if t<len(r['turns']) else {}; pred=row.get('prediction')
                exact=pred==step['expected']; success &= exact
                totals['turns']+=1;totals['correct_turns']+=exact
                totals['format_errors']+=('format_error' in row)
                totals['operation_rejections']+=row.get('rejection_kind')=='operation'
                if row.get('messages'):
                    supplied=json.loads(row['messages'][1]['content'])
                    local_target=execute(step['annotation_ops'],supplied['last_predicted_state'],supplied['history'])
                    totals['local_transition_correct']+=pred==local_target
                    totals['local_transition_observed']+=1
                if arm=='program' and row.get('raw'):
                    try:
                        ops=json.loads(row['raw'])['ops']
                        canonical=lambda xs: sorted(json.dumps(x,sort_keys=True,ensure_ascii=False) for x in xs)
                        totals['canonical_operation_matches']+=canonical(ops)==canonical(step['annotation_ops'])
                    except (ValueError,KeyError,TypeError): pass
                totals['unreturned_turns']+=row.get('status')!='returned'
                totals['latency_seconds']+=row.get('latency_seconds',0)
                usage=row.get('usage')
                totals['missing_usage_turns']+=usage is None
                if usage:
                    for k in ['prompt_tokens','completion_tokens','total_tokens']:totals[k]+=usage.get(k,0)
                before=c['initial'] if t==0 else c['turns'][t-1]['expected']
                addressed={tuple(x) for x in step['addressed']}
                for shot in before:
                    for field in before[shot]:
                        if (shot,field) not in addressed:
                            totals['preservation_fields']+=1
                            totals['preservation_violations']+=pred is None or pred[shot][field]!=step['expected'][shot][field]
                            if pred is None: totals['preservation_unscorable_fields']+=1
                            elif row.get('messages'):
                                totals['new_off_target_changes']+=pred[shot][field]!=supplied['last_predicted_state'][shot][field]
                if any(op['op']=='restore_before' for op in step['annotation_ops']):
                    totals['undo_turns']+=1;totals['correct_undo_turns']+=exact
            totals['correct_sequences']+=success;family[c['family']].append(bool(success))
            pairs[(r['case'],r['repeat'],arm)]=bool(success)
        expected=sum(c['split']==args.split for c in p['cases'])*config['repeats']
        byarm[arm]={'counts':dict(totals),'expected_records':expected,'expected_turns':sum(len(c['turns']) for c in p['cases'] if c['split']==args.split)*config['repeats'],'sequence_success_rate':totals['correct_sequences']/expected,
                    'family_sequences':{k:{'correct':sum(v),'observed':len(v)} for k,v in family.items()}}
    comparisons={}
    for baseline in ['state','patch']:
        comparison=collections.Counter()
        for c in p['cases']:
            if c['split']!=args.split:continue
            for repeat in range(config['repeats']):
                a=pairs.get((c['id'],repeat,'program'));b=pairs.get((c['id'],repeat,baseline))
                if a is None or b is None:comparison['missing_pair']+=1
                elif a==b:comparison['tie']+=1
                elif a:comparison['program_win']+=1
                else:comparison['program_loss']+=1
        comparisons[baseline]=dict(comparison)
    complete=all(byarm[a]['counts'].get('records',0)==byarm[a]['expected_records'] and
                 byarm[a]['counts'].get('unreturned_turns',0)==0 for a in ARMS)
    gate_details={}
    for baseline in ['state','patch']:
        family_deltas=collections.Counter(); control_ok=True
        for c in p['cases']:
            if c['split']!=args.split: continue
            for repeat in range(config['repeats']):
                a=pairs.get((c['id'],repeat,'program'),False)
                b=pairs.get((c['id'],repeat,baseline),False)
                if c['family']=='control' and b and not a: control_ok=False
                if c['family']!='control': family_deltas[c['family']]+=int(a)-int(b)
        gate_details[baseline]={'family_net_wins':dict(family_deltas), 'control_no_regression':control_ok,
          'passes':complete and control_ok and sum(v>0 for v in family_deltas.values())>=2 and
           byarm['program']['counts'].get('correct_sequences',0)>byarm[baseline]['counts'].get('correct_sequences',0)}
    report={'configuration':config,'metrics':byarm,'paired_program_vs_baselines':comparisons,
            'continuation_gate':{'evaluated':args.split=='eval','complete':complete, 'pass':args.split=='eval' and all(v['passes'] for v in gate_details.values()),'comparisons':gate_details},
            'claim':'Local transition uses gold operations OFFLINE on the arm own history; canonical op match is syntax-sensitive and not a complete semantic parser metric. No human-video-quality or independent-story generalization result; missing records are failures in sequence denominator. Token totals are observed lower bounds when usage is missing.'}
    (dest/'report.txt').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('command',choices=['prepare','run','score'])
    ap.add_argument('--output',type=Path,required=True);ap.add_argument('--split',choices=['dev','eval'],default='dev')
    ap.add_argument('--base-url',default=os.environ.get('INTENT_BASE_URL','http://127.0.0.1:11434/v1'))
    ap.add_argument('--model',default=os.environ.get('INTENT_MODEL',''))
    ap.add_argument('--repeats',type=int,default=1);ap.add_argument('--max-tokens',type=int,default=2048);ap.add_argument('--timeout',type=float,default=180)
    args=ap.parse_args()
    if args.repeats<1 or args.max_tokens<1:ap.error('positive repeats and max tokens required')
    if args.command=='prepare':
        p=prepare(args.output); print(json.dumps({'protocol_hash':fingerprint(p),'sequences':len(p['cases']),'real_model_runs':0}))
    elif args.command=='run':
        if not args.model:ap.error('--model or INTENT_MODEL is required')
        run(args)
    else:score(args)

if __name__=='__main__':main()
