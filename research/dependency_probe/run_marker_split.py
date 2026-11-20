"""Equal-call development diagnostic for separate decal reading and ownership."""

import argparse
import base64
from collections import defaultdict
import json
from pathlib import Path
import subprocess
import time

import run_anchor_pair as pair
import run_marker_region as region

BASELINE, SPLIT = 'full-direct-repeat', 'split-marker'
READ = """These are two independent image crops, left anchor and right candidate.
Read each crop separately. Report visible=yes only if the complete external
boundary of a separate identity decal is visible. For a missing, cropped, or
occluded decal report no or unknown as appropriate. A knob, dial ring, screw,
or a symbol within a decal does not count. Report the outer shape and color
of the decal itself; a faceted gem counts as diamond. Do not compare identity.
Use unknown for unsupported shapes or colors. Supported shapes: circle,
triangle, diamond, hexagon, square, stripe. Supported colors: white, black,
silver, gray, red, orange, yellow, green, blue, purple.
"""
OWNERSHIP = """Each half is a full frame. The yellow outline indicates the
source region of a potential decal. Independently for each half, decide if
a distinct decal in that region is on the same physical device as the
requested control. Return yes only when both the decal and the control can be
identified on the same device. Return no for visibly different devices and
unknown when either is missing, ambiguous, cut off, or occluded. Do not
compare the devices or describe decal shape or color.
Requested control: {part}.
"""
TRISTATE = ('yes', 'no', 'unknown')


def enum(values):
    return {'type': 'string', 'enum': values}


def obj(**fields):
    return {'type': 'object', 'additionalProperties': False,
            'required': list(fields), 'properties': fields}


READ_SIDE = obj(visible=enum(TRISTATE), shape=enum(pair.SHAPES), color=enum(pair.COLORS))
READ_SCHEMA = obj(anchor=READ_SIDE, candidate=READ_SIDE)
OWNERSHIP_SCHEMA = obj(anchor=enum(TRISTATE), candidate=enum(TRISTATE))


def validated(data, schema):
    if 'properties' in schema:
        fields = schema['properties']
        return isinstance(data, dict) and set(data) == set(fields) and all(
            validated(data[key], sub) for key, sub in fields.items())
    return isinstance(data, str) and data in schema['enum']


def combine(reading, ownership):
    if reading is None or ownership is None or any(
        reading[side]['visible'] != 'yes'
        or reading[side]['shape'] in ('unknown', 'other')
        or reading[side]['color'] in ('unknown', 'other')
        or ownership[side] != 'yes'
        for side in ('anchor', 'candidate')
    ):
        return 'unknown'
    return 'match' if reading['anchor'] == reading['candidate'] else 'distinct'


def transform(source, output, filters):
    subprocess.run([
        'ffmpeg', '-nostdin', '-v', 'error', '-filter_threads', '1',
        '-i', str(source), '-vf', filters, '-frames:v', '1',
        '-threads', '1', '-update', '1', str(output),
    ], check=True)
    return {'file': output.name, 'sha256': pair.sha(output)}


