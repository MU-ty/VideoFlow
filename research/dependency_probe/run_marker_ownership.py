"""Counterfactual decal ownership benchmark on 12 synthetic device layouts."""

import argparse
import json
from pathlib import Path
import random

from PIL import Image, ImageDraw

import run_anchor_pair as pair
import run_marker_region as region
import run_marker_split as split

BICUBIC = getattr(Image, 'Resampling', Image).BICUBIC

SEEDS = tuple(range(9401, 9413))
PART = 'red rotary knob'
LABELS = {'same_device': 'yes', 'other_device': 'no'}
COLORS = {'blue': '#4477df', 'green': '#239845', 'purple': '#9a50c7'}
SHAPES = ('circle', 'triangle', 'diamond')
OPTIONS = [pair.OPTIONS['seed'], pair.OPTIONS['seed'] + 1]


def sticker(draw, x, y, shape, color):
    if shape == 'circle':
        draw.ellipse((x - 18, y - 18, x + 18, y + 18), fill=color)
    else:
        points = [(x, y - 20), (x + 20, y + 16), (x - 20, y + 16)]
        if shape == 'diamond':
            points = [(x, y - 22), (x + 20, y), (x, y + 22), (x - 20, y)]
        draw.polygon(points, fill=color)


def frame(seed, mirrored, sticker_on_target, shape, color):
    rng = random.Random(seed)
    canvas = Image.new('RGB', (768, 432), '#303741')
    draw = ImageDraw.Draw(canvas)
    target_slot = (seed + mirrored) % 2
    y = 208 + rng.randint(-14, 14)
    for slot in (0, 1):
        x = 197 + 374 * slot + rng.randint(-12, 12)
        box = (x - 148, y - 120, x + 148, y + 120)
        draw.rounded_rectangle((box[0] + 6, box[1] + 8, box[2] + 6, box[3] + 8),
                               radius=22, fill='#222830')
        draw.rounded_rectangle(box, radius=22, fill='#c2c6c8',
                               outline='#e1e4e7', width=3)
        draw.rounded_rectangle((x - 117, y - 94, x + 117, y + 90),
                               radius=10, fill='#b0b7bd')
        if slot == target_slot:
            cx, cy = x - 61, y - 31
            draw.ellipse((cx - 35, cy - 35, cx + 35, cy + 35), fill='#444851')
            draw.ellipse((cx - 29, cy - 29, cx + 29, cy + 29), fill='#b52924')
            draw.line((cx, cy - 24, cx, cy - 13), fill='#fff2ec', width=4)
        else:
            draw.rounded_rectangle((x - 100, y - 42, x - 24, y - 20),
                                   radius=8, fill='#282f36')
            draw.rectangle((x - 67, y - 47, x - 52, y - 15), fill='#5b6268')
        if (slot == target_slot) == sticker_on_target:
            center = (x + 50, y + 17)
            sticker(draw, *center, shape, color)
    return canvas, center


def save(image, output, name):
    path = output / f'{name}.png'
    image.save(path)
    return {'file': path.name, 'sha256': pair.sha(path)}


def make_pair(left, right, left_center, right_center):
    sheet = Image.new('RGB', (1536, 432))
    crop = Image.new('RGB', (512, 224))
    for side, (frame, (x, y)) in enumerate(((left, left_center), (right, right_center))):
        crop_box = (x - 64, y - 56, x + 64, y + 56)
        excerpt = frame.crop(crop_box).resize((256, 224), BICUBIC)
        crop.paste(excerpt, (256 * side, 0))
        outlined = frame.copy()
        ImageDraw.Draw(outlined).rectangle(crop_box, outline='yellow', width=2)
        sheet.paste(outlined, (768 * side, 0))
    return crop, sheet


