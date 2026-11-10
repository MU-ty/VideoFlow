"""Evaluate grid-based localization and a spatial veto on fixed candidate shots."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
from time import monotonic

from PIL import Image, ImageDraw

from vision_local_recheck import (
    PANEL_HEIGHT, PANEL_WIDTH, REVIEW_PROMPT, REVIEW_SCHEMA, ROLES,
    make_review_image, parse_review,
)
from vision_manual_crop_review import prepare_frames, score
from vision_probe import image_reply


CELL = 30
COLS = tuple('ABCDEFGHIJKLMNOP')
ROWS = tuple('123456789')
GRID_FIELDS = {'left_col': COLS, 'right_col': COLS, 'top_row': ROWS, 'bottom_row': ROWS}
REGION = {
    'type': 'object', 'required': list(GRID_FIELDS), 'additionalProperties': False,
    'properties': {field: {'enum': values} for field, values in GRID_FIELDS.items()},
}
GRID_SCHEMA = {
    'type': 'object', 'required': list(ROLES), 'additionalProperties': False,
    'properties': {role: {'anyOf': [REGION, {'type': 'null'}]} for role in ROLES},
}
GRID_PROMPT = (
    'Localize three regions independently on the overlaid grid. The columns are A to P '
    'from left to right and the rows are 1 to 9 from top to bottom. For each region, give '
    'the inclusive cell span: left_col, right_col, top_row, bottom_row. For instance a '
    'region occupying just cell B3 uses B for both columns and 3 for both rows. '
    'housing is the physical device described as {housing}, excluding other objects. '
    'marker is the actual geometric identity mark, including if it appears on another object. '
    'part is the actual visible {part}; a cover or its expected location does not count. '
    'Use null for a region you cannot see or cannot localize. Do not move a region '
    'into the housing just because it is nearby.'
)


def make_grid(frame, output):
    margin = 24
    with Image.open(frame) as source:
        if source.size != (PANEL_WIDTH, PANEL_HEIGHT):
            raise ValueError(f'unexpected frame dimensions: {frame}')
        view = Image.new('RGB', (PANEL_WIDTH + margin, PANEL_HEIGHT + margin), '#15212b')
        view.paste(source.convert('RGB'), (margin, margin))
    draw = ImageDraw.Draw(view, 'RGBA')
    for i, col in enumerate(COLS):
        x = margin + i * CELL
        draw.line((x, margin, x, view.height - 1), fill=(230, 245, 255, 105))
        draw.text((x + CELL // 2, margin // 2), col, anchor='mm', fill='white')
    for i, row in enumerate(ROWS):
        y = margin + i * CELL
        draw.line((margin, y, view.width - 1, y), fill=(230, 245, 255, 105))
        draw.text((margin // 2, y + CELL // 2), row, anchor='mm', fill='white')
    view.save(output)


def grid_boxes(raw):
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != set(ROLES):
        raise ValueError('expected housing, marker, and part')
    boxes = {}
    for role, region in data.items():
        if region is None:
            boxes[role] = None
            continue
        if (not isinstance(region, dict) or set(region) != set(GRID_FIELDS)
                or any(region[field] not in values for field, values in GRID_FIELDS.items())):
            raise ValueError(f'invalid grid region for {role}')
        left, right, top, bottom = (values.index(region[field])
                                    for field, values in GRID_FIELDS.items())
        if left > right or top > bottom:
            raise ValueError(f'reversed grid region for {role}')
        boxes[role] = (left * CELL, top * CELL, (right + 1) * CELL, (bottom + 1) * CELL)
    return boxes


def spatial_status(boxes):
    if any(boxes[role] is None for role in ROLES):
        return 'missing_region'
    x1, y1, x2, y2 = boxes['housing']
    for role in ('marker', 'part'):
        left, top, right, bottom = boxes[role]
        if not (x1 <= left < right <= x2 and y1 <= top < bottom <= y2):
            return f'{role}_outside_housing'
    # Adjacent objects can occupy some of the same grid cells.
    if boxes['marker'] == boxes['part']:
        return 'indistinguishable_marker_and_part'
    return 'pass'


def box_iou(predicted, reference):
    if predicted is None:
        return 0.0
    x1, y1, x2, y2 = predicted
    a1, b1, a2, b2 = reference
    overlap = max(0, min(x2, a2) - max(x1, a1)) * max(0, min(y2, b2) - max(y1, b1))
    return overlap / ((x2 - x1) * (y2 - y1) + (a2 - a1) * (b2 - b1) - overlap)


def localize_and_review(frame, grid, review, rules, args):
    row = dict(confirmed=False, model_calls=1, boxes_px={})
    row['localization_raw'] = image_reply(args.server, args.model, grid,
                                          GRID_PROMPT.format(**rules), GRID_SCHEMA)
    try:
        boxes = grid_boxes(row['localization_raw'])
    except ValueError as error:
        row.update(stage='invalid_localization', error=str(error))
        return row
    row['boxes_px'] = boxes
    row['stage'] = spatial_status(boxes)
    if row['stage'] != 'pass':
        return row
    make_review_image(frame, review, boxes)
    row['review_image_sha256'] = sha256(review.read_bytes()).hexdigest()
    row['model_calls'] += 1
    row['review_raw'] = image_reply(args.server, args.model, review,
                                    REVIEW_PROMPT.format(**rules), REVIEW_SCHEMA)
    try:
        observed = parse_review(row['review_raw'])
    except ValueError as error:
        row.update(stage='invalid_review', error=str(error))
        return row
    row['observations'] = observed
    row['confirmed'] = (observed['marker_shape'] == rules['conflicting_shape']
                        and observed['part_visibility'] == 'visible'
                        and observed['same_housing'] == 'yes')
    row['stage'] = 'confirmed' if row['confirmed'] else 'review_did_not_confirm'
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--server', default='http://127.0.0.1:11434')
    parser.add_argument('--model', default='qwen2.5vl:7b-ctx8k')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    frames, cases, rules_by_group = prepare_frames(args.source_run)
    args.output.mkdir(parents=True, exist_ok=False)
    grids, crops = (args.output / name for name in ('grids', 'crops'))
    grids.mkdir()
    crops.mkdir()
    protocol = dict(model=args.model, cell_size_px=CELL, columns=COLS, rows=ROWS,
                    localization_prompt=GRID_PROMPT, localization_schema=GRID_SCHEMA,
                    review_prompt=REVIEW_PROMPT, review_schema=REVIEW_SCHEMA,
                    gate='all four frames must pass spatial veto and crop review', frames=frames)
    (args.output / 'protocol.json').write_text(json.dumps(protocol, indent=2) + '\n')
    for a in frames:
        frame = args.source_run / a['source_frame']
        make_grid(frame, grids / frame.name)
    if args.prepare_only:
        print(f'Prepared {len(frames)} grid images')
        return

    results, shots = [], {}
    with (args.output / 'review.jsonl').open('w') as log:
        for a in frames:
            frame = args.source_run / a['source_frame']
            grid = grids / frame.name
            print(f"{a['id']} panel {a['panel']}", flush=True)
            start = monotonic()
            row = localize_and_review(frame, grid, crops / frame.name,
                                      rules_by_group[cases[a['id']]['group']], args)
            row.update(id=a['id'], panel=a['panel'], condition=a['condition'],
                       expected=a['expected_conflict_confirmation'], frame_sha256=a['frame_sha256'],
                       grid_sha256=sha256(grid.read_bytes()).hexdigest(), seconds=monotonic() - start)
            row['iou'] = {role: box_iou(row['boxes_px'].get(role), ref)
                          for role, ref in a['boxes_px'].items() if ref is not None}
            row['localized_hidden_part'] = (a['boxes_px']['part'] is None
                                            and row['boxes_px'].get('part') is not None)
            log.write(json.dumps(row) + '\n')
            log.flush()
            results.append(row)
            shots.setdefault(a['id'], []).append(row)

    shot_results = [dict(id=case, condition=group[0]['condition'],
                         expected=all(row['expected'] for row in group),
                         confirmed=all(row['confirmed'] for row in group),
                         confirmed_panels=[row['panel'] for row in group if row['confirmed']],
                         **({'error': 'invalid frame reply'} if any('error' in row for row in group) else {}))
                    for case, group in shots.items()]
    conditions = score(results, 'condition')
    for condition, scores in conditions.items():
        group = [row for row in results if row['condition'] == condition]
        scores['stage_counts'] = {stage: sum(row['stage'] == stage for row in group)
                                  for stage in sorted({row['stage'] for row in group})}
        scores['crop_review_calls'] = sum(row['model_calls'] - 1 for row in group)
        scores['localized_hidden_parts'] = sum(row['localized_hidden_part'] for row in group)
        scores['mean_iou'] = {}
        for role in ROLES:
            values = [row['iou'][role] for row in group if role in row['iou']]
            if values:
                scores['mean_iou'][role] = sum(values) / len(values)
    report = dict(model_calls=sum(row['model_calls'] for row in results),
                  frames=conditions, shots=score(shot_results, 'condition'),
                  shot_results=shot_results)
    text = json.dumps(report, indent=2)
    (args.output / 'report.json').write_text(text + '\n')
    print(text)


if __name__ == '__main__':
    main()
