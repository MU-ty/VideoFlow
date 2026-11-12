"""Run nine prespecified H3 source cases, then four paired policies after source review."""
import argparse
import copy
from functools import partial
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from urllib.error import URLError

import run_egdr_runtime as runtime
from egdr_radio_holdout import POLICIES, TARGETS, ffmpeg, verified_media
from structured_identity import INSTRUCTIONS, SCHEMA, decide, parse_panels
from vision_probe import image_reply

STORIES = {
    'survey-case': dict(seeds=(90501, 90502, 90503), host='a dark gray survey case',
                        part='silver side latch', target='one orange stripe on the case body',
                        excluded='one blue stripe on the case body', markers=(('stripe','orange'), ('stripe','blue')),
                        view='From an oblique side angle, a hand briefly obscures the latch, then moves away.',
                        unrelated='a closed black field notebook'),
    'desk-timer': dict(seeds=(90601, 90602, 90603), host='a dark gray desk timer',
                        part='silver control knob', target='one white hexagonal badge on the timer body',
                        excluded='one white circular badge on the timer body', markers=(('hexagon','white'), ('circle','white')),
                        view='Use a low three-quarter camera angle. Alternate emphasis between the badge and the knob.',
                        unrelated='a single clear empty glass'),
    'greenhouse-sprayer': dict(seeds=(90701, 90702, 90703), host='a forest-green garden sprayer',
                        part='silver spray nozzle', target='two separate black rings around the sprayer neck',
                        excluded='one black ring around the sprayer neck', markers=(('stripe','black'), ('stripe','black')),
                        view='Rotate the sprayer from a rear three-quarter angle to the front. Briefly occlude the neck.',
                        unrelated='a single closed green seed packet', count=True),
}
MODEL = 'qwen2.5vl:7b-ctx8k'
HERE = Path(__file__).resolve().parent


def story_inputs(story, seed):
    spec = STORIES[story]
    framing = 'Keep the marked host and the full part uncropped against a plain studio background.'
    target = f"{spec['host']} with {spec['target']} and a {spec['part']}"
    shots = {
        'S1': f'Front product shot of {target}. {framing}',
        'S2': f"Front product shot of {spec['host']} with {spec['excluded']} and a {spec['part']}. {framing}",
        'S3': f"Product shot of {target}. {framing} {spec['view']}",
        'S4': f"Locked product shot of {spec['unrelated']} alone against a plain studio background.",
    }
    edit = (f"Recolor only the {spec['part']} of {spec['host']} with {spec['target']} to cobalt blue. "
            'Preserve its identity marking, other objects, and the background.')
    case = dict(id=story, entity=target, attribute='color', edit=edit, shots=shots, declared=['S1'])
    question = f'Does any source panel show the {target}, with the marking on the same object as the edited part?'
    return runtime.color_manifest(shots, seed), case, question


def classify(raw, pair, spec):
    if not spec.get('count'):
        return parse_panels(raw, pair)
    reply = json.loads(raw)
    if not isinstance(reply, dict) or not isinstance(reply.get('panels'), list):
        raise ValueError('invalid ring observations')
    counts = {}
    for row in reply['panels']:
        if not isinstance(row, dict) or type(row.get('panel')) is not int or row['panel'] not in (1, 2):
            raise ValueError('invalid ring panel')
        count = row.pop('ring_count', None)
        if type(count) is not int or count not in (0, 1, 2, 3):
            raise ValueError('invalid ring count')
        counts[pair[row['panel'] - 1]] = count
    panels = parse_panels(json.dumps(reply), pair)
    for panel, row in panels.items():
        row['ring_count'] = counts[panel]
    return panels


def verdict(panels, spec, use_location):
    target, excluded = spec['markers']
    # Reuse the same comparison with a canonical location in the advisory arm.
    mapped = {}
    for index, panel in panels.items():
        row = dict(panel)
        if spec.get('count'):
            if row['complete'] and row['shape'] == 'stripe' and row['color'] == 'black':
                row['shape'] = {1: 'one-ring', 2: 'two-rings'}.get(row['ring_count'], 'unknown')
            else:
                row['shape'] = 'unknown'
        if not use_location and row['host_visible'] == 'yes':
            row['location'] = 'body'
        mapped[index] = row
    if spec.get('count'):
        target, excluded = ('two-rings', 'black'), ('one-ring', 'black')
    return decide(mapped, (*target, 'body'), [(*excluded, 'body')])[0]


