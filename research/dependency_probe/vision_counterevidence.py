"""Paired verifier-only evaluation. No detector, worker, or render service calls."""
import argparse
import json
from hashlib import sha256
from pathlib import Path
import subprocess
from time import monotonic
from zipfile import ZipFile

from vision_probe import (
    VERIFY_INSTRUCTIONS, VERIFY_SCHEMA, image_reply, parse_verification, parse_visual,
)

PART_LINKS = ['target', 'alternative', 'unresolved']
SCHEMA = {
    'type': 'object',
    'required': ['panel', 'marker', 'part_link', 'evidence'],
    'additionalProperties': False,
    'properties': {
        'panel': {'enum': [None, 1, 2, 3, 4]},
        'marker': {'enum': PART_LINKS},
        'part_link': {'enum': PART_LINKS},
        'evidence': {'type': 'string'},
    },
}
INSTRUCTIONS = (
    'Verify object identity in the supplied source image. Inspect all panels before answering. '
    'First identify a visible identity marker and then check whether the named part is visibly '
    'attached to the same housing in that panel. Report marker and part_link separately. '
    'Use target for the specified target instance and alternative for the explicitly excluded '
    'instance. If any single panel shows the target marker and its attached named part, cite '
    'that panel even if an alternative is also visible. Otherwise cite an alternative only '
    'when its conflicting marker and its attached lookalike part are both visible together. '
    'Never infer an alternative from missing evidence, color alone, occlusion, or a crop. '
    'Do not combine evidence across panels. Use unresolved and null panel if no complete '
    'association is visible. Name both the visible marker and its link to the part in evidence. '
    'A single frame is panel 1. For a 2x2 sheet, panels are numbered in reading order.\n'
)
SINGLE_BASELINE = (
    'Inspect this single source frame. Check identity, part, and position separately. '
    'Return JSON with identity, part, and position as yes, no, or unknown. '
    'If all three are yes, use panel 1 and describe the visible evidence. '
    'Otherwise use a null panel. '
)
STATION_QUESTION = (
    'In any one panel, is the round red pushbutton mounted on the navy station intercom '
    'with an ivory triangular badge visible? An ivory plus-shaped badge marks a different intercom.'
)
FIXTURE = Path(__file__).with_name('fixtures') / 'egdr_evidence_resampling_v1.zip'
HOLDOUT_FIXTURE = FIXTURE.with_name('egdr_observation_holdout_v1.zip')

OBSERVATION_PROMPT = (
    'Record visual facts for each of the {panels} source panels. '
    'Number panels from 1 in reading order. Inspect the {housing}. '
    'Read the shape of the actual marker on the housing and check visibility of the {part}. '
    'Report same_housing as yes only if you can see both the marker and that part on the '
    'same physical object in this panel. Do not carry evidence between panels. Use '
    'unknown for an obscured or unclear marker or attachment. Use multiple for the '
    'marker shape when more than one candidate housing is visible in a panel. '
    'Return one observation per panel.\n'
)
OBSERVATION_RULES = {
    'lamp_regression': dict(housing='cream desk lamp base', part='silver dome shade',
                            target_shape='hexagon', conflicting_shape='triangle'),
    'station_transfer': dict(housing='navy station intercom faceplate', part='round red pushbutton',
                             target_shape='triangle', conflicting_shape='plus'),
}
OBSERVATION_VALUES = {
    'marker_shape': ['unknown', 'triangle', 'hexagon', 'plus', 'other', 'multiple'],
    'part_visibility': ['unknown', 'visible', 'not_visible'],
    'same_housing': ['unknown', 'yes', 'no'],
}


def observation_schema(panel_count):
    return {
        'type': 'array', 'minItems': panel_count, 'maxItems': panel_count,
        'items': {
            'type': 'object', 'required': ['panel', *OBSERVATION_VALUES],
            'additionalProperties': False,
            'properties': {
                'panel': {'enum': list(range(1, panel_count + 1))},
                **{key: {'enum': values} for key, values in OBSERVATION_VALUES.items()},
            },
        },
    }


