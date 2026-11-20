"""Matched-budget replay of candidate verification on a shared VLM response bank."""
import argparse
import base64
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import time
from urllib.request import Request, urlopen

POLICIES = ('fixed', 'uncertainty', 'graph-aware', 'graph-static',
            'fixed-s2-first', 'fixed-s3-first')
FRACTIONS = ((.35, .60), (.10, .85), (.22, .73), (.45, .92))
BUDGETS = (3, 4, 5, 6)
SCREEN_FIELDS = ('block', 'shot', 'readable_linked_panels')


def load(path):
    return json.loads(path.read_text())


def save(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def closure(shots, edges):
    affected = set(shots)
    while True:
        expanded = affected | {child for parent, child in edges if parent in affected}
        if expanded == affected:
            return affected
        affected = expanded


def observations(raw, schema, case, *, require_complete_for_distinct=True):
    rows = json.loads(raw)['panels']
    fields = schema['properties']['panels']['items']['properties']
    if len(rows) != 2 or {p['panel'] for p in rows} != {1, 2}:
        raise ValueError('expected panels 1 and 2')
    votes = []
    for row in rows:
        if set(row) != set(fields) or type(row['complete']) is not bool or type(row['panel']) is not int:
            raise ValueError('malformed panel fields')
        if any('enum' in spec and row[field] not in spec['enum'] for field, spec in fields.items()):
            raise ValueError('unknown field value')
        linked = all(row[key] == 'yes' for key in ('host_visible', 'part_visible', 'same_instance'))
        marker = row['color'] + ' ' + row['shape']
        if linked and marker == case['target_marker']:
            votes.append('target')
        elif (linked and marker == case['distractor_marker']
              and (row['complete'] or not require_complete_for_distinct)):
            votes.append('distinct')
        else:
            votes.append('unknown')
    return votes


def state(history):
    positive = sum(view.count('target') for view in history)
    negative = sum(view.count('distinct') for view in history)
    negative_views = sum('distinct' in view for view in history)
    if positive >= 2 and negative == 0:
        verdict = 'confirmed'
    elif negative_views >= 2 and positive == 0:
        verdict = 'distinct'
    else:
        verdict = 'unknown'
    # Residual ambiguity is a heuristic, not a calibrated probability.
    ambiguity = (1 + min(positive, negative)) / (1 + abs(positive - negative))
    return verdict, ambiguity


def replay(case, bank, policy, budget, order):
    fixed_start = {'fixed-s2-first': 'S2', 'fixed-s3-first': 'S3'}.get(policy)
    priority = sorted(order, key=lambda shot: shot != fixed_start) if fixed_start else order
    history = {shot: [] for shot in order}
    trace = []

    def reveal(shot):
        index = len(history[shot])
        history[shot].append(bank[shot][index]['votes'])
        trace.append([shot, index + 1])

    for shot in order:
        reveal(shot)
    edges = case['edges']
    while len(trace) < budget:
        states = {shot: state(history[shot]) for shot in order}
        committed = closure({'S1'} | {s for s in order if states[s][0] == 'confirmed'}, edges)
        scores = {}
        for shot in priority:
            if len(history[shot]) == len(bank[shot]):
                continue
            # Evaluate the shot's marginal impact with its own confirmation removed.
            other = committed if states[shot][0] != 'confirmed' else closure(
                {'S1'} | {s for s in order if s != shot and states[s][0] == 'confirmed'}, edges)
            impact = len(closure({shot}, edges) - other)
            verdict, uncertainty = states[shot]
            if policy == 'fixed-recheck':
                score = -priority.index(shot)
            elif policy.startswith('fixed'):
                score = -len(history[shot])
            elif policy == 'graph-static':
                score = len(closure({shot}, edges)) / len(history[shot])
            else:
                score = (1 if verdict == 'unknown' else .25) * uncertainty / len(history[shot])
                if policy == 'graph-aware':
                    score *= impact
            scores[shot] = score
        reveal(max(scores, key=scores.get))
    decisions = {shot: state(history[shot])[0] for shot in order}
    # All arms share the conservative fallback, including when the parser fails.
    selected = {shot for shot in order if decisions[shot] != 'distinct'}
    expected = set(case['expected'])
    actual_closure = closure({'S1'} | selected, edges)
    expected_closure = closure({'S1'} | expected, edges)
    return dict(trace=trace, states=decisions, selected=sorted(selected),
                missed=sorted(expected - selected), excess=sorted(selected - expected),
                missed_render=sorted(expected_closure - actual_closure),
                excess_render=sorted(actual_closure - expected_closure),
                errors=sum(bool(bank[shot][index - 1].get('error')) for shot, index in trace))


def score(cases, banks, budgets, policies=POLICIES):
    rows = {}
    for index, (block, bank) in enumerate(sorted(banks.items())):
        order = cases[block].get('recheck_order', sorted(bank, reverse=bool(index % 2)))
        if len(order) != len(bank) or set(order) != set(bank):
            raise ValueError(f'invalid recheck order: {block}')
        rows[block] = {budget: {policy: replay(cases[block], bank, policy, budget, order)
                               for policy in policies}
                       for budget in budgets}
    totals = {}
    for budget in budgets:
        totals[budget] = {}
        for policy in policies:
            runs = [row[budget][policy] for row in rows.values()]
            totals[budget][policy] = dict(
                paired_seeds=len(runs),
                exact=sum(not row['missed'] and not row['excess'] for row in runs),
                missed=sum(len(row['missed']) for row in runs),
                excess=sum(len(row['excess']) for row in runs),
                missed_render=sum(len(row['missed_render']) for row in runs),
                excess_render=sum(len(row['excess_render']) for row in runs),
                unknown=sum(list(row['states'].values()).count('unknown') for row in runs),
                confirmed=sum(list(row['states'].values()).count('confirmed') for row in runs),
                errors=sum(row['errors'] for row in runs))
    return dict(totals=totals, seeds=rows)


def archived(args):
    protocol = load(args.protocol)
    report = load(args.archive)
    cases = {}
    rounds = []
    for repeat in range(protocol['repeats']):
        banks = {}
        for block, plan in sorted(protocol['blocks'].items()):
            record = plan['record']
            cases[block] = dict(record['story'], expected=['S3'])
            bank = {}
            for shot, trials in report['trials'][block].items():
                calls = trials[repeat]['fixed-comprehensive']['calls']
                expected = plan['plans'][shot]['images']
                if [(x['path'], x['sha256']) for x in calls] != [
                    (x['path'], x['sha256']) for x in expected
                ]:
                    raise ValueError(f'archive input mismatch: {block}/{shot}')
                bank[shot] = [dict(votes=observations(row['raw'], protocol['structured_schema'],
                                                     cases[block])) for row in calls]
            banks[block] = bank
        rounds.append(score(cases, banks, (3, 4)))
    return dict(design='retrospective', rounds=rounds)


def request(url, payload=None):
    if payload is None:
        req = Request(url)
    else:
        req = Request(url, json.dumps(payload).encode(), {'Content-Type': 'application/json'})
    with urlopen(req, timeout=180) as response:
        return json.load(response)


def identity(server, model):
    tags = request(server + '/api/tags')['models']
    digests = [row['digest'] for row in tags if row['name'] == model]
    if len(digests) != 1 or not digests[0]:
        raise ValueError(f'Ollama model unavailable: {model}')
    return dict(digest=digests[0], version=request(server + '/api/version')['version'], model=model)


def sheet(clip, output, fractions):
    duration = float(subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', str(clip)], text=True))
    if not 0 < duration < float('inf'):
        raise ValueError(f'invalid clip duration: {clip}')
    left, right = [round(duration * fraction, 6) for fraction in fractions]
    subprocess.run([
        'ffmpeg', '-nostdin', '-v', 'error', '-filter_complex_threads', '1',
        '-ss', str(left), '-i', str(clip), '-ss', str(right), '-i', str(clip),
        '-filter_complex',
        '[0:v]scale=768:432:force_original_aspect_ratio=decrease,pad=768:432:(ow-iw)/2:(oh-ih)/2,setsar=1[a];'
        '[1:v]scale=768:432:force_original_aspect_ratio=decrease,pad=768:432:(ow-iw)/2:(oh-ih)/2,setsar=1[b];'
        '[a][b]hstack=inputs=2', '-frames:v', '1', '-threads', '1', '-update', '1',
        '-y', str(output)], check=True)
    return [left, right]


def eligibility(args, protocol, source_report):
    cases = protocol['cases']
    if not protocol.get('requires_screen'):
        return {block for block in cases if cases[block].get('eligible') is True}
    if not args.screen:
        raise ValueError('complete the frozen source screen before collecting VLM replies')
    rows = {}
    with args.screen.open(newline='', encoding='utf-8-sig') as file:
        reader = csv.DictReader(file)
        if tuple(reader.fieldnames or ()) != SCREEN_FIELDS:
            raise ValueError('use the generated screening template')
        for row in reader:
            block, shot = row['block'], row['shot']
            key = (block, shot)
            count = row['readable_linked_panels'].strip()
            if (block not in cases or source_report[block]['status'] != 'completed'
                    or shot not in ('S1', *cases[block]['candidates'])
                    or key in rows or count not in {'0', '1', '2', '3', '4'}):
                raise ValueError(f'invalid screening row: {block}/{shot}')
            rows[key] = int(count)
    expected = {(block, shot) for block, case in cases.items()
                if source_report[block]['status'] == 'completed'
                for shot in ('S1', *case['candidates'])}
    if set(rows) != expected:
        raise ValueError('incomplete source screen')
    return {block for block, case in cases.items() if source_report[block]['status'] == 'completed'
            and all(rows[block, shot] > 0 for shot in ('S1', *case['candidates']))}


def collect(args):
    protocol = load(args.protocol)
    cases = protocol['cases']
    source_report = load(args.source_run / 'source-report.json')
    if set(source_report) != set(cases):
        raise ValueError('source cohort differs from the protocol')
    included = eligibility(args, protocol, source_report)
    server = args.ollama.rstrip('/')
    model = protocol['model']
    metadata = dict(protocol_sha256=sha(args.protocol), model=identity(server, model),
                    source_report_sha256=sha(args.source_run / 'source-report.json'),
                    screen_sha256=sha(args.screen) if args.screen else None)
    if ('rule_model_digest' in protocol
            and metadata['model']['digest'] != protocol['rule_model_digest']):
        raise ValueError('model digest differs from the locked validation protocol')
    metadata_path = args.output / 'metadata.json'
    if metadata_path.exists() and load(metadata_path) != metadata:
        raise ValueError('protocol, source, or model differs from this run')
    save(metadata_path, metadata)
    schema = protocol['schema']
    results = {}
    for block, case in sorted(cases.items()):
        if source_report[block]['status'] != 'completed':
            continue
        source = args.source_run / block / 'source_media'
        assets = {row['asset']: row for row in load(source / 'revision-1.json')['assets']}
        root = args.output / block
        root.mkdir(exist_ok=True)
        bank = {}
        for shot in case['candidates']:
            asset = assets[shot]
            clip = source / asset['file']
            digest = sha(clip)
            if digest != asset['sha256'] or digest != source_report[block]['screening_inputs'][shot]['clip_sha256']:
                raise ValueError(f'source clip hash mismatch: {block}/{shot}')
            bank[shot] = []
            for index, fractions in enumerate(FRACTIONS, 1):
                result_path = root / f'{shot}-{index}.json'
                image = result_path.with_suffix('.png')
                if result_path.exists():
                    row = load(result_path)
                    if row['clip_sha256'] != digest or row['image_sha256'] != sha(image):
                        raise ValueError(f'cached view mismatch: {result_path}')
                else:
                    seconds = sheet(clip, image, fractions)
                    prompt = case['prompt']
                    payload = dict(model=model, stream=False, format=schema,
                                   options=protocol['options'],
                                   messages=[dict(role='user', content=prompt, images=[
                                       base64.b64encode(image.read_bytes()).decode('ascii')])])
                    start = time.monotonic()
                    row = dict(seconds=seconds, clip_sha256=digest, image_sha256=sha(image),
                               prompt=prompt)
                    try:
                        raw = request(server + '/api/chat', payload)['message']['content']
                        row['raw'] = raw
                        row['votes'] = observations(raw, schema, case)
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        row.update(error=str(exc), votes=['unknown', 'unknown'])
                    row['call_s'] = time.monotonic() - start
                    save(result_path, row)
                bank[shot].append(row)
        if identity(server, model) != metadata['model']:
            raise ValueError('Ollama model changed during the run')
        results[block] = bank
        print(f'{block}: {sum(map(len, bank.values()))} archived calls', flush=True)
    budgets = protocol.get('budgets', BUDGETS)
    policies = (*POLICIES, 'fixed-recheck') if protocol.get('include_fixed_recheck') else POLICIES
    report = score(cases, results, budgets, policies)
    report.update(design='shared-response-bank-replay',
                  actual_bank_calls=sum(len(calls) for bank in results.values() for calls in bank.values()),
                  source_status={block: row['status'] for block, row in source_report.items()},
                  primary_budget=protocol.get('primary_budget', 3),
                  eligibility={block: ('eligible' if block in included else 'excluded')
                               for block in results},
                  bank_errors=sum(bool(row.get('error')) for bank in results.values()
                                  for calls in bank.values() for row in calls))
    # Source eligibility is the pre-existing screen, frozen before this follow-up.
    eligible = {k: v for k, v in results.items() if k in included}
    report['source_eligible'] = score(cases, eligible, budgets, policies)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--archive', type=Path, help='923 fixed-comprehensive aggregate report')
    modes.add_argument('--source-run', type=Path, help='frozen source generation directory')
    parser.add_argument('--protocol', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--screen', type=Path)
    parser.add_argument('--ollama', default='http://127.0.0.1:11434')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = archived(args) if args.archive else collect(args)
    save(args.output / 'report.json', report)
    print(args.output / 'report.json')


if __name__ == '__main__':
    main()
