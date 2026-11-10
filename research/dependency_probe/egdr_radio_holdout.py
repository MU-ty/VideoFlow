"""Prepare source annotations and a method-blinded review for the radio holdout."""

import argparse
import csv
import json
from pathlib import Path
import random
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from artifacts import checksum, write_json

STORY = 'cross-radio'
SEED = 89101
TARGETS = ('S1', 'S3')
SHOTS = ('S1', 'S2', 'S3', 'S4')
POLICIES = ('worklist', 'oracle_egdr', 'online_egdr', 'full')
PROTOCOL = Path(__file__).with_name('EGDR_RADIO_HOLDOUT.md')
FIELDS = ('clip', 'edit', 'unintended', 'timestamps_and_notes')


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def verified_media(directory, revision):
    manifest = load(directory / f'revision-{revision}.json')
    if manifest['revision'] != revision:
        raise ValueError(f'{directory}: wrong manifest revision')
    assets = {row['asset']: row for row in manifest['assets']}
    if len(manifest['assets']) != len(SHOTS) or set(assets) != set(SHOTS):
        raise ValueError(f'{directory}: expected exactly four shots')
    files = {}
    for shot, row in assets.items():
        path = directory / row['file']
        if not path.resolve().is_relative_to(directory.resolve()) or checksum(path) != row['sha256']:
            raise ValueError(f'{shot}: archive failed verification')
        files[shot] = path
    return manifest, assets, files


def check_protocol(root):
    protocol = load(root / 'protocol.json')
    if protocol['story'] != STORY or protocol['seed'] != SEED:
        raise ValueError('expected the preregistered radio story and seed')
    if protocol['holdout_protocol_sha256'] != checksum(PROTOCOL):
        raise ValueError('the holdout protocol changed after this run was recorded')
    return protocol


def ffmpeg(*args):
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-n', *map(str, args)],
                   check=True, timeout=90)


def frames(source_run, output):
    check_protocol(source_run)
    _, _, files = verified_media(source_run / 'source', 1)
    output.mkdir(parents=True)
    for shot, clip in files.items():
        ffmpeg('-i', clip, '-map', '0:v:0', '-frames:v', 1, '-threads', 1, output / f'{shot}.png')


def bind(source_run, annotation, output):
    check_protocol(source_run)
    _, assets, _ = verified_media(source_run / 'source', 1)
    row = load(annotation)
    if row['source_eligible'] is not True or not row['reviewer'].strip() or not row['notes'].strip():
        raise ValueError('record the source eligibility decision and reviewer notes before binding')
    outlines = row['frame0_handle_polygons']
    if set(outlines) != set(TARGETS):
        raise ValueError('provide first-frame handle polygons for S1 and S3 only')
    regions = {}
    hashes = {shot: entry['sha256'] for shot, entry in assets.items()}
    for shot in TARGETS:
        polygon = outlines[shot]
        if (len(polygon) < 3 or any(len(point) != 2 or any(type(c) is not int for c in point)
                                   or not (0 <= point[0] < 864 and 0 <= point[1] < 480)
                                   for point in polygon)):
            raise ValueError(f'{shot}: expected a polygon of at least three 864x480 pixel points')
        regions[shot] = [hashes[shot], polygon]
    output.mkdir(parents=True)
    write_json(output / 'source_annotation.json', row)
    write_json(output / 'oracle_labels.json', dict(target_shots=list(TARGETS), source_sha256=hashes))
    write_json(output / 'reference_regions.json', regions)