def refine(spec, server, model, question, panels, output, log):
    baseline = runtime.capture_probe(server, model, question, panels, output, log)
    if baseline['decision'] == 'no':
        return dict(baseline, baseline_decision='no', recheck_calls=0)
    schema = SCHEMA
    prompt = (INSTRUCTIONS + f" Edited part to locate: {spec['part']}. Host type: {spec['host']}. "
              'Use body for the main housing, including its neck. Keep the marker on the same host as the part.')
    if spec.get('count'):
        schema = copy.deepcopy(SCHEMA)
        item = schema['properties']['panels']['items']
        item['properties']['ring_count'] = {'enum': [0, 1, 2, 3]}
        item['required'].append('ring_count')
        prompt += (' Treat a continuous ring as a stripe. Set ring_count to 1 or 2 for that exact '
                   'number of clearly visible black neck rings, 3 for more, or 0 for unclear or absent. Set complete=false whenever any part of the ring-bearing neck is obscured.')
    observed = {}
    calls = 0
    for pair in ((1, 2), (3, 4)):
        raw = None
        error = None
        try:
            with TemporaryDirectory() as tmp:
                sheet = Path(tmp) / 'pair.png'
                ffmpeg('-i', panels[pair[0]-1], '-i', panels[pair[1]-1], '-filter_complex_threads', 1,
                       '-filter_complex', 'hstack=inputs=2', '-frames:v', 1, '-threads', 1, sheet)
                calls += 1
                raw = image_reply(server, model, sheet, prompt, schema)
            observed.update(classify(raw, pair, spec))
        except (ValueError, URLError, TimeoutError) as exc:
            error = str(exc)
        log.write(json.dumps(dict(probe=output.name, stage='structured', pair=pair, raw=raw, error=error)) + '\n')
        log.flush()
    advisory = verdict(observed, spec, use_location=False)
    strict = verdict(observed, spec, use_location=True)
    decision = {'confirmed': 'yes', 'rejected': 'no', 'unknown': baseline['decision']}[advisory]
    return dict(baseline, decision=decision, baseline_decision=baseline['decision'],
                structured_advisory=advisory, structured_strict=strict,
                observations=observed, recheck_calls=calls, calls=baseline['calls'] + calls)


def frozen_protocol():
    return {name: runtime.checksum(HERE / name) for name in (
        Path(__file__).name, 'run_egdr_runtime.py', 'structured_identity.py', 'EGDR_IDENTITY_HOLDOUT.md',
        'vision_probe.py', 'vision_evidence_reuse.py')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('source', 'paired'))
    parser.add_argument('--story', choices=STORIES, required=True)
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--ollama', default='http://127.0.0.1:11434')
    parser.add_argument('--source-run', type=Path)
    parser.add_argument('--annotation', type=Path)
    args = parser.parse_args()
    spec = STORIES[args.story]
    if args.seed not in spec['seeds']:
        parser.error('choose a prespecified seed for the story')
    if not all(shutil.which(command) for command in ('ffmpeg', 'ffprobe')):
        parser.error('ffmpeg and ffprobe are required')
    if args.output.exists():
        parser.error('use a new output directory')
    args.source_only = args.phase == 'source'
    if args.source_only and (args.source_run or args.annotation):
        parser.error('source phase takes no source run or annotation')
    if not args.source_only and (not args.source_run or not args.annotation):
        parser.error('paired phase requires a frozen source run and an eligibility annotation')
    hashes = frozen_protocol()
    extras = dict(holdout_files_sha256=hashes, structured_location_policy='advisory',
                  reference_frame_indices=dict.fromkeys(TARGETS, 0))
    runtime.STORIES[args.story] = partial(story_inputs, args.story)
    args.policies = list(POLICIES)
    args.continuous = not args.source_only
    args.vision_model = MODEL
    args.text_model = 'qwen2.5:14b'
    args.timeout = 1800
    args.poll = 3
    args.frame_notes = args.lamp_reference_frames = args.lunchbox_later_reference = None
    args.oracle_labels = args.reference_regions = None
    if args.source_only:
        runtime.run(args, protocol_extras=extras)
        _, _, files = verified_media(args.output / 'source', 1)
        frames = args.output / 'frame0'
        frames.mkdir()
        for shot, clip in files.items():
            ffmpeg('-i', clip, '-frames:v', 1, '-threads', 1, frames / f'{shot}.png')
        return
    source = json.loads((args.source_run / 'protocol.json').read_text())
    if (source['story'], source['seed'], source.get('holdout_files_sha256')) != (args.story, args.seed, hashes):
        raise ValueError('source run must match the frozen story, seed and code')
    _, assets, _ = verified_media(args.source_run / 'source', 1)
    annotation = json.loads(args.annotation.read_text())
    source_hashes = {shot: row['sha256'] for shot, row in assets.items()}
    if (annotation['source_eligible'] is not True or not annotation['reviewer'].strip()
            or not annotation['notes'].strip() or annotation['source_sha256'] != source_hashes):
        raise ValueError('an eligible human review bound to the source is required')
    outlines = annotation['frame0_polygons']
    if set(outlines) != set(TARGETS) or any(
            len(polygon) < 3 or any(len(point) != 2 or any(type(c) is not int for c in point)
                                   or not (0 <= point[0] < 864 and 0 <= point[1] < 480)
                                   for point in polygon)
            for polygon in outlines.values()):
        raise ValueError('provide S1 and S3 polygons in original 864x480 pixels')
    extras['source_annotation_sha256'] = runtime.checksum(args.annotation)
    with TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        args.oracle_labels = tmp / 'oracle.json'
        args.reference_regions = tmp / 'regions.json'
        runtime.write_json(args.oracle_labels, dict(target_shots=list(TARGETS), source_sha256=source_hashes))
        runtime.write_json(args.reference_regions, {shot: [source_hashes[shot], polygon]
                                                   for shot, polygon in outlines.items()})
        runtime.run(args, probe_capture=partial(refine, spec), protocol_extras=extras,
                    target_prompt=(spec['part'], spec['part'].replace('silver', 'scarlet red'),
                                   'the original host, identity marker, and background'))
    runtime.write_json(args.output / 'source_annotation.json', annotation)


if __name__ == '__main__':
    main()
