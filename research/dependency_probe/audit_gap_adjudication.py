"""Compare three adjudicators on a completed replay_gaps.py archive, without VLM calls."""

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

RULES = ('original', 'conflict-abstain', 'majority-counterevidence')


def linked(panel, marker):
    color, shape = marker.split()
    return (panel['host_visible'], panel['part_visible'], panel['same_instance'],
            panel['shape'], panel['color']) == ('yes', 'yes', 'yes', shape, color)


def adjudicate(panels, case):
    target = [p for p in panels if linked(p, case['target_marker'])]
    opposing = [p for p in panels if linked(p, case['distractor_marker'])]
    complete = len(panels) == 4 and all(p['complete'] is True for p in panels)
    original = 'unknown'
    if target:
        original = 'confirmed'
    elif complete and opposing and all(
            p in opposing or p['host_visible'] == 'no' for p in panels):
        original = 'rejected'

    abstain = 'unknown' if target and opposing else original
    majority = abstain
    if complete and len(target) == 1 and len(opposing) == 3:
        majority = 'rejected'
    return dict(zip(RULES, (original, abstain, majority)))


def observed_panels(calls, schema):
    panels = []
    fields = schema['properties']['panels']['items']['properties']
    for call in calls:
        if 'error' in call:
            continue
        rows = json.loads(call['raw'])['panels']
        if len(rows) != 2 or {row['panel'] for row in rows} != {1, 2}:
            raise ValueError('expected two distinct panels per successful call')
        for row in rows:
            if (set(row) != set(fields) or type(row['complete']) is not bool
                    or type(row['panel']) is not int
                    or any('enum' in spec and row[key] not in spec['enum']
                           for key, spec in fields.items())):
                raise ValueError('invalid panel fields')
        panels.extend(rows)
    return panels


def audit(path):
    with ZipFile(path) as archive:
        (protocol_path,) = [name for name in archive.namelist()
                            if name.rsplit('/', 1)[-1] == 'protocol.json']
        base = protocol_path.removesuffix('protocol.json')
        protocol = json.loads(archive.read(protocol_path))
        report = json.loads(archive.read(base + 'report.json'))
        if report['status'] != 'completed' or set(report['trials']) != set(protocol['blocks']):
            raise ValueError('expected a complete gap replay')
        results = {}
        for seed, block in protocol['blocks'].items():
            case = block['record']['story']
            shots = block['record']['shots']
            expected = set(report['summary'][seed]['initial']['expected'])
            trials = report['trials'][seed]
            if set(trials) != set(shots) or any(
                    len(rounds) != protocol['repeats'] for rounds in trials.values()):
                raise ValueError(f'incomplete replay: {seed}')
            rounds = results[seed] = []
            for index in range(protocol['repeats']):
                methods = {}
                for arm in protocol['arms']:
                    verdicts = {}
                    for shot in shots:
                        plan = block['plans'][shot]['images']
                        trial = trials[shot][index][arm]
                        if len(trial['calls']) != len(plan) or len(plan) != 2:
                            raise ValueError(f'unexpected probe budget: {seed}/{shot}')
                        for call, image in zip(trial['calls'], plan):
                            image_path = base + block['path'] + '/' + image['path']
                            sha = hashlib.sha256(archive.read(image_path)).hexdigest()
                            if call['sha256'] != image['sha256'] or image['sha256'] != sha:
                                raise ValueError(f'changed input: {seed}/{shot}')
                        panels = observed_panels(trial['calls'], protocol['structured_schema'])
                        verdicts[shot] = adjudicate(panels, case)
                        if verdicts[shot]['original'] != trial['verdict']:
                            raise ValueError(f'original verdict mismatch: {seed}/{shot}')
                    rules = methods[arm] = {}
                    for rule in RULES:
                        decisions = {shot: {'confirmed': 'match', 'rejected': 'distinct'}.get(
                            verdicts[shot][rule], item['decision']) for shot, item in shots.items()}
                        selected = {shot for shot, state in decisions.items() if state != 'distinct'}
                        rules[rule] = dict(
                            selected=sorted(selected), missed=sorted(expected - selected),
                            excess=sorted(selected - expected),
                            verdicts={shot: value[rule] for shot, value in verdicts.items()},
                        )
                rounds.append(methods)

    aggregate = {}
    for arm in protocol['arms']:
        aggregate[arm] = {
            rule: [dict(
                seeds=len(results),
                seeds_with_miss=sum(bool(rows[index][arm][rule]['missed']) for rows in results.values()),
                seeds_with_excess=sum(bool(rows[index][arm][rule]['excess']) for rows in results.values()),
            ) for index in range(protocol['repeats'])]
            for rule in RULES
        }
    return dict(archive=path.name, aggregate_by_round=aggregate, seeds=results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = audit(args.archive)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result['aggregate_by_round'], indent=2))


if __name__ == '__main__':
    main()