def parse_observations(raw, panel_count):
    panels = json.loads(raw)
    fields = {'panel', *OBSERVATION_VALUES}
    if not isinstance(panels, list) or len(panels) != panel_count:
        raise ValueError('expected exactly one observation per source panel')
    for row in panels:
        if (not isinstance(row, dict) or set(row) != fields or type(row['panel']) is not int
                or any(row[key] not in values for key, values in OBSERVATION_VALUES.items())):
            raise ValueError('invalid visual observation')
    if {row['panel'] for row in panels} != set(range(1, panel_count + 1)):
        raise ValueError('duplicate or nonexistent source panel')
    return panels


def decide_observations(panels, rules):
    """Resolve edit-relative roles in code using the declared shape rules."""
    support = {'target': [], 'conflict': [], 'uncertain': []}
    for row in panels:
        bound = row['part_visibility'] == 'visible' and row['same_housing'] == 'yes'
        if bound and row['marker_shape'] == rules['target_shape']:
            support['target'].append(row['panel'])
        elif bound and row['marker_shape'] == rules['conflicting_shape']:
            support['conflict'].append(row['panel'])
        else:
            support['uncertain'].append(row['panel'])
    if support['target']:
        return 'match', 'target_part_bound_in_sample', support
    if support['conflict'] and not support['uncertain']:
        return 'distinct', 'conflicting_marker_and_part_bound_in_every_sample', support
    return 'unknown', 'insufficient_or_ambiguous_evidence', support


def counterevidence_verdict(raw, panel_count):
    row = json.loads(raw)
    if (not isinstance(row, dict) or set(row) != set(SCHEMA['required'])
            or row['marker'] not in PART_LINKS or row['part_link'] not in PART_LINKS
            or not isinstance(row['evidence'], str)):
        raise ValueError('invalid structured counterevidence')
    panel = row['panel']
    if panel is not None and (type(panel) is not int or not 1 <= panel <= panel_count):
        raise ValueError('counterevidence cites a nonexistent panel')
    if panel is None or not row['evidence'].strip() or row['marker'] != row['part_link']:
        return 'unknown'
    return {'target': 'match', 'alternative': 'distinct', 'unresolved': 'unknown'}[row['marker']]


def ffmpeg(*args):
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', *map(str, args)], check=True)


