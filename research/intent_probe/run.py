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

ARMS = ['history', 'state', 'ledger']
RULES = '''你是视频项目连续编辑状态管理器。镜头为S2、S4、S6，每个有color、light、subtitle。
仅执行用户明确要求的改变，其他字段保持。颜色用white/blue/black/red；光照用day/sunset。
历史复制是当轮开始时的值，不建立持续绑定。撤销指定轮次指定字段，是恢复到该轮开始前的值，保留其余后续改动。
只输出JSON对象，键state和memory。state必须包含全部镜头全部字段，不能加字段。
memory是供你下轮使用的JSON对象；不得把用户未要求的变化当作有效约束。
本实验输入不含歧义；无需提问，不生成视频。'''
POLICIES = {
    'history': '从完整交互历史推导当前状态。memory输出空对象。',
    'state': '利用完整交互历史和最近预测状态表，核对当前轮的覆盖、例外、撤销、复制及应保持字段。历史与预测有冲突时按用户要求纠错。memory输出空对象。',
    'ledger': '利用完整交互历史、最近预测状态和你自己的memory，更新显式约束记录。memory含records数组，每项可记录turn、shots、field、before、after、overrides；记录局部例外、覆盖关系和指定撤销的历史值。不得只靠最近一轮。',
}


def fingerprint(x):
    return hashlib.sha256(json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_state(x, initial):
    if not isinstance(x, dict) or set(x) != set(initial):
        raise ValueError('state scene schema')
    for shot, attrs in x.items():
        if not isinstance(attrs, dict) or set(attrs) != set(initial[shot]):
            raise ValueError('state field schema')
        if attrs['color'] not in ['white', 'blue', 'black', 'red'] or attrs['light'] not in ['day', 'sunset'] or not isinstance(attrs['subtitle'], str):
            raise ValueError('state value schema')
    return x


def parse_output(raw, initial):
    # Strict JSON; markdown fences count as format errors under the frozen protocol.
    x = json.loads(raw)
    state = validate_state(x['state'], initial)
    if not isinstance(x.get('memory'), dict):
        raise ValueError('memory schema')
    return state, x['memory']


def messages(case, turn, arm, history, state, memory):
    public = {'initial': case['initial'], 'history': history,
              'turn': turn+1, 'instruction': case['turns'][turn]['instruction']}
    if arm in ['state', 'ledger']:
        public['last_predicted_state'] = state
    if arm == 'ledger':
        public['self_generated_memory'] = memory
    return [{'role': 'system', 'content': RULES + '\n' + POLICIES[arm]},
            {'role': 'user', 'content': json.dumps(public, ensure_ascii=False)}]


def prepare(out):
    out.mkdir(parents=True, exist_ok=True)
    protocol = {'version': 1, 'cases': suite(), 'rules': RULES, 'policies': POLICIES,
                'arms': ARMS, 'evidence': 'authored_symbolic_diagnostic_not_video_quality',
                'gate': 'Exploratory only: require positive eval paired sequence-success difference against state arm, no control regression, and report all costs; inspect before any independent validation.'}
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
    if p['rules'] != RULES or p['policies'] != POLICIES or p['cases'] != json.loads(json.dumps(suite())):
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
    lock = dest/'configuration.json'
    if lock.exists() and json.loads(lock.read_text()) != config:
        raise ValueError('Run configuration changed; use a fresh output directory')
    lock.write_text(json.dumps(config, indent=2))
    for ci, case in enumerate(c for c in p['cases'] if c['split'] == args.split):
        for repeat in range(args.repeats):
            # Rotate arm order, avoiding a single fixed order on every case.
            offset = (ci+repeat)%3
            for arm in ARMS[offset:]+ARMS[:offset]:
                path = dest/f"{case['id']}-{repeat}-{arm}.json"
                if path.exists():
                    raise ValueError(f'Existing result or pending intent: {path.name}; no automatic retry. Score completed records or use a new directory.')
                record = {'case':case['id'], 'repeat':repeat, 'arm':arm, 'status':'running', 'turns':[]}
                with path.open('x') as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, indent=2))
                history=[]; state=case['initial']; memory={}
                for t, step in enumerate(case['turns']):
                    msgs = messages(case,t,arm,history,state,memory)
                    row={'turn':t+1, 'messages':msgs, 'status':'request_pending'}
                    record['turns'].append(row)
                    path.write_text(json.dumps(record,ensure_ascii=False,indent=2))
                    started=time.monotonic()
                    try:
                        raw, usage, model, system_fp = call_model(args.base_url,args.model,key,msgs,args.max_tokens,args.timeout)
                        row.update(raw=raw,usage=usage,returned_model=model,system_fingerprint=system_fp,status='returned')
                        try:
                            state,memory=parse_output(raw,case['initial']); row['prediction']=state
                        except (ValueError,KeyError,TypeError) as err:
                            row['format_error']=str(err)
                            # Keep last valid state for the next input; mark this turn incorrect.
                        history.append({'turn':t+1,'instruction':step['instruction'],'assistant_raw':raw})
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
                if any(op[0]=='restore' for op in step['annotation_ops']):
                    totals['undo_turns']+=1;totals['correct_undo_turns']+=exact
            totals['correct_sequences']+=success;family[c['family']].append(bool(success))
            pairs[(r['case'],r['repeat'],arm)]=bool(success)
        expected=sum(c['split']==args.split for c in p['cases'])*config['repeats']
        byarm[arm]={'counts':dict(totals),'expected_records':expected,'sequence_success_rate':totals['correct_sequences']/expected,
                    'family_sequences':{k:{'correct':sum(v),'observed':len(v)} for k,v in family.items()}}
    comparison=collections.Counter()
    for c in p['cases']:
        if c['split']!=args.split:continue
        for repeat in range(config['repeats']):
            a=pairs.get((c['id'],repeat,'ledger'));b=pairs.get((c['id'],repeat,'state'))
            if a is None or b is None: comparison['missing_pair']+=1
            elif a==b:comparison['tie']+=1
            elif a:comparison['ledger_win']+=1
            else:comparison['ledger_loss']+=1
    report={'configuration':config,'metrics':byarm,'paired_ledger_vs_state':dict(comparison),
            'claim':'No human-video-quality or independent-story generalization result; missing records are failures in sequence denominator. Token totals are observed lower bounds when usage is missing.'}
    (dest/'report.txt').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('command',choices=['prepare','run','score'])
    ap.add_argument('--output',type=Path,required=True);ap.add_argument('--split',choices=['dev','eval'],default='dev')
    ap.add_argument('--base-url',default=os.environ.get('INTENT_BASE_URL','http://127.0.0.1:8000/v1'))
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
