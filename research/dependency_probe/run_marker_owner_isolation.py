"""Post-hoc Round 941 ownership diagnostic with frozen marker reads."""

import argparse
from collections import defaultdict
import json
from pathlib import Path

import run_marker_holdout as holdout
import run_marker_split as split

pair = split.pair
SCHEMA = split.obj(
    part_visible=split.enum(split.TRISTATE),
    marker_visible=split.enum(split.TRISTATE),
    relation=split.enum(('same-device', 'different-devices', 'unknown')),
)
PROMPT = """This image shows one candidate frame. The yellow rectangle marks
the location of a possible identity decal. Find the requested control and
the decal in that region. For relation, use same-device only if the visible
control and decal are attached to the same physical body. Use different-devices
only if you can identify both items on two visibly separate bodies. If either
item is missing, obscured, or its body cannot be traced, use unknown.
Requested control: {part}.
"""
ARMS = ('archived-split', 'candidate-only-abstain', 'candidate-only-explicit-reject')


def load(path):
    return json.loads(path.read_text())


def decoded_vote(reading, relation, explicit_reject):
    if (relation['part_visible'] != 'yes' or relation['marker_visible'] != 'yes'
            or relation['relation'] == 'unknown'):
        return 'unknown'
    if relation['relation'] == 'different-devices':
        # A fully observed different host is evidence about the candidate itself.
        return 'distinct' if explicit_reject else 'unknown'
    return split.combine(reading, {'anchor': 'yes', 'candidate': 'yes'})


def read_archived(prior, items):
    calls = {}
    for block in sorted({i['block'] for i in items}):
        file = prior / block / 'egdr-split-marker/vlm.jsonl'
        lines = [json.loads(line) for line in file.read_text().splitlines()]
        for row in lines:
            key = (row['block'], row['shot'], row['panel'], row['stage'])
            if key in calls or row['error']:
                raise ValueError(f'invalid archived call: {key}')
            calls[key] = row
    if len(calls) != len(items) * 2:
        raise ValueError('expected two archived stage calls per view')
    for i in items:
        key = (i['block'], i['shot'], i['panel'])
        for tag, field in [('read', 'crop'), ('ownership', 'outlined')]:
            if calls[(*key, tag)]['sha256'] != i[field]['sha256']:
                raise ValueError(f'archived input digest mismatch: {key}/{tag}')
    return calls


def score(rows):
    votes = defaultdict(list)
    for row in rows:
        for arm in ARMS:
            votes[arm, row['block'], row['shot']].append(row[arm])
    blocks = sorted({r['block'] for r in rows})
    results = {}
    for arm in ARMS:
        per_seed = {}
        for block in blocks:
            shot_votes = {shot: votes[arm, block, shot] for shot in pair.CANDIDATES}
            decisions = {shot: pair.aggregate(v) for shot, v in shot_votes.items()}
            chosen = [shot for shot, decision in decisions.items() if decision != 'distinct']
            per_seed[block] = dict(votes=shot_votes, decisions=decisions, chosen=chosen)
        sets = [x['chosen'] for x in per_seed.values()]
        results[arm] = dict(
            exact=sum(x == ['S3'] for x in sets), covers_target=sum('S3' in x for x in sets),
            excess=sum('S2' in x for x in sets), per_seed=per_seed,
        )
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', type=Path, required=True)
    parser.add_argument('--prior', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ollama', default='http://127.0.0.1:11434')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    cases = holdout.cohort()
    prior_protocol = load(args.prior / 'holdout-protocol.json')
    if prior_protocol['cases'] != cases or load(args.prior / 'summary.json')['complete_pairs'] != 4:
        raise ValueError('expected the original complete Round 941 archive')
    args.output.mkdir(parents=True)
    regions, _ = holdout.screened(args.prior / 'locked-screen.csv',
                                  load(args.source_run / 'source-report.json'), cases)
    prepared = args.output / 'inputs'
    prepared.mkdir()
    items = split.prepare(args.source_run, prepared, regions,
                          {block: dict(targets=['S3']) for block in regions})
    prior_inputs = load(args.prior / 'paired_inputs/index.json')
    if json.dumps(items, sort_keys=True) != json.dumps(prior_inputs, sort_keys=True):
        raise ValueError('source sheets and crop settings must match archived inputs byte for byte')
    calls = read_archived(args.prior, items)
    for item in items:
        outlined = prepared / item['outlined']['file']
        candidate = outlined.with_name(outlined.stem + '-candidate.png')
        item['candidate'] = split.transform(outlined, candidate, 'crop=768:432:768:0')
    (args.output / 'inputs.json').write_text(json.dumps(items, indent=2) + '\n')
    if args.prepare_only:
        print(f'Validated {len(items)} frozen inputs.')
        return
    server = args.ollama.rstrip('/')
    expected = prior_protocol['model']
    model = expected['name']
    identity = pair.model_info(server, model)
    if identity != expected:
        raise ValueError('Ollama model identity differs from the archived run')
    rows = []
    with (args.output / 'calls.jsonl').open('x', encoding='utf-8') as log:
        for item in items:
            key = (item['block'], item['shot'], item['panel'])
            reading = calls[(*key, 'read')]['parsed']
            ownership, failed = split.query(
                server, model, prepared, log, item, 'candidate-only', 'ownership',
                item['candidate'], PROMPT.format(part=cases[item['block']]['part'].removeprefix('{{target.color}} ')),
                SCHEMA, pair.OPTIONS['seed'] + 1,
            )
            if failed:
                raise RuntimeError(f'candidate-only call failed: {key}; see calls.jsonl')
            rows.append(dict(
                block=key[0], shot=key[1], panel=key[2],
                **{'archived-split': split.combine(reading, calls[(*key, 'ownership')]['parsed']),
                   'candidate-only-abstain': decoded_vote(reading, ownership, False),
                   'candidate-only-explicit-reject': decoded_vote(reading, ownership, True)},
            ))
    if pair.model_info(server, model) != identity:
        raise RuntimeError('Ollama model identity changed during the diagnostic')
    results = score(rows)
    report = dict(model=identity, diagnostic='post-hoc replay on the screened Round 941 cohort',
                  new_calls=len(rows), frozen_read_calls=len(rows), results=results, view_votes=rows)
    (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    for arm, stats in results.items():
        print(arm, {k: stats[k] for k in ('exact', 'covers_target', 'excess')}, flush=True)


if __name__ == '__main__':
    main()