def prepare_cases(lamp, output):
    protocol = json.loads((lamp / 'protocol.json').read_text())
    original = (lamp / 'responses.jsonl').read_bytes()
    evidence = json.loads((lamp / 'evidence.json').read_text())
    log_digest = sha256(original).hexdigest()
    logged_digest = evidence['responses_sha256']
    if log_digest != logged_digest:
        print('Warning: exported lamp response log differs from the recorded digest. '
              'Both digests are recorded; the original image digests are checked separately.',
              flush=True)
    rows = [json.loads(line) for line in original.splitlines()]
    cases = []
    for shot, gold in [('S2', 'distinct'), ('S3', 'match')]:
        archived = [row for row in rows if row['probe'] == f'probe-{shot}']
        detector = [row for row in archived if row['stage'] in ('detect', 'detect_retry')][-1]
        detected = parse_visual(detector['raw'])
        if detected['decision'] != 'yes':
            raise ValueError(f'{shot}: archived detector did not nominate a panel')
        panel = detected['panel']
        previous = [row for row in archived if row['stage'].startswith('verify_isolated_panel')][-1]
        frame = lamp / f'{shot}-panel-{panel}.png'
        frame_bytes = frame.read_bytes()
        if sha256(frame_bytes).hexdigest() != evidence['audit']['probes'][shot]['panels'][panel - 1]:
            raise ValueError(f'{shot}: original verifier image digest mismatch')
        image = output / f'lamp-{shot}.png'
        image.write_bytes(frame_bytes)
        cases.append(dict(id=f'lamp-{shot}', group='lamp_regression', gold=gold,
                          image=image.name, panels=1, question=protocol['question'],
                          archived_detector=detector, archived_verification=previous,
                          source_panel=panel, archived_log_sha256=log_digest,
                          recorded_log_sha256=logged_digest))
    with ZipFile(FIXTURE) as bundle:
        for shot in ('S1', 'S2', 'S3'):
            clip = output / f'station-{shot}.mp4'
            clip.write_bytes(bundle.read(f'station_intercom/{shot}.mp4'))
            panels = ';'.join(
                f'[{i}:v]scale=480:270,setsar=1[p{i}]' for i in range(4))
            ffmpeg(*(arg for time in (0.2, 0.6, 1.0, 1.4) for arg in ('-ss', time, '-i', clip)),
                   '-filter_complex_threads', 1, '-filter_complex',
                   panels + ';[p0][p1][p2][p3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0',
                   '-frames:v', 1, '-threads', 1, output / f'station-{shot}.png')
            clip.unlink()
    # In the mixed sheet, the target appears only in the last panel.
    ffmpeg('-i', output / 'station-S3.png', '-i', output / 'station-S1.png',
           '-filter_complex_threads', 1, '-filter_complex',
           '[1:v]crop=480:270:480:270[p];[0:v][p]overlay=480:270',
           '-frames:v', 1, '-threads', 1, output / 'station-mixed.png')
    # Hide the badge area while leaving the pushbutton visible in each panel.
    for shot in ('S1', 'S3'):
        boxes = ','.join(
            f'drawbox=x={x}:y={y}:w=110:h=108:color=gray:t=fill'
            for x in (220, 700) for y in (35, 305))
        ffmpeg('-i', output / f'station-{shot}.png', '-vf', boxes,
               '-frames:v', 1, '-threads', 1, output / f'station-occluded-{shot}.png')
    for case_id, gold in (
        ('S1', 'match'), ('S2', 'match'), ('S3', 'distinct'), ('mixed', 'match'),
        ('occluded-S1', 'unknown'), ('occluded-S3', 'unknown'),
    ):
        cases.append(dict(id=f'station-{case_id}', group='station_transfer',
                          gold=gold, image=f'station-{case_id}.png', panels=4,
                          question=STATION_QUESTION))
    for case in cases:
        case['image_sha256'] = sha256((output / case['image']).read_bytes()).hexdigest()
    return cases


def prepare_holdout(output):
    with ZipFile(HOLDOUT_FIXTURE) as bundle:
        protocol = json.loads(bundle.read('protocol.json'))
        for case in protocol['cases']:
            image = case['image']
            if Path(image).name != image:
                raise ValueError('holdout image name must be a basename')
            data = bundle.read(image)
            if sha256(data).hexdigest() != case['image_sha256']:
                raise ValueError(f'holdout image digest mismatch: {image}')
            (output / image).write_bytes(data)
    protocol['fixture_sha256'] = sha256(HOLDOUT_FIXTURE.read_bytes()).hexdigest()
    return protocol


def summarize(rows, variant='counterevidence'):
    report = {}
    for group in dict.fromkeys(row['group'] for row in rows):
        for method in ('baseline', variant):
            subset = [row for row in rows if row['group'] == group and row['method'] == method]
            report.setdefault(group, {})[method] = dict(
                cases=len(subset),
                clear_targets=sum(row['gold'] == 'match' for row in subset),
                clear_non_targets=sum(row['gold'] == 'distinct' for row in subset),
                occluded_cases=sum(row['gold'] == 'unknown' for row in subset),
                missed_targets=sum(row['gold'] == 'match' and row['decision'] == 'distinct'
                                   for row in subset),
                non_target_retained=sum(row['gold'] == 'distinct' and row['decision'] != 'distinct'
                                         for row in subset),
                invalid_replies=sum('error' in row for row in subset),
                excluded_occluded=sum(row['gold'] == 'unknown' and row['decision'] == 'distinct'
                                      for row in subset),
                confirmed_targets=sum(row['gold'] == 'match' and row['decision'] == 'match'
                                      for row in subset),
            )
    return report