def prepare(output):
    rows = []
    for index, seed in enumerate(SEEDS):
        shape = SHAPES[index % len(SHAPES)]
        color = tuple(COLORS)[index // len(SHAPES) % len(COLORS)]
        anchor, anchor_center = frame(seed, 0, True, shape, COLORS[color])
        matched = []
        for label in LABELS:
            candidate, center = frame(seed, 1, label == 'same_device', shape, COLORS[color])
            crop, full = make_pair(anchor, candidate, anchor_center, center)
            block = f'layout-{seed}'
            prefix = f'{block}-{label}'
            matched.append(dict(block=block, shot=label, panel=1,
                                expected=dict(anchor='yes', candidate=LABELS[label]),
                                marker=dict(shape=shape, color=color),
                                centers=[anchor_center, center],
                                crop=save(crop, output, prefix+'-crop'),
                                full=save(full, output, prefix+'-full')))
        if matched[0]['crop']['sha256'] != matched[1]['crop']['sha256']:
            raise RuntimeError(f'paired crops should be pixel-identical: {seed}')
        rows.extend(matched if index % 2 == 0 else reversed(matched))
    return rows


def run(server, model, output, items):
    identity = pair.model_info(server, model)
    if identity['digest'].removeprefix('sha256:') != region.REFERENCE_DIGEST:
        raise RuntimeError('expected the frozen Round 925 model digest')
    results = []
    errors = 0
    with (output / 'calls.jsonl').open('x', encoding='utf-8') as log:
        for item in items:
            reading, read_error = split.query(
                server, model, output, log, item, 'split-marker', 'read', item['crop'],
                split.READ, split.READ_SCHEMA, OPTIONS[0])
            owner, owner_error = split.query(
                server, model, output, log, item, 'split-marker', 'ownership', item['full'],
                split.OWNERSHIP.format(part=PART), split.OWNERSHIP_SCHEMA, OPTIONS[1])
            errors += read_error + owner_error
            results.append(dict(block=item['block'], condition=item['shot'],
                                expected=item['expected'], reading=reading,
                                ownership=owner, vote=split.combine(reading, owner),
                                reading_only_vote=split.combine(
                                    reading, {'anchor': 'yes', 'candidate': 'yes'})))
            print(item['block'], item['shot'], owner, flush=True)
    if pair.model_info(server, model) != identity:
        raise RuntimeError('model identity changed during the run')
    metrics = {}
    for label, expected in LABELS.items():
        rows = [r for r in results if r['condition'] == label]
        candidates = [(r['ownership'] or {}).get('candidate', 'call_error') for r in rows]
        metrics[label] = dict(
            count=len(rows), expected=expected,
            candidate={v: candidates.count(v) for v in (*split.TRISTATE, 'call_error')},
            correct_pair=sum(r['ownership'] == r['expected'] for r in rows),
            reading_only_match=sum(r['reading_only_vote'] == 'match' for r in rows),
            fused_match=sum(r['vote'] == 'match' for r in rows))
    outcomes = {(r['block'], r['condition']): r['ownership'] == r['expected']
                for r in results}
    paired_correct = sum(all(outcomes[f'layout-{seed}', label] for label in LABELS)
                         for seed in SEEDS)
    return dict(model=identity, layouts=len(SEEDS), call_errors=errors, metrics=metrics,
                paired_correct=paired_correct, results=results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--server', default='http://127.0.0.1:11434')
    parser.add_argument('--model', default='qwen2.5vl:7b-ctx8k')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    items = prepare(args.output)
    (args.output / 'inputs.json').write_text(json.dumps(items, indent=2) + '\n')
    print(f'Prepared {len(items)} paired fixtures ({2 * len(items)} model calls)', flush=True)
    if not args.prepare_only:
        result = run(args.server.rstrip('/'), args.model, args.output, items)
        (args.output / 'report.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result['metrics'], indent=2))
        if result['call_errors']:
            raise SystemExit('Call errors recorded; inspect calls.jsonl before scoring.')


if __name__ == '__main__':
    main()
