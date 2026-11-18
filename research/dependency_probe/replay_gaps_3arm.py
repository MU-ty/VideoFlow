"""Compare generic, fixed-comprehensive and gap-directed checks at matched call budgets."""
import argparse
import collections
import json
from pathlib import Path

from replay_active import FIXED, duration, ffmpeg, make_pair, prepare_archive
from replay_pairing import infer, shot_prompt
from run_online import (
    ROOT, dependency_result, live_setup, load_json, model_identity, parse_panels, structured_state,
)

ARMS = ('generic', 'fixed-comprehensive', 'gap-directed')
ACTIONS = {
    'identity-conflict': (
        'Check both marker identities independently. Trace the actual outline on the host body. '
        'Count visible corners if the identities differ in shape. Ignore part contours, shadows '
        'and reflections. Use unknown if the marker cannot be distinguished.'
    ),
    'ownership': (
        'Trace the visible physical body that carries the marker, then follow that same body to '
        'the edited part. A marker on a host body and a part mounted elsewhere on that body still '
        'belong to the same object. Do not require the marker to be drawn on the part. '
        'Mark unknown if occlusion prevents tracing this relationship.'
    ),
    'marker-description': (
        'Check whether the marking is an elongated stripe with a rectangular outline. In this '
        'task record an elongated band, including a rectangular band, as stripe. Use rectangle '
        'for a compact rectangular patch. Retain the observed color and verify the marker is on '
        'the same physical host as the edited part.'
    ),
    'coverage': (
        'First look for any physical host object, independently of its marker and whether it is '
        'the requested identity. An object with a different marker still counts as a visible '
        'host. Then check the part, marker, and ownership. Keep occluded fields unknown.'
    ),
}


def marker_matches(panel, marker):
    color, shape = marker.split()
    return (panel['color'], panel['shape']) == (color, shape)


def archived_panels(item, schema):
    rows = []
    for record in item['archived_calls']:
        if not record['phase'].startswith('verify'):
            continue
        try:
            rows.extend(parse_panels(record['raw'], schema))
        except (ValueError, KeyError, TypeError):
            continue
    return rows


def select_action(history, current, story, prior):
    """Route using model observations and the requested identity vocabulary."""
    target, distractor = story['target_marker'], story['distractor_marker']
    observed = [panel for panel in (*history, *current) if panel['host_visible'] == 'yes']
    seen_target = any(marker_matches(row, target) for row in observed)
    seen_excluded = any(marker_matches(row, distractor) for row in observed)
    if prior == 'confirmed' or (seen_target and seen_excluded):
        return 'identity-conflict'

    stripe_colors = {marker.split()[0] for marker in (target, distractor)
                     if marker.split()[1] == 'stripe'}
    if any(row['color'] in stripe_colors and row['shape'] == 'rectangle' for row in observed):
        return 'marker-description'
    if any(row['part_visible'] == 'yes' and row['same_instance'] != 'yes'
           and any(marker_matches(row, marker) for marker in (target, distractor))
           for row in observed):
        return 'ownership'
    return 'coverage'


def trial(item, story, history, images, prompt, arm, query):
    calls, panels = [], []
    for image in images:
        action = None
        request_prompt = prompt
        if arm == 'gap-directed':
            action = select_action(history, panels, story, item['verification'])
            request_prompt += '\n' + ACTIONS[action]
        elif arm == 'fixed-comprehensive':
            action = 'fixed-comprehensive'
            checklist = '\n'.join(f'- {text}' for text in ACTIONS.values())
            request_prompt += '\nApply the relevant items in this fixed checklist:\n' + checklist
        row, parsed = query(image, request_prompt)
        calls.append(dict(row, action=action, prompt=request_prompt))
        panels.extend(parsed or [])
    # Use only fresh evidence for adjudication and retain the archived decision on unknown.
    verdict = structured_state(panels, story)
    decision = {'confirmed': 'match', 'rejected': 'distinct'}.get(verdict, item['decision'])
    return dict(calls=calls, verdict=verdict, decision=decision)


def freeze(root, block, record, api, schema, reverse):
    plans = {}
    for shot, item in record['shots'].items():
        clip = root / item['clip']
        total = duration(clip)
        times = sorted(total * fraction for pair in FIXED for fraction in pair)
        directory = root / 'probes' / block / shot
        directory.mkdir(parents=True)
        images = []
        for index, seconds in enumerate((times[:2], times[2:]), 1):
            path = directory / f'{index}.png'
            make_pair(clip, seconds, path)
            images.append(dict(path=path.relative_to(root).as_posix(), seconds=seconds,
                               sha256=api['checksum'](path)))
        if reverse:
            images.reverse()
        plans[shot] = dict(images=images, history=archived_panels(item, schema),
                           prompt=shot_prompt(record['story']))
    return plans