def blind(run, output, key):
    protocol = check_protocol(run)
    report = load(run / 'report.json')
    before, _, source_files = verified_media(run / 'source', 1)
    rows = {row['policy']: row for row in report['rows']}
    if (tuple(protocol['policies']) != POLICIES or set(rows) != set(POLICIES)
            or report['source_timeline_hash'] != before['timeline_hash']):
        raise ValueError('expected a completed preregistered four-arm run')
    if key.resolve().is_relative_to(output.resolve()) or key.exists():
        raise ValueError('use a new private key path outside the reviewer directory')
    items = []
    for policy in POLICIES:
        _, _, files = verified_media(run / policy / 'media', 3)
        items.extend((policy, shot, files[shot]) for shot in SHOTS)
    random.SystemRandom().shuffle(items)
    output.mkdir(parents=True)
    source_dir = output / 'source'
    source_dir.mkdir()
    for shot, path in source_files.items():
        remux(path, source_dir / f'{shot}.mp4')
    mapping = {}
    for index, (policy, shot, path) in enumerate(items, 1):
        name = f'R{index:02d}'
        remux(path, output / f'{name}-{shot}.mp4')
        mapping[name] = dict(policy=policy, shot=shot, sha256=checksum(path))
    write_json(key, mapping)
    with (output / 'ratings.csv').open('x', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(FIELDS)
        writer.writerows((name, '', '', '') for name in mapping)
    (output / 'REVIEW.md').write_text(
        'Score all clips in numeric order against the same-shot source. '
        'Keep your ratings independent.\n\n' + PROTOCOL.read_text().split('## Reviewer rubric\n', 1)[1]
        .split('\n## ', 1)[0], encoding='utf-8',
    )


def remux(source, output):
    ffmpeg('-i', source, '-map', '0:v:0', '-map_metadata', '-1', '-map_metadata:s:v', '-1',
           '-map_chapters', '-1', '-an', '-c:v', 'copy', output)


def scores(run, key, ratings):
    check_protocol(run)
    mapping = load(key)
    report = load(run / 'report.json')
    expected = {(policy, shot) for policy in POLICIES for shot in SHOTS}
    if len(mapping) != len(expected) or {
        (entry['policy'], entry['shot']) for entry in mapping.values()
    } != expected:
        raise ValueError('invalid private review key')
    result = {}
    for row in report['rows']:
        policy = row['policy']
        _, assets, _ = verified_media(run / policy / 'media', 3)
        if any(entry['sha256'] != assets[entry['shot']]['sha256']
               for entry in mapping.values() if entry['policy'] == policy):
            raise ValueError('the key does not match this run')
        selected = set(row['final_selection'])
        result[policy] = dict(
            missed=sorted(set(TARGETS) - selected), excess=sorted(selected - set(TARGETS)),
            submission_intents=row['submission_intents'], reused=row['reused'],
            s3_repair_decision=next((repair['decision'] for repair in row['repairs']
                                     if repair['child'] == 'S3'), None),
            publication_seconds_after_edit=row['publication_seconds_after_edit'], reviews=[],
        )
    for path in ratings:
        with path.open(newline='', encoding='utf-8-sig') as stream:
            review = list(csv.DictReader(stream))
        if len(review) != len(mapping) or {row['clip'] for row in review} != set(mapping):
            raise ValueError(f'{path}: expected one rating per clip')
        by_policy = {policy: {} for policy in POLICIES}
        for row in review:
            entry = mapping[row['clip']]
            shot = entry['shot']
            edit = row['edit']
            allowed = ('pass', 'fail', 'uncertain') if shot in TARGETS else ('na',)
            if edit not in allowed or row['unintended'] not in ('yes', 'no', 'uncertain'):
                raise ValueError(f"{path}: invalid rating for {row['clip']}")
            if ((edit in ('fail', 'uncertain') or row['unintended'] != 'no')
                    and not row['timestamps_and_notes'].strip()):
                raise ValueError(f"{path}: missing evidence notes for {row['clip']}")
            by_policy[entry['policy']][shot] = row
        for policy, shots in by_policy.items():
            result[policy]['reviews'].append(dict(
                reviewer_file=path.name,
                passed_target=[shot for shot in TARGETS if shots[shot]['edit'] == 'pass'],
                uncertain_target=[shot for shot in TARGETS if shots[shot]['edit'] == 'uncertain'],
                changed_non_target=[shot for shot in SHOTS if shot not in TARGETS and shots[shot]['unintended'] == 'yes'],
                uncertain_non_target=[shot for shot in SHOTS if shot not in TARGETS and shots[shot]['unintended'] == 'uncertain'],
                changed_target=[shot for shot in TARGETS if shots[shot]['unintended'] == 'yes'],
                shots=shots,
            ))
    worklist = result['worklist']['reviews']
    egdr = result['online_egdr']['reviews']
    paired = [
        dict(reviewer_file=left['reviewer_file'],
             changed_non_target_delta=len(right['changed_non_target']) - len(left['changed_non_target']),
             s3_edit_worklist=left['shots']['S3']['edit'], s3_edit_egdr=right['shots']['S3']['edit'])
        for left, right in zip(worklist, egdr)
    ]
    print(json.dumps(dict(policies=result, paired_vlm_minus_worklist=paired),
                     indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command, names in (
        (frames, ('source_run', 'output')),
        (bind, ('source_run', 'annotation', 'output')),
        (blind, ('run', 'output', 'key')),
        (scores, ('run', 'key', 'ratings')),
    ):
        sub = commands.add_parser(command.__name__)
        for name in names:
            sub.add_argument(name, type=Path, nargs='+' if name == 'ratings' else None)
        sub.set_defaults(callback=command, names=names)
    args = parser.parse_args()
    args.callback(*(getattr(args, name) for name in args.names))


if __name__ == '__main__':
    main()
