"""Replay the two-story observation study and locally recheck candidate exclusions."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
from time import monotonic

from vision_counterevidence import (
    OBSERVATION_VALUES, decide_observations, ffmpeg, parse_observations,
    prepare_holdout, summarize,
)
from vision_probe import image_reply, parse_verification


ROLES = ('housing', 'marker', 'part')
BOX_SCHEMA = {
    'anyOf': [
        {'type': 'null'},
        {'type': 'array', 'minItems': 4, 'maxItems': 4,
         'items': {'type': 'integer', 'minimum': 0, 'maximum': 1000}},
    ],
}
LOCALIZE_SCHEMA = {
    'type': 'object', 'required': list(ROLES), 'additionalProperties': False,
    'properties': {role: BOX_SCHEMA for role in ROLES},
}
LOCALIZE_PROMPT = (
    'Localize three visible regions separately in this single source frame. '
    'housing is the physical object described as {housing}. '
    'marker is the geometric identity mark, which can be on another object. '
    'part is the {part}. Return a tight [left, top, right, bottom] box for '
    'each region on a 0 to 1000 scale relative to this whole frame. Use null '
    'if a region is occluded, absent, or ambiguous. An occluding cover is '
    'not the part it hides. The housing box must surround only that device '
    'and its attached parts, and exclude nearby signs and other devices.'
)
REVIEW_SCHEMA = {
    'type': 'object', 'required': list(OBSERVATION_VALUES), 'additionalProperties': False,
    'properties': {key: {'enum': values} for key, values in OBSERVATION_VALUES.items()},
}
REVIEW_PROMPT = (
    'The four panels are different views of the SAME source frame. Top left '
    'is the full frame with cyan housing, yellow marker, and green part boxes. '
    'Top right shows the housing crop. Bottom left shows the marker crop. '
    'Bottom right shows the part crop. Inspect the marker crop for the actual '
    'mark shape and the part crop for the {part}. Report visible only if the '
    'named part itself is clearly visible in its crop. A cover, nearby object, '
    'or expected location is insufficient. Check the full frame and housing '
    'crop to decide if both the observed marker and part physically belong '
    'to the same {housing}. Being inside one box or near each other is '
    'insufficient. If the marker is on another object use no. When visibility '
    'or attachment cannot be verified use unknown.'
)
COLORS = dict(zip(ROLES, ('cyan', 'yellow', 'lime')))
PANEL_WIDTH, PANEL_HEIGHT = 480, 270  # Published 2x2 holdout sheets.


def pixel_boxes(raw):
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != set(ROLES):
        raise ValueError('expected housing, marker, and part boxes')
    boxes = {}
    for role in ROLES:
        box = data[role]
        if box is None:
            raise ValueError(f'{role} could not be localized')
        if (not isinstance(box, list) or len(box) != 4
                or any(type(value) is not int or not 0 <= value <= 1000 for value in box)):
            raise ValueError(f'invalid {role} box')
        left, top, right, bottom = box
        if left >= right or top >= bottom:
            raise ValueError(f'empty {role} box')
        boxes[role] = (left * PANEL_WIDTH // 1000, top * PANEL_HEIGHT // 1000,
                       (right * PANEL_WIDTH + 999) // 1000,
                       (bottom * PANEL_HEIGHT + 999) // 1000)
    x1, y1, x2, y2 = boxes['housing']
    for role in ('marker', 'part'):
        left, top, right, bottom = boxes[role]
        if not (x1 <= left < right <= x2 and y1 <= top < bottom <= y2):
            raise ValueError(f'{role} box is outside the device box')
    a, b = boxes['marker'], boxes['part']
    if max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3]):
        raise ValueError('marker and part boxes overlap')
    return boxes


def make_review_image(frame, output, boxes):
    filters = []
    outline = []
    for i, role in enumerate(ROLES):
        x1, y1, x2, y2 = boxes[role]
        w, h = x2 - x1, y2 - y1
        outline.append(f'drawbox=x={x1}:y={y1}:w={w}:h={h}:color={COLORS[role]}:t=2')
        filters.append(
            f'[src{i}]crop={w}:{h}:{x1}:{y1}:exact=1,'
            f'scale={PANEL_WIDTH}:{PANEL_HEIGHT}:force_original_aspect_ratio=decrease,'
            f'pad={PANEL_WIDTH}:{PANEL_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1[p{i}]'
        )
    graph = ['[0:v]split=4[full][src0][src1][src2]',
             '[full]' + ','.join(outline) + '[p3]', *filters,
             '[p3][p0][p1][p2]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0']
    ffmpeg('-i', frame, '-filter_complex_threads', 1, '-filter_complex',
           ';'.join(graph), '-frames:v', 1, '-threads', 1, output)


def parse_review(raw):
    data = json.loads(raw)
    if (not isinstance(data, dict) or set(data) != set(OBSERVATION_VALUES)
            or any(data[key] not in values for key, values in OBSERVATION_VALUES.items())):
        raise ValueError('invalid crop review')
    return data


def recheck_panel(case, panel, rules, args):
    name = f"{case['id']}-p{panel}"
    frame = args.output / 'crops' / f'{name}.png'
    review = frame.with_name(f'{name}-review.png')
    x, y = (panel - 1) % 2 * PANEL_WIDTH, (panel - 1) // 2 * PANEL_HEIGHT
    ffmpeg('-i', args.output / case['image'], '-vf', f'crop={PANEL_WIDTH}:{PANEL_HEIGHT}:{x}:{y}',
           '-frames:v', 1, '-threads', 1, frame)
    row = dict(id=case['id'], panel=panel, confirmed=False, model_calls=1,
               source_image_sha256=case['image_sha256'],
               frame_sha256=sha256(frame.read_bytes()).hexdigest())
    start = monotonic()
    row['localization_raw'] = image_reply(args.server, args.model, frame,
                                           LOCALIZE_PROMPT.format(**rules), LOCALIZE_SCHEMA)
    try:
        boxes = pixel_boxes(row['localization_raw'])
        row['boxes_px'] = boxes
        make_review_image(frame, review, boxes)
        row['review_image_sha256'] = sha256(review.read_bytes()).hexdigest()
        row['model_calls'] += 1
        row['review_raw'] = image_reply(args.server, args.model, review,
                                        REVIEW_PROMPT.format(**rules), REVIEW_SCHEMA)
        observed = parse_review(row['review_raw'])
        row['observations'] = observed
        row['confirmed'] = (observed['marker_shape'] == rules['conflicting_shape']
                            and observed['part_visibility'] == 'visible'
                            and observed['same_housing'] == 'yes')
        row['reason'] = 'confirmed' if row['confirmed'] else 'crop_did_not_confirm_conflict'
    except ValueError as error:
        row['reason'] = str(error)
    row['seconds'] = monotonic() - start
    return row


def replay_rows(path, protocol):
    cases = {case['id']: case for case in protocol['cases']}
    indexed = {}
    for line in path.read_text().splitlines():
        row = json.loads(line)
        case = cases.get(row['id'])
        method = row['method']
        key = row['id'], method
        if (case is None or method not in ('baseline', 'observations') or key in indexed
                or any(row[field] != case[field] for field in ('image_sha256', 'group', 'gold'))):
            raise ValueError(f'replay log does not match the frozen holdout: {key}')
        try:
            if method == 'baseline':
                decision = 'match' if parse_verification(row['raw']) else 'unknown'
            else:
                observations = parse_observations(row['raw'], case['panels'])
                rules = protocol['observation_rules'][case['group']]
                decision, _, _ = decide_observations(observations, rules)
        except ValueError:
            decision = 'unknown'
        if decision != row['decision']:
            raise ValueError(f'cannot reproduce logged decision: {key}')
        indexed[key] = row
    if len(indexed) != 2 * len(cases):
        raise ValueError('replay log must contain both methods for every holdout case')
    return indexed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--responses', type=Path, required=True,
                        help='responses.jsonl from the frozen --holdout run')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--server', default='http://127.0.0.1:11434')
    parser.add_argument('--model', default='qwen2.5vl:7b-ctx8k')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'crops').mkdir()
    protocol = prepare_holdout(args.output)
    previous = replay_rows(args.responses, protocol)
    protocol.update(model=args.model, source_responses_sha256=sha256(args.responses.read_bytes()).hexdigest(),
                    localization_prompt=LOCALIZE_PROMPT, review_prompt=REVIEW_PROMPT,
                    localization_schema=LOCALIZE_SCHEMA, review_schema=REVIEW_SCHEMA,
                    gate='distinct only when every sampled frame passes local confirmation')
    (args.output / 'protocol.json').write_text(json.dumps(protocol, indent=2) + '\n')

    guarded, candidate_cases, model_calls = [], 0, 0
    with (args.output / 'review.jsonl').open('w') as log, (args.output / 'responses.jsonl').open('w') as results:
        for case in protocol['cases']:
            old = previous[case['id'], 'observations']
            new = {key: old[key] for key in ('id', 'group', 'gold', 'image_sha256', 'decision')}
            new.update(method='local_recheck', candidate=old['decision'] == 'distinct')
            if new['candidate']:
                candidate_cases += 1
                rules = protocol['observation_rules'][case['group']]
                confirmed = []
                for panel in range(1, case['panels'] + 1):
                    print(f"{case['id']} panel {panel}", flush=True)
                    row = recheck_panel(case, panel, rules, args)
                    model_calls += row['model_calls']
                    if row['confirmed']:
                        confirmed.append(panel)
                    log.write(json.dumps(row) + '\n')
                    log.flush()
                new['confirmed_panels'] = confirmed
                if len(confirmed) != case['panels']:
                    new['decision'] = 'unknown'
            guarded.append(new)
            for row in (previous[case['id'], 'baseline'], old, new):
                results.write(json.dumps(row) + '\n')
            results.flush()
    recorded = list(previous.values())
    report = dict(candidate_cases=candidate_cases, additional_model_calls=model_calls,
                  metrics=summarize(recorded, 'observations'))
    for group, rows in summarize(recorded + guarded, 'local_recheck').items():
        report['metrics'][group]['local_recheck'] = rows['local_recheck']
    text = json.dumps(report, indent=2)
    (args.output / 'report.json').write_text(text + '\n')
    print(text)


if __name__ == '__main__':
    main()