def prepare(source, output, regions):
    inputs = region.prepare(source, output, regions)
    for item in inputs:
        full = output / item['file']
        inset = output / item['inset']['file']
        item['crop'] = transform(
            inset, full.with_name(full.stem + '-crop.png'),
            'split[a][b];[a]crop=256:224:256:432[left];'
            '[b]crop=256:224:1024:432[right];[left][right]hstack',
        )
        boxes = []
        for side, (x, y) in enumerate(item['inset']['centers_xy']):
            left = max(0, min(x - region.WIDTH // 2, region.FRAME_WIDTH - region.WIDTH))
            top = max(0, min(y - region.HEIGHT // 2, region.FRAME_HEIGHT - region.HEIGHT))
            boxes.append(f'drawbox=x={left + side * region.FRAME_WIDTH}:y={top}:'
                         f'w={region.WIDTH}:h={region.HEIGHT}:color=yellow:t=2')
        item['outlined'] = transform(full, full.with_name(full.stem + '-outlined.png'),
                                     ','.join(boxes))
    return inputs


def query(server, model, output, log, item, arm, stage, image, prompt, schema, seed):
    options = dict(pair.OPTIONS, seed=seed)
    raw, parsed, error = None, None, None
    start = time.monotonic()
    try:
        encoded = base64.b64encode((output / image['file']).read_bytes()).decode()
        raw = pair.request(server, '/api/chat', {
            'model': model, 'stream': False, 'options': options, 'format': schema,
            'messages': [{'role': 'user', 'content': prompt, 'images': [encoded]}],
        })['message']['content']
        result = json.loads(raw)
        if not validated(result, schema):
            raise ValueError('reply does not match the response schema')
        parsed = result
    except (OSError, ValueError, KeyError, TypeError) as exc:
        error = str(exc)
    identity = {k: item[k] for k in ('block', 'shot', 'panel')}
    log.write(json.dumps(dict(identity, arm=arm, stage=stage, file=image['file'],
                              sha256=image['sha256'], seed=seed, prompt=prompt, raw=raw,
                              parsed=parsed, error=error,
                              elapsed_s=round(time.monotonic() - start, 3))) + '\n')
    log.flush()
    return parsed, error is not None


def score(ballots):
    results = defaultdict(dict)
    summary = {}
    for arm in ('full-direct-first', BASELINE, SPLIT):
        for block, case in pair.CASES.items():
            votes = {shot: ballots[arm, block, shot] for shot in pair.CANDIDATES}
            states = {shot: pair.aggregate(values) for shot, values in votes.items()}
            selected = {shot for shot, state in states.items() if state != 'distinct'}
            expected = set(case['targets'])
            results[arm][block] = dict(votes=votes, states=states, selected=sorted(selected),
                                       exact=selected == expected, missed=sorted(expected - selected),
                                       excess=sorted(selected - expected))
        rows = results[arm].values()
        summary[arm] = dict(seeds=len(rows), exact=sum(r['exact'] for r in rows),
                            misses=sum(len(r['missed']) for r in rows),
                            excess=sum(len(r['excess']) for r in rows))
    return results, summary


def run(server, model, output, inputs):
    before = pair.model_info(server, model)
    if before['digest'].removeprefix('sha256:') != region.REFERENCE_DIGEST:
        raise RuntimeError(f'expected the archived Round 925 model, got {before}')
    ballots = defaultdict(list)
    errors = 0
    with (output / 'calls.jsonl').open('x', encoding='utf-8') as log:
        for index, item in enumerate(inputs):
            block, shot = item['block'], item['shot']
            part = pair.CASES[block]['part']
            direct = pair.COMMON.format(part=part) + pair.DIRECT
            schema = pair.SCHEMAS[pair.ARMS[0]]
            plans = {
                BASELINE: [('direct-first', item, direct, schema),
                           ('direct-repeat', item, direct, schema)],
                SPLIT: [('read', item['crop'], READ, READ_SCHEMA),
                        ('ownership', item['outlined'], OWNERSHIP.format(part=part), OWNERSHIP_SCHEMA)],
            }
            order = (BASELINE, SPLIT) if index % 2 == 0 else (SPLIT, BASELINE)
            for arm in order:
                responses = []
                for seed, (stage, image, prompt, schema) in enumerate(plans[arm], pair.OPTIONS['seed']):
                    parsed, failed = query(server, model, output, log, item, arm, stage,
                                           image, prompt, schema, seed)
                    responses.append(parsed)
                    errors += failed
                if arm == BASELINE:
                    first, second = [(value or {}).get('verdict', 'unknown') for value in responses]
                    ballots['full-direct-first', block, shot].append(first)
                    verdict = first if first == second else 'unknown'
                else:
                    verdict = combine(*responses)
                ballots[arm, block, shot].append(verdict)
            print(f"{index + 1}/{len(inputs)} {block} {shot} panel {item['panel']}", flush=True)
    if pair.model_info(server, model) != before:
        raise RuntimeError('model identity changed during the run')
    results, summary = score(ballots)
    return dict(model=before, calls_per_seed={BASELINE: 12, SPLIT: 12, 'full-direct-first': 6},
                seeds_per_panel=[pair.OPTIONS['seed'], pair.OPTIONS['seed'] + 1],
                options=pair.OPTIONS, call_errors=errors, results=results, summary=summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--server', default='http://127.0.0.1:11434')
    parser.add_argument('--model', default='qwen2.5vl:7b-ctx8k')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    regions = json.loads(Path(__file__).with_name('marker_regions_925.json').read_text())
    args.output.mkdir(parents=True)
    inputs = prepare(args.source, args.output, regions)
    for name, data in (('inputs', inputs), ('cases', pair.CASES), ('regions', regions)):
        (args.output / f'{name}.json').write_text(json.dumps(data, indent=2) + '\n')
    print(f'Prepared {len(inputs)} paired panels', flush=True)
    if not args.prepare_only:
        result = run(args.server.rstrip('/'), args.model, args.output, inputs)
        (args.output / 'report.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result['summary'], indent=2))
        if result['call_errors']:
            raise SystemExit('Call errors recorded; do not score this run as a clean experiment.')


if __name__ == '__main__':
    main()
