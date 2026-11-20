"""Six-call paired source-panel diagnostic for Table VI identity errors."""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request

CASES = {
    'amplifier-chain-92511': {'part': 'volume knob', 'targets': ['S3']},
    'camera-fanout-92522': {'part': 'top mode dial', 'targets': ['S3']},
    'radio-fanout-92542': {'part': 'front tuning knob', 'targets': ['S3']},
}
CANDIDATES = ('S2', 'S3')
VIEWS = (0, 1, 3)  # Top-left, top-right, bottom-right on the archived 2x2 sheets.
ARMS = ('pair-checklist', 'pair-structured')
VERDICTS = ('match', 'distinct', 'unknown')
SHAPES = ('circle', 'triangle', 'diamond', 'hexagon', 'square', 'stripe', 'other', 'unknown')
COLORS = ('white', 'black', 'silver', 'gray', 'red', 'orange', 'yellow', 'green',
          'blue', 'purple', 'other', 'unknown')
OPTIONS = {'temperature': 0, 'seed': 925, 'num_predict': 300, 'num_ctx': 8192}

COMMON = """The left frame is the intended object (anchor). For the right frame,
determine whether the requested control belongs to the same marked object.
Use the outer shape and color of the separate identity decal on each host,
and check that the named control and the decal belong to the same host in
each frame. Ignore knob outlines, dial rings, text or icons inside decals,
and background objects. A faceted gem icon counts as diamond. Do not infer
a shape from a cropped or obscured edge. Sharing a kind of device or a
color alone is insufficient. The view and decal position may change. Use unknown when shape or color
cannot be placed in the supported vocabulary. Shapes: circle, triangle,
diamond, hexagon, square, stripe. Colors: white, black, silver, gray, red,
orange, yellow, green, blue, purple.
Requested control: {part}.
"""
DIRECT = """Apply this checklist before deciding: locate each host, separate
the decal from the control, compare both decals, and verify control ownership.
Return match if both linked decals agree on shape and color, distinct if
both are readable and disagree, and unknown if a required cue is unclear.
"""
STRUCTURED = """Describe each frame independently before the host comparison.
Set linked=yes only when the named control and the separate decal can both
be traced to that host in the image. Use unknown for any unreadable cue."""
PANEL = {
    'type': 'object', 'additionalProperties': False,
    'required': ['shape', 'color', 'linked'],
    'properties': {
        'shape': {'type': 'string', 'enum': SHAPES},
        'color': {'type': 'string', 'enum': COLORS},
        'linked': {'type': 'string', 'enum': ('yes', 'no', 'unknown')},
    },
}
SCHEMAS = {
    'pair-checklist': {
        'type': 'object', 'additionalProperties': False,
        'required': ['verdict'],
        'properties': {'verdict': {'type': 'string', 'enum': VERDICTS}},
    },
    'pair-structured': {
        'type': 'object', 'additionalProperties': False,
        'required': ['anchor', 'candidate'], 'properties': {'anchor': PANEL, 'candidate': PANEL},
    },
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def request(server, path, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(server + path, data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.load(response)


def model_info(server, model):
    digest, = (x['digest'] for x in request(server, '/api/tags')['models'] if x['name'] == model)
    return {'name': model, 'digest': digest, 'server_version': request(server, '/api/version')['version']}


def pair_frames(anchor, candidate, view, out):
    x, y = 768 * (view % 2), 432 * (view // 2)
    filters = f'[0:v]crop=768:432:{x}:{y}[a];[1:v]crop=768:432:{x}:{y}[c];[a][c]hstack'
    subprocess.run([
        'ffmpeg', '-nostdin', '-v', 'error', '-filter_complex_threads', '1',
        '-i', str(anchor), '-i', str(candidate), '-filter_complex', filters,
        '-frames:v', '1', '-threads', '1', '-update', '1', str(out),
    ], check=True)


def vote(raw, arm):
    data = json.loads(raw)
    if arm == 'pair-checklist':
        if set(data) != {'verdict'} or data['verdict'] not in VERDICTS:
            raise ValueError('invalid direct verdict')
        return data['verdict']
    if set(data) != {'anchor', 'candidate'}:
        raise ValueError('invalid pair structure')
    anchor, candidate = data['anchor'], data['candidate']
    for panel in (anchor, candidate):
        if (set(panel) != {'shape', 'color', 'linked'} or panel['shape'] not in SHAPES
                or panel['color'] not in COLORS or panel['linked'] not in ('yes', 'no', 'unknown')):
            raise ValueError('invalid panel fields')
    if any(panel['linked'] != 'yes' or panel['shape'] in ('other', 'unknown')
           or panel['color'] in ('other', 'unknown') for panel in (anchor, candidate)):
        return 'unknown'
    return 'match' if anchor == candidate else 'distinct'


def aggregate(votes):
    for decision, opposite in (('match', 'distinct'), ('distinct', 'match')):
        if votes.count(decision) >= 2 and opposite not in votes:
            return decision
    return 'unknown'


def build_inputs(sources, cases, out):
    report = json.loads((sources / 'source-report.json').read_text())
    inputs = []
    for block, case in sorted(cases.items()):
        labels = case['targets']
        if not labels or len(set(labels)) != len(labels) or set(labels) - set(CANDIDATES):
            raise ValueError(f'invalid candidate labels: {block}')
        frames = {s: sources / 'screen' / block / f'{s}.png' for s in ('S1', *CANDIDATES)}
        for shot, path in frames.items():
            if sha(path) != report[block]['screening_inputs'][shot]['sheet_sha256']:
                raise ValueError(f'frozen panel hash mismatch: {block}/{shot}')
        for shot in CANDIDATES:
            for view in VIEWS:
                path = out / f'{block}-{shot}-panel{view+1}.png'
                pair_frames(frames['S1'], frames[shot], view, path)
                inputs.append(dict(block=block, shot=shot, panel=view+1, file=path.name, sha256=sha(path)))
    return inputs


def run(server, model, cases, out, inputs):
    before = model_info(server, model)
    votes = {(k, arm, shot): [] for k in cases for arm in ARMS for shot in CANDIDATES}
    errors = 0
    with (out / 'calls.jsonl').open('x', encoding='utf-8') as log:
        for index, item in enumerate(inputs):
            image = base64.b64encode((out / item['file']).read_bytes()).decode()
            for arm in (ARMS if index % 2 == 0 else ARMS[::-1]):
                prompt = COMMON.format(part=cases[item['block']]['part'])
                prompt += DIRECT if arm == ARMS[0] else STRUCTURED
                raw, error, decision = None, None, 'unknown'
                try:
                    raw = request(server, '/api/chat', {
                        'model': model, 'stream': False, 'options': OPTIONS,
                        'format': SCHEMAS[arm],
                        'messages': [{'role': 'user', 'content': prompt, 'images': [image]}],
                    })['message']['content']
                    decision = vote(raw, arm)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    error = str(exc)
                    errors += 1
                votes[item['block'], arm, item['shot']].append(decision)
                log.write(json.dumps(dict(item, arm=arm, prompt=prompt, raw=raw,
                                          error=error, vote=decision)) + '\n')
                log.flush()
    if model_info(server, model) != before:
        raise RuntimeError('model identity changed during the run')
    results = {}
    for block, case in sorted(cases.items()):
        expected = set(case['targets'])
        results[block] = {}
        for arm in ARMS:
            raw = {shot: votes[block, arm, shot] for shot in CANDIDATES}
            states = {shot: aggregate(value) for shot, value in raw.items()}
            # Both methods use the same conservative policy for unresolved candidates.
            selected = {shot for shot, state in states.items() if state != 'distinct'}
            results[block][arm] = {
                'votes': raw, 'states': states, 'selected': sorted(selected),
                'exact': selected == expected, 'missed': sorted(expected - selected),
                'excess': sorted(selected - expected), 'coverage': expected <= selected,
            }
    return {'model': before, 'call_errors': errors, 'calls_per_seed_per_arm': 6,
            'results': results, 'options': OPTIONS}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--server', default='http://127.0.0.1:11434')
    parser.add_argument('--model', default='qwen2.5vl:7b-ctx8k')
    parser.add_argument('--cases', type=Path, help='Optional JSON with frozen block names, parts and target labels.')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text()) if args.cases else CASES
    if not cases:
        parser.error('at least one frozen case is required')
    args.output.mkdir(parents=True)
    inputs = build_inputs(args.source, cases, args.output)
    (args.output / 'inputs.json').write_text(json.dumps(inputs, indent=2) + '\n')
    (args.output / 'cases.json').write_text(json.dumps(cases, indent=2) + '\n')
    print(f'Prepared {len(inputs)} frame pairs ({6 * len(cases)} calls per arm)', flush=True)
    if not args.prepare_only:
        result = run(args.server.rstrip('/'), args.model, cases, args.output, inputs)
        (args.output / 'report.json').write_text(json.dumps(result, indent=2) + '\n')
        for arm in ARMS:
            rows = [pair[arm] for pair in result['results'].values()]
            print(arm, 'exact', sum(r['exact'] for r in rows), 'of', len(rows),
                  'missed', sum(len(r['missed']) for r in rows),
                  'excess', sum(len(r['excess']) for r in rows))
        if result['call_errors']:
            raise SystemExit('Call errors recorded; inspect calls.jsonl before using scores.')


if __name__ == '__main__':
    main()
