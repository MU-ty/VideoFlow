"""Export method-blinded Rev.2 and source pairs for completed Round 931 runs."""

import argparse
import csv
from pathlib import Path
from random import SystemRandom
from shutil import copyfile

from active_budget import load, save, sha
from execute_pair import RULES


def export(run, out, key):
    report = load(run / 'report.json')
    if (not report.get('negative_rule_report_sha256') or report['failures']
            or not report['arm_order'] or set(report['blocks']) != set(report['arm_order'])
            or any(set(arms) != set(RULES) for arms in report['blocks'].values())):
        raise ValueError('a complete Round 931 paired run is required')
    if key.exists() or key.resolve().is_relative_to(out.resolve()):
        raise ValueError('use a fresh key path outside the reviewer folder')

    samples = []
    for block, arms in sorted(report['blocks'].items()):
        for arm, summary in sorted(arms.items()):
            media = run / block / arm / 'media'
            source = {row['asset']: row for row in load(media / 'revision-1.json')['assets']}
            final = {row['asset']: row for row in load(media / 'revision-2.json')['assets']}
            expected = {f'S{i}' for i in range(1, 7)}
            if source.keys() != expected or final.keys() != expected:
                raise ValueError(f'incomplete media exports for {block}/{arm}')
            for shot, row in sorted(final.items()):
                paths = []
                for record in (source[shot], row):
                    path = media / record['file']
                    if path.parent != media or sha(path) != record['sha256']:
                        raise ValueError(f'archive mismatch: {block}/{arm}/{shot}')
                    paths.append(path)
                samples.append((block, arm, shot, shot in summary['expected'], *paths))

    SystemRandom().shuffle(samples)
    out.mkdir(parents=True)
    (out / 'source').mkdir()
    (out / 'clips').mkdir()
    mapping = {}
    with (out / 'ratings.csv').open('w', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        writer.writerow(['clip_id', 'source_file', 'edited_file', 'expected_appearance',
                         'strict_recolor', 'identity_preserved', 'temporal_consistency',
                         'unintended_change', 'notes'])
        for index, (block, arm, shot, target, source, edited) in enumerate(samples, 1):
            clip = f'V{index:03}'
            a, b = f'source/{clip}{source.suffix}', f'clips/{clip}{edited.suffix}'
            copyfile(source, out / a)
            copyfile(edited, out / b)
            writer.writerow([clip, a, b, 'scarlet red' if target else 'unchanged',
                             '' if target else 'NA', '', '', '', ''])
            mapping[clip] = dict(block=block, arm=arm, shot=shot, target=target)
    save(key, mapping)
    print(f'Exported {len(samples)} clips to {out}; keep {key} private.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--key', required=True, type=Path)
    args = parser.parse_args()
    export(args.run, args.out, args.key)


if __name__ == '__main__':
    main()
