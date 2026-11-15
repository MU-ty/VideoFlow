"""Export the first published videos with randomized labels and a private key."""
import argparse
import csv
from hashlib import sha256
import json
from pathlib import Path
import random
import subprocess
from zipfile import ZIP_STORED, ZipFile


def load(path):
    return json.loads(path.read_text())


def export_clip(directory, row, output):
    source = directory / row['file']
    if sha256(source.read_bytes()).hexdigest() != row['sha256']:
        raise ValueError(f'manifest video hash mismatch: {source}')
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(source),
                    '-map', '0:v:0', '-c:v', 'copy', '-map_metadata', '-1', '-map_chapters', '-1',
                    str(output)], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    results = load(args.run / 'report.json')
    if not results or any(row['status'] != 'completed' for row in results.values()):
        raise ValueError('all scheduled blocks must finish before blind export')
    root = args.out
    root.mkdir(parents=True)
    review = root / 'reviewer'
    review.mkdir()
    items = []
    for block, report in results.items():
        directory = args.run / block
        case = load(directory / 'story.json')
        original = directory / 'source_media'
        base = review / 'source' / block
        base.mkdir(parents=True)
        source = load(original / 'revision-1.json')['assets']
        for row in source:
            export_clip(original, row, base / (row['asset'] + '.mp4'))
        for result in report['policies']:
            policy = result['policy']
            published = directory / policy / 'media'
            for row in load(published / 'revision-2.json')['assets']:
                items.append((block, policy, published, row, case))
    random.Random(20261116).shuffle(items)
    key = {}
    media = review / 'outputs'
    media.mkdir()
    with (review / 'ratings.csv').open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['clip_id', 'source_file', 'object_or_scene', 'part', 'expected_color',
                         'identity_preserved', 'strict_recolor', 'temporal_consistency',
                         'unintended_change', 'notes'])
        for i, (block, policy, published, row, case) in enumerate(items, 1):
            shot = row['asset']
            clip = f'V{i:03d}'
            export_clip(published, row, media / (clip + '.mp4'))
            target = shot in ('S1', 'S3', 'S4', 'S6')
            part = case['part'].replace('{{target.color}} ', '') if shot != 'S5' else ''
            obj = (case['host'] + ' (' + case['target_marker'] + ')') if target else (
                (case['host'] + ' (' + case['distractor_marker'] + ')') if shot == 'S2'
                else case['independent'])
            expected = 'scarlet red' if target else case['initial'] if shot == 'S2' else 'unchanged'
            writer.writerow([clip, f'source/{block}/{shot}.mp4', obj, part, expected, '', '', '', '', ''])
            key[clip] = dict(block=block, policy=policy, shot=shot, target=target,
                             manifest_sha256=row['sha256'])
    with (review / 'source_screen.csv').open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['block', 'shot', 'required_marker', 'marker_visible', 'part_visible',
                         'same_instance', 'notes'])
        for block in sorted({item[0] for item in items}):
            case = load(args.run / block / 'story.json')
            for shot in ('S1', 'S2', 'S3'):
                marker = case['distractor_marker'] if shot == 'S2' else case['target_marker']
                writer.writerow([block, shot, marker, '', '', '', ''])
    (root / 'KEEP_PRIVATE_key.json').write_text(json.dumps(key, indent=2) + '\n')
    (review / 'REVIEW.txt').write_text(
        'Score source_screen.csv before opening outputs. Use yes/no/unclear.\n'
        'For each output, compare against its source clip and review the entire video.\n'
        'identity_preserved: subject identity and sticker remain recognizable.\n'
        'strict_recolor: target part remains scarlet red whenever visible. Use NA for S2/S5.\n'
        'temporal_consistency: no visually material flicker, identity drift or color reversion.\n'
        'unintended_change: yes if other attributes, object identity, or non-target appearance changed.\n'
        'Use unclear if occlusion or ambiguity prevents a judgment and explain in notes.\n'
        'Score independently and return the original CSV files.\n', encoding='utf-8')
    with ZipFile(root / 'send_to_reviewers.zip', 'w', ZIP_STORED) as z:
        for file in sorted(review.rglob('*')):
            if file.is_file():
                z.write(file, file.relative_to(review))


if __name__ == '__main__':
    main()