def summarize(record, shot_results):
    initial = record['shots']
    baseline = dependency_result(initial)
    result = dict(initial=baseline, rounds=[])
    if not all(shot in shot_results for shot in initial):
        return result
    for index in range(2):
        methods = {}
        for arm in ARMS:
            if not all(len(shot_results[shot]) > index and arm in shot_results[shot][index]
                       for shot in initial):
                continue
            trials = {shot: shot_results[shot][index][arm] for shot in initial}
            scored = dependency_result({
                shot: dict(item, decision=trials[shot]['decision'],
                           verification=trials[shot]['verdict'])
                for shot, item in initial.items()
            })
            selected, before, expected = map(set, (scored['selected'], baseline['selected'],
                                                  baseline['expected']))
            dropped = before - selected
            methods[arm] = dict(
                scored, corrected_exclusions=sorted(dropped - expected),
                incorrect_exclusions=sorted(set(scored['missed'])),
                newly_dropped_targets=sorted(dropped & expected),
                initially_confirmed_rejected=sorted(s for s in trials
                    if initial[s]['verification'] == 'confirmed' and trials[s]['verdict'] == 'rejected'),
                call_errors=sum('error' in call for t in trials.values() for call in t['calls']),
                routed_actions=dict(collections.Counter(
                    call['action'] for t in trials.values() for call in t['calls'] if call['action'])),
            )
        if methods:
            result['rounds'].append(methods)
    return result


def totals(summary):
    rounds = []
    for repeat in range(2):
        counts = {}
        for arm in ARMS:
            rows = [seed['rounds'][repeat][arm] for seed in summary.values()]
            counts[arm] = dict(
                seeds=len(rows),
                seeds_with_miss=sum(bool(row['missed']) for row in rows),
                seeds_with_excess=sum(bool(row['excess']) for row in rows),
                call_errors=sum(row['call_errors'] for row in rows),
            )
        rounds.append(counts)
    return rounds


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archives', nargs='+', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--ollama', default='http://127.0.0.1:11434')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    args.ollama = args.ollama.rstrip('/')
    args.output.mkdir(parents=True)
    api = live_setup(ROOT / 'runtime')
    protocol = load_json(ROOT / 'verifier_protocol.json')
    schema = protocol['structured_schema']
    blocks = {}
    for number, archive in enumerate(args.archives, 1):
        root = args.output / f'archive-{number:02}'
        for name, record in prepare_archive(archive, root, include_all=True).items():
            if name in blocks:
                raise ValueError(f'duplicate source seed: {name}')
            plans = freeze(root, name, record, api, schema, reverse=len(blocks) % 2 == 1)
            blocks[name] = dict(record=record, plans=plans, path=root.name)
    write = api['write_json']
    write(args.output / 'protocol.json', dict(
        blocks=blocks, actions=ACTIONS, arm_order='rotate by candidate; reverse on second round', calls_per_candidate_per_arm=2, repeats=2,
        arms=ARMS, options=protocol['options'], structured_schema=schema,
    ))
    print(f'{len(blocks)} seeds, {sum(len(b["plans"]) for b in blocks.values())} candidates', flush=True)
    if args.prepare_only:
        return

    report = dict(status='running', warmups={}, trials={}, summary={})
    path = args.output / 'warmup.png'
    ffmpeg('-f', 'lavfi', '-i', 'color=c=gray:s=1536x432', '-frames:v', 1,
           '-threads', 1, '-update', 1, path)
    warm_image = dict(path=path.name, sha256=api['checksum'](path))
    candidate_index = 0
    for name, block in blocks.items():
        record = block['record']
        model = record['model']['model']
        if model not in report['warmups']:
            identity = model_identity(args, model)
            if identity['digest'] != record['model']['digest']:
                raise ValueError(f'archived model digest does not match installed model: {name}')
            row, _ = infer(args, api, model, args.output, warm_image,
                           'Describe these two blank panels.', schema)
            report['warmups'][model] = dict(row, identity=identity)
            write(args.output / 'report.json', report)
            if 'raw' not in row:
                raise RuntimeError(f'could not warm {model}; see report.json')
        if report['warmups'][model]['identity']['digest'] != record['model']['digest']:
            raise ValueError(f'archived model digest mismatch: {name}')
        results = report['trials'][name] = {}
        root = args.output / block['path']

        def query(image, prompt):
            return infer(args, api, model, root, image, prompt, schema)

        for shot, plan in block['plans'].items():
            runs = results[shot] = []
            rotation = candidate_index % len(ARMS)
            forward = ARMS[rotation:] + ARMS[:rotation]
            candidate_index += 1
            for repeat, order in enumerate((forward, forward[::-1])):
                trials = {'order': order}
                runs.append(trials)
                for arm in order:
                    print(f'{name}/{shot} round {repeat + 1}: {arm}', flush=True)
                    trials[arm] = trial(record['shots'][shot], record['story'], plan['history'],
                                        plan['images'], plan['prompt'], arm, query)
                    report['summary'][name] = summarize(record, results)
                    write(args.output / 'report.json', report)
    report['status'] = 'completed'
    report['totals_by_repeat'] = totals(report['summary'])
    write(args.output / 'report.json', report)
    print(json.dumps(report['totals_by_repeat'], indent=2), flush=True)


if __name__ == '__main__':
    main()
