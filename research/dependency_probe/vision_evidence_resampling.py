"""Evaluate visual witness reuse under decoded-frame resampling on two new stories."""
import argparse
import json
from pathlib import Path
from zipfile import ZipFile

from vision_evidence_reuse import FIXTURES, compare, capture_probe, digest, ffmpeg

BUNDLE = FIXTURES / 'egdr_evidence_resampling_v1.zip'
FRAMES = 48
FRACTIONS = (10, 35, 60, 85)
TRIMS = {'trim_h2_t0': (2, 48), 'trim_h4_t1': (4, 47),
         'trim_h1_t1': (1, 47), 'trim_h3_t4': (3, 44)}
SWEEP = {(head, tail): (head, FRAMES - tail)
         for head in range(5) for tail in range(5) if head or tail}
STORIES = {
    'station_intercom': (
        'In any one panel, is the round red pushbutton mounted on the navy station intercom '
        'with an ivory triangular badge visible? A plus-shaped badge marks a different intercom.'
    ),
    'weather_beacon': (
        'In any one panel, is the yellow rectangular indicator mounted on the mint-green weather '
        'beacon with exactly three black round dots above the indicator visible? A beacon with two dots does not count.'
    ),
}
SHOTS = ('S1', 'S2', 'S3')
GOLD = ('S1', 'S2')  # By construction, S3 is a marked lookalike in both stories.


