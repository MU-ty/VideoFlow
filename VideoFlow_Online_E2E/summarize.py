"""Join first-publication trajectories with a method-blinded rating record."""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path

FIELDS = ('strict_recolor', 'identity_preserved', 'temporal_consistency', 'unintended_change')
JUDGMENTS = {'yes', 'no', 'unclear'}


def load_ratings(blind_key, rating_file):
    key = json.loads(blind_key.read_text())
    with rating_file.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != len(key) or {row['clip_id'] for row in rows} != set(key):
        raise ValueError('rating rows must cover all anonymous clips exactly once')
    ratings = {}
    for row in rows:
        clip = row['clip_id']
        item = key[clip]
        block, policy, shot = (item[field] for field in ('block', 'policy', 'shot'))
        if row['source_file'] != f'source/{block}/{shot}.mp4':
            raise ValueError(f'source mapping changed for {clip}')
        values = {field: (row.get(field) or '').strip().lower() for field in FIELDS}
        recolor = JUDGMENTS if item['target'] else {'na'}
        if (values['strict_recolor'] not in recolor
                or any(values[field] not in JUDGMENTS for field in FIELDS[1:])):
            raise ValueError(f'missing or invalid judgment for {clip}')
        group = ratings.setdefault((block, policy), {})
        if shot in group:
            raise ValueError('duplicate shot in private mapping')
        group[shot] = (item['target'], values)
    return ratings


def summarize(report, ratings=None):
    result = {}
    seen = set()
    for block, run in report.items():
        policies = {}
        result[block] = dict(run, policies=policies)
        for arm in run['policies']:
            policy = arm['policy']
            metrics = dict(arm, recolor=None)
            policies[policy] = metrics
            if ratings is not None:
                seen.add((block, policy))
                clips = ratings[(block, policy)]
                if set(clips) != {'S1', 'S2', 'S3', 'S4', 'S5', 'S6'}:
                    raise ValueError(f'{block}/{policy}: ratings must cover all six shots')
                if {shot for shot, (target, _) in clips.items() if target} != set(arm['expected']):
                    raise ValueError(f'{block}/{policy}: target mismatch')
                scores = {shot: row for shot, (_, row) in clips.items()}
                target_scores = [scores[shot]['strict_recolor'] for shot in arm['expected']]
                metrics['recolor'] = dict(target_total=len(target_scores),
                    target_counts=dict(Counter(target_scores)), by_shot=scores)
    if ratings is not None and seen != set(ratings):
        raise ValueError('rating groups must match the run report')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--key', type=Path)
    parser.add_argument('--ratings', type=Path)
    args = parser.parse_args()
    if (args.key is None) != (args.ratings is None):
        parser.error('--key and --ratings are required together')
    report = json.loads(args.report.read_text())
    ratings = load_ratings(args.key, args.ratings) if args.key else None
    print(json.dumps(summarize(report, ratings), indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
