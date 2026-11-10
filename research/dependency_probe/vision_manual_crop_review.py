"""Test the crop verifier on fixed manual boxes, bypassing automatic localization."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
from time import monotonic
from zipfile import ZipFile

from vision_counterevidence import HOLDOUT_FIXTURE
from vision_local_recheck import (
    PANEL_HEIGHT, PANEL_WIDTH, REVIEW_PROMPT, REVIEW_SCHEMA, ROLES,
    make_review_image, parse_review,
)
from vision_probe import image_reply


ANNOTATIONS = HOLDOUT_FIXTURE.with_name('egdr_manual_crop_boxes_v1.json')


def prepare_frames(source):
    annotation = json.loads(ANNOTATIONS.read_text())
    if annotation['image_size_px'] != [PANEL_WIDTH, PANEL_HEIGHT]:
        raise ValueError('manual annotations use a different frame size')
    with ZipFile(HOLDOUT_FIXTURE) as bundle:
        holdout = json.loads(bundle.read('protocol.json'))
    cases = {case['id']: case for case in holdout['cases']}
    frames = annotation['frames']
    seen = set()
    for row in frames:
        case = cases[row['id']]
        panel = row['panel']
        key = row['id'], panel
        if key in seen or type(panel) is not int or not 1 <= panel <= case['panels']:
            raise ValueError(f'invalid or duplicate panel {key}')
        seen.add(key)
        if row['source_image_sha256'] != case['image_sha256']:
            raise ValueError(f'annotation does not match holdout sheet: {key}')
        frame_name = f"crops/{row['id']}-p{panel}.png"
        if row['source_frame'] != frame_name:
            raise ValueError(f'annotation references an unexpected frame: {key}')
        data = (source / frame_name).read_bytes()
        if sha256(data).hexdigest() != row['frame_sha256']:
            raise ValueError(f'source frame hash mismatch: {frame_name}')
        boxes = row['review_boxes_px']
        if set(boxes) != set(ROLES):
            raise ValueError(f'missing review boxes: {key}')
        for box in boxes.values():
            x1, y1, x2, y2 = box
            if (any(type(v) is not int for v in box)
                    or not (0 <= x1 < x2 <= PANEL_WIDTH and 0 <= y1 < y2 <= PANEL_HEIGHT)):
                raise ValueError(f'invalid review box: {key}')
    if len(seen) != 24 or len({case for case, _ in seen}) != 6:
        raise ValueError('expected 24 fixed frames from six candidate shots')
    return frames, cases, holdout['observation_rules']


def score(rows, key):
    groups = {}
    for row in rows:
        groups.setdefault(row[key], []).append(row)
    return {
        condition: {
            'total': len(group),
            'expected_confirmed': sum(row['expected'] for row in group),
            'confirmed': sum(row['confirmed'] for row in group),
            'missed_confirmations': sum(row['expected'] and not row['confirmed'] for row in group),
            'false_confirmations': sum(not row['expected'] and row['confirmed'] for row in group),
            'invalid_replies': sum('error' in row for row in group),
        }
        for condition, group in groups.items()
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', type=Path, required=True,
                        help='original egdr-local-recheck run directory with its crops/ frames')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--server', default='http://127.0.0.1:11434')
    parser.add_argument('--model', default='qwen2.5vl:7b-ctx8k')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()

    frames, cases, rules_by_group = prepare_frames(args.source_run)
    args.output.mkdir(parents=True, exist_ok=False)
    images = args.output / 'crops'
    images.mkdir()
    paths = []
    for row in frames:
        frame = args.source_run / row['source_frame']
        image = images / f'{frame.stem}-review.png'
        # Manual control intentionally keeps a marker outside the device box
        # when the marker belongs to a separate object.
        make_review_image(frame, image, row['review_boxes_px'])
        paths.append(image)
    protocol = dict(model=args.model, boxes='manual', annotation_sha256=sha256(ANNOTATIONS.read_bytes()).hexdigest(),
                    review_prompt=REVIEW_PROMPT, review_schema=REVIEW_SCHEMA, frames=frames,
                    gate='confirm a shot only when all its frames confirm')
    (args.output / 'protocol.json').write_text(json.dumps(protocol, indent=2) + '\n')
    if args.prepare_only:
        print(f'Prepared {len(paths)} crop review images')
        return

    results = []
    shots = {}
    with (args.output / 'review.jsonl').open('w') as log:
        for annotation, image in zip(frames, paths):
            case_id = annotation['id']
            rules = rules_by_group[cases[case_id]['group']]
            print(f"{case_id} panel {annotation['panel']}", flush=True)
            start = monotonic()
            raw = image_reply(args.server, args.model, image, REVIEW_PROMPT.format(**rules), REVIEW_SCHEMA)
            row = dict(id=case_id, panel=annotation['panel'], condition=annotation['condition'],
                       frame_sha256=annotation['frame_sha256'],
                       review_image_sha256=sha256(image.read_bytes()).hexdigest(),
                       expected=annotation['expected_conflict_confirmation'],
                       confirmed=False, review_raw=raw, seconds=monotonic() - start)
            try:
                observed = parse_review(raw)
                row['observations'] = observed
                row['confirmed'] = (observed['marker_shape'] == rules['conflicting_shape']
                                    and observed['part_visibility'] == 'visible'
                                    and observed['same_housing'] == 'yes')
            except ValueError as error:
                row['error'] = str(error)
            log.write(json.dumps(row) + '\n')
            log.flush()
            results.append(row)
            shots.setdefault(case_id, []).append(row)

    shot_results = [{
        'id': case_id, 'condition': group[0]['condition'],
        'expected': all(row['expected'] for row in group),
        'confirmed': all(row['confirmed'] for row in group),
        'confirmed_panels': [row['panel'] for row in group if row['confirmed']],
        **({'error': 'invalid frame reply'} if any('error' in row for row in group) else {}),
    } for case_id, group in shots.items()]
    report = dict(model_calls=len(results), frames=score(results, 'condition'),
                  shots=score(shot_results, 'condition'), shot_results=shot_results)
    text = json.dumps(report, indent=2)
    (args.output / 'report.json').write_text(text + '\n')
    print(text)


if __name__ == '__main__':
    main()