def evaluate(cases, args, log, observation_rules=OBSERVATION_RULES):
    rows = []
    for index, case in enumerate(cases):
        methods = ('baseline', args.variant)
        if index % 2:
            methods = methods[::-1]
        for method in methods:
            if method == 'baseline':
                instruction = SINGLE_BASELINE if case['panels'] == 1 else VERIFY_INSTRUCTIONS
                prompt, schema = instruction + case['question'], VERIFY_SCHEMA
            elif method == 'observations':
                rules = observation_rules[case['group']]
                prompt = OBSERVATION_PROMPT.format(panels=case['panels'], **rules)
                schema = observation_schema(case['panels'])
            else:
                prompt, schema = INSTRUCTIONS + case['question'], SCHEMA
            print(f'{case["id"]} {method}', flush=True)
            start = monotonic()
            raw = image_reply(args.server, args.model, args.output / case['image'],
                              prompt, schema)
            row = {key: case[key] for key in ('id', 'group', 'gold', 'image_sha256')}
            row.update(method=method, raw=raw, seconds=monotonic() - start)
            try:
                if method == 'baseline':
                    matched = parse_verification(raw)
                    if matched and json.loads(raw)['panel'] > case['panels']:
                        raise ValueError('baseline cites a nonexistent panel')
                    row['decision'] = 'match' if matched else 'unknown'
                elif method == 'counterevidence':
                    row['decision'] = counterevidence_verdict(raw, case['panels'])
                else:
                    observations = parse_observations(raw, case['panels'])
                    decision, reason, support = decide_observations(observations, rules)
                    row.update(decision=decision, observations=observations,
                               decision_reason=reason, support_panels=support)
            except ValueError as error:
                row.update(decision='unknown', error=str(error))
            log.write(json.dumps(row) + '\n')
            log.flush()
            rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--lamp-run', type=Path)
    source.add_argument('--holdout', action='store_true',
                        help='run the frozen observation policy on the two rendered holdouts')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--server', default='http://127.0.0.1:11434')
    parser.add_argument('--model', default='qwen2.5vl:7b-ctx8k')
    parser.add_argument('--variant', choices=('counterevidence', 'observations'))
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    args.variant = args.variant or ('observations' if args.holdout else 'counterevidence')
    args.output.mkdir(parents=True, exist_ok=False)
    protocol = prepare_holdout(args.output) if args.holdout else {}
    cases = protocol['cases'] if args.holdout else prepare_cases(args.lamp_run, args.output)
    observation_rules = protocol.get('observation_rules', OBSERVATION_RULES)
    protocol.update(model=args.model, cases=cases, model_options=dict(seed=42, temperature=0),
                    decision_policy='retain on match or unknown, exclude on distinct',
                    transfer_note=('newly rendered holdout stories with predeclared labels'
                                   if args.holdout else 'previously used rendered station clips '
                                   'plus mixed and masked controls'),
                    prompts=dict(single_baseline=SINGLE_BASELINE, baseline=VERIFY_INSTRUCTIONS,
                                 counterevidence=INSTRUCTIONS),
                    schemas=dict(baseline=VERIFY_SCHEMA, counterevidence=SCHEMA))
    if args.variant == 'observations':
        protocol.update(
            variant=args.variant, observation_rules=observation_rules,
            observation_prompt=OBSERVATION_PROMPT,
            observation_schemas={count: observation_schema(count) for count in (1, 4)},
            observation_policy='match on any bound target; distinct only if every sampled '
                               'panel binds a conflicting shape and visible part; otherwise retain',
        )
    (args.output / 'protocol.json').write_text(json.dumps(protocol, indent=2) + '\n')
    if args.prepare_only:
        print(f'Prepared {len(cases)} cases')
        return
    with (args.output / 'responses.jsonl').open('w') as log:
        rows = evaluate(cases, args, log, observation_rules)
    report = dict(model_calls=len(rows), metrics=summarize(rows, args.variant))
    result = json.dumps(report, indent=2)
    (args.output / 'report.json').write_text(result + '\n')
    print(result)


if __name__ == '__main__':
    main()