def grid(start, end):
    """Four evenly spaced quantiles on the inclusive frame-index extent."""
    return tuple(start + ((end - start - 1) * f + 50) // 100 for f in FRACTIONS)


def matched_controls(witness_index):
    """Keep one old index in each arm and use the same three newly sampled indices."""
    initial = grid(0, FRAMES)
    replacement = next(index for index in initial if index != witness_index)
    shared_new = grid(0, 44)[:3]
    return (tuple(sorted((*shared_new, witness_index))), tuple(sorted((*shared_new, replacement))))


def decode(bundle, root, story):
    shots = {}
    for shot in SHOTS:
        data = bundle.read(f'{story}/{shot}.mp4')
        target = root / story / shot
        target.mkdir(parents=True)
        clip = target / 'source.mp4'
        clip.write_bytes(data)
        ffmpeg('-i', clip, '-vf', 'scale=768:432:flags=bicubic,setsar=1', '-fps_mode',
               'passthrough', '-start_number', 0, '-threads', 1, target / 'frame-%03d.png')
        paths = sorted(target.glob('frame-*.png'))
        if len(paths) != FRAMES:
            raise ValueError(f'{story}/{shot}: expected {FRAMES} decoded frames')
        shots[shot] = dict(paths=paths, hashes=[digest(p.read_bytes()) for p in paths],
                           source_sha256=digest(data))
    return shots


def capture(story, condition, selections, decoded, root, args, log):
    probes = {}
    for shot, indices in selections.items():
        probe = capture_probe(args.server, args.model, STORIES[story],
                              tuple(decoded[shot]['paths'][i] for i in indices),
                              root / f'{story}-{condition}-{shot}', log)
        probes[shot] = dict(probe, frame_indices=list(indices))
    return dict(story=story, id=f'{story}-{condition}', condition=condition, question=STORIES[story],
                gold=list(GOLD), probes=probes)


def control_indices(initial):
    retain, remove = {}, {}
    for shot, probe in initial['probes'].items():
        # On unverified shots use a fixed nominal frame. Such shots remain in
        # selection scoring and outside the witness-survival denominator.
        panel = probe['panels'].index(probe['witness']) if probe['witness'] is not None else 0
        retain[shot], remove[shot] = matched_controls(probe['frame_indices'][panel])
    return retain, remove


def evaluate(captures):
    rows, availability = [], []
    for initial, edits in captures:
        for edit in edits:
            # Every resampling branches from the same original capture. An
            # alternate condition cannot prime the cache for a different one.
            result = compare([initial, edit], evidence='live_paired_vision_resampling')
            rows.extend(row for row in result['rows'] if row['edit'] == edit['id'])
            for shot, probe in initial['probes'].items():
                witness = probe['witness']
                if witness is None:
                    continue
                after = edit['probes'][shot]
                availability.append(dict(story=edit['story'], condition=edit['condition'], shot=shot,
                                         original_witness=probe['frame_indices'][probe['panels'].index(witness)],
                                         resampled_indices=after['frame_indices'],
                                         gold_target=shot in edit['gold'],
                                         survived=witness in after['panels'],
                                         witness_sha256=witness))
    totals = {}
    for row in rows:
        key = (row['story'], row['condition'], row['policy'])
        totals[key] = {metric: len(row[metric]) for metric in ('missed', 'excess', 'unresolved', 'cache_hits')}
        totals[key]['model_calls'] = row['model_calls']
    natural = [row for row in availability if row['condition'] in TRIMS]
    return dict(rows=rows, by_story_condition=[dict(story=s, condition=c, policy=p, **v)
                                               for (s, c, p), v in totals.items()],
                witness_availability=availability,
                natural_witness_survival=dict(eligible=len(natural),
                                             survived=sum(row['survived'] for row in natural)),
                initial_capture_model_calls=sum(probe['calls'] for initial, _ in captures
                                                for probe in initial['probes'].values()))


def preflight(decoded):
    original = grid(0, FRAMES)
    report = {}
    for shot, data in decoded.items():
        hashes = data['hashes']
        def overlap(window):
            indices = grid(*window)
            sampled = {hashes[i] for i in indices}
            return dict(indices=indices, shared_original_indices=[i for i in original if hashes[i] in sampled])

        report[shot] = dict(source_sha256=data['source_sha256'], decoded_frames=len(hashes),
                            distinct_frame_hashes=len(set(hashes)), original_indices=original,
                            natural={name: overlap(window) for name, window in TRIMS.items()},
                            geometry_only_sweep={f'h{h}_t{t}': overlap(window)
                                                 for (h, t), window in SWEEP.items()})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--story', choices=STORIES, action='append')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--server', default='http://127.0.0.1:11434')
    parser.add_argument('--model', default='qwen2.5vl:7b-ctx8k')
    args = parser.parse_args()
    stories = tuple(dict.fromkeys(args.story or STORIES))
    args.output.mkdir(parents=True)
    sampling, captures = {}, []
    with ZipFile(BUNDLE) as bundle, (args.output / 'responses.jsonl').open('w', encoding='utf-8') as log:
        for story in stories:
            decoded = decode(bundle, args.output / 'decoded', story)
            sampling[story] = preflight(decoded)
            if args.prepare_only:
                continue

            def run(condition, selections):
                print(f'{story} {condition}', flush=True)
                return capture(story, condition, selections, decoded, args.output, args, log)

            initial = run('original', dict.fromkeys(SHOTS, grid(0, FRAMES)))
            natural = [run(name, dict.fromkeys(SHOTS, grid(*window))) for name, window in TRIMS.items()]
            retain, remove = control_indices(initial)
            controls = [run('retain_witness', retain), run('remove_witness', remove)]
            captures.append((initial, [*natural, *controls]))
    (args.output / 'sampling.json').write_text(json.dumps(sampling, indent=2) + '\n', encoding='utf-8')
    if captures:
        report = evaluate(captures)
        geometry = []
        for initial, _ in captures:
            story = initial['story']
            for shot, probe in initial['probes'].items():
                if probe['witness'] is None:
                    continue
                index = probe['frame_indices'][probe['panels'].index(probe['witness'])]
                sweep = sampling[story][shot]['geometry_only_sweep']
                geometry.append(dict(story=story, shot=shot, original_witness=index,
                                     gold_target=shot in initial['gold'], windows=len(sweep),
                                     survived=sum(index in cell['shared_original_indices']
                                                  for cell in sweep.values())))
        report.update(evidence='live_paired_vision_resampling', vision_model=args.model, sampling=sampling,
                      geometry_only_witness_sweep=geometry,
                      initial_captures=[initial for initial, _ in captures],
                      resampled_captures=[edit for _, edits in captures for edit in edits],
                      raw_model_calls=sum(p['calls'] for initial, edits in captures
                                          for edit in [initial, *edits] for p in edit['probes'].values()))
        (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report['natural_witness_survival'], indent=2))


if __name__ == '__main__':
    main()
