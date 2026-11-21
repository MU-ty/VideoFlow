"""Compare full paired frames with the same frames plus a manual decal inset."""

import argparse
import base64
import json
from pathlib import Path
import subprocess
import time

import run_anchor_pair as pair

VIEWS = ('full', 'inset')
CONDITIONS = [(view, arm) for view in VIEWS for arm in pair.ARMS]
WIDTH, HEIGHT = 128, 112
FRAME_WIDTH, FRAME_HEIGHT = 768, 432
REFERENCE_DIGEST = '9304a775f2a97356548183530c45c96ee2a11b8e6bb7c1a9e46b791472a1af8d'


def inset_pair(source, output, anchor, candidate):
    filters = ['[0:v]split=3[whole][a0][c0]']
    for label, offset, center in (('a', 0, anchor), ('c', FRAME_WIDTH, candidate)):
        x, y = center
        if not (0 <= x < FRAME_WIDTH and 0 <= y < FRAME_HEIGHT):
            raise ValueError(f'invalid decal center: {center}')
        left = max(0, min(x - WIDTH // 2, FRAME_WIDTH - WIDTH)) + offset
        top = max(0, min(y - HEIGHT // 2, FRAME_HEIGHT - HEIGHT))
        filters.append(f'[{label}0]crop={WIDTH}:{HEIGHT}:{left}:{top},'
                       f'scale=256:224:flags=bicubic,pad=768:224:256:0:color=gray[{label}]')
    filters.append('[a][c]hstack=inputs=2[insets];[whole][insets]vstack=inputs=2')
    subprocess.run([
        'ffmpeg', '-nostdin', '-v', 'error', '-filter_complex_threads', '1', '-i', str(source),
        '-filter_complex', ';'.join(filters), '-frames:v', '1', '-threads', '1',
        '-update', '1', str(output),
    ], check=True)


def prepare(source, output, regions, cases=None):
    cases = pair.CASES if cases is None else cases
    if set(regions) != set(cases):
        raise ValueError('region blocks differ from the evaluation cases')
    inputs = pair.build_inputs(source, cases, output)
    panels = tuple(p + 1 for p in pair.VIEWS)
    for item in inputs:
        block = regions[item['block']]
        index = panels.index(item['panel'])
        anchor, candidate = block['S1'][index], block[item['shot']][index]
        image = output / item['file']
        inset = image.with_name(image.stem + '-inset.png')
        inset_pair(image, inset, anchor, candidate)
        item['inset'] = {'file': inset.name, 'sha256': pair.sha(inset),
                         'centers_xy': [anchor, candidate]}
    return inputs


def run(server, model, output, inputs):
    before = pair.model_info(server, model)
    if before['digest'].removeprefix('sha256:') != REFERENCE_DIGEST:
        raise RuntimeError(f'expected the archived Round 925 model ({REFERENCE_DIGEST}), got {before}')
    votes = {(block, view, arm, shot): [] for block in pair.CASES
             for view, arm in CONDITIONS for shot in pair.CANDIDATES}
    errors = 0
    with (output / 'calls.jsonl').open('x', encoding='utf-8') as log:
        for index, item in enumerate(inputs):
            paths = {'full': item, 'inset': item['inset']}
            images = {view: base64.b64encode((output / paths[view]['file']).read_bytes()).decode()
                      for view in VIEWS}
            offset = index % len(CONDITIONS)
            for view, arm in CONDITIONS[offset:] + CONDITIONS[:offset]:
                prompt = pair.COMMON.format(part=pair.CASES[item['block']]['part'])
                if view == 'inset':
                    prompt += ('The bottom row shows a magnified region of each frame where '
                               'the decal may appear. Use the top row for host and control '
                               'ownership. If the inset has no readable decal, mark it unknown.\n')
                prompt += pair.DIRECT if arm == pair.ARMS[0] else pair.STRUCTURED
                raw, error, decision = None, None, 'unknown'
                start = time.monotonic()
                try:
                    raw = pair.request(server, '/api/chat', {
                        'model': model, 'stream': False, 'options': pair.OPTIONS,
                        'format': pair.SCHEMAS[arm],
                        'messages': [{'role': 'user', 'content': prompt, 'images': [images[view]]}],
                    })['message']['content']
                    decision = pair.vote(raw, arm)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    error = str(exc)
                    errors += 1
                votes[item['block'], view, arm, item['shot']].append(decision)
                row = {k: item[k] for k in ('block', 'shot', 'panel')}
                log.write(json.dumps(dict(row, view=view, arm=arm, sha256=paths[view]['sha256'],
                                          prompt=prompt, raw=raw, vote=decision, error=error,
                                          elapsed_s=round(time.monotonic() - start, 3))) + '\n')
                log.flush()
            print(f"{index + 1}/{len(inputs)} {item['block']} {item['shot']} panel {item['panel']}",
                  flush=True)
    if pair.model_info(server, model) != before:
        raise RuntimeError('model identity changed during the run')
    results = {}
    summary = {}
    for view, arm in CONDITIONS:
        rows = {}
        for block, case in pair.CASES.items():
            expected = set(case['targets'])
            raw = {shot: votes[block, view, arm, shot] for shot in pair.CANDIDATES}
            states = {shot: pair.aggregate(ballot) for shot, ballot in raw.items()}
            selected = {shot for shot, state in states.items() if state != 'distinct'}
            rows[block] = dict(votes=raw, states=states, selected=sorted(selected),
                               missed=sorted(expected - selected), excess=sorted(selected - expected),
                               exact=selected == expected)
        name = f'{view}/{arm}'
        results[name] = rows
        summary[name] = dict(seeds=len(rows), exact=sum(r['exact'] for r in rows.values()),
                             misses=sum(len(r['missed']) for r in rows.values()),
                             excess=sum(len(r['excess']) for r in rows.values()))
    return dict(model=before, options=pair.OPTIONS, calls_per_seed_per_condition=6,
                call_errors=errors, summary=summary, results=results)


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
    print(f'Prepared {len(inputs)} pairs with and without an inset', flush=True)
    if not args.prepare_only:
        result = run(args.server.rstrip('/'), args.model, args.output, inputs)
        (args.output / 'report.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result['summary'], indent=2))
        if result['call_errors']:
            raise SystemExit('Call errors recorded; do not score this run as a clean experiment.')


if __name__ == '__main__':
    main()
