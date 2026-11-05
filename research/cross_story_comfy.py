"""Frozen two-story, two-seed paired ComfyUI evaluation."""
import argparse
from copy import deepcopy
from pathlib import Path

from video_edit_ablation import ComfyUI, asset, run_comfy_trial, write_json


SEEDS = (3107, 7201)
POLICIES = ('videoflow', 'strong_baseline')


def story(assets, state_pairs, key, shared_value, local, local_key, local_value, review):
    edits = [
        dict(id='shared-edit', expected_revision=1, target='S1', key=key, value=shared_value),
        dict(id='local-edit', expected_revision=2, target=local, key=local_key, value=local_value),
    ]
    manifest = dict(
        assets=assets,
        edges=[
            *[dict(parent=a, child=b, kind='state', keys=[key]) for a, b in state_pairs],
            dict(parent='S1', child='S4', kind='order'),
        ],
    )
    return dict(
        manifest=manifest, edits=edits,
        expected={'shared-edit': ['S1', 'S2', 'S3'], 'local-edit': [local]},
        review=review,
    )


STORIES = {
    'bicycle_workshop_chain': story(
        [
            asset('S1', 'Workshop wide shot of a bicycle with a visibly {{frame.color}} '
                  'frame, mounted on a repair stand in neutral daylight.',
                  {'frame.color': 'yellow'}, 0),
            asset('S2', 'Workshop side close-up of hands tightening a bolt on the '
                  '{{frame.color}} bicycle frame, with the painted tube clearly visible.',
                  {'frame.color': 'yellow'}, 0),
            asset('S3', 'Slow side tracking shot of the finished bicycle at a doorway. '
                  'The {{frame.color}} frame and {{basket.color}} front basket are clearly '
                  'visible under neutral daylight.',
                  {'frame.color': 'yellow', 'basket.color': 'white'}, 0),
            asset('S4', 'Static overhead insert of metal wrenches on a clean workbench. '
                  'No bicycle is visible.', {}, 0),
        ],
        [('S1', 'S2'), ('S2', 'S3')], 'frame.color', 'red', 'S3', 'basket.color', 'black',
        {'S1': 'Red bicycle frame.', 'S2': 'Red bicycle frame.',
         'S3': 'Red bicycle frame and black front basket.',
         'S4': 'Original workshop tools clip retained byte-for-byte.'},
    ),
    'coffee_cart_fanout': story(
        [
            asset('S1', 'Medium shot of a barista placing a {{cup.color}} ceramic mug '
                  'on an outdoor coffee cart counter in neutral daylight.',
                  {'cup.color': 'white'}, 0),
            asset('S2', 'Close side shot of the {{cup.color}} ceramic mug on the counter '
                  'with a clearly visible {{coaster.color}} coaster directly beneath it.',
                  {'cup.color': 'white', 'coaster.color': 'black'}, 0),
            asset('S3', 'Overhead shot of coffee being poured into a {{cup.color}} '
                  'ceramic mug. Keep the colored rim and handle visible.',
                  {'cup.color': 'white'}, 0),
            asset('S4', 'Static cutaway to a metal grinder and coffee beans. '
                  'No mug or coaster is visible.', {}, 0),
        ],
        [('S1', 'S2'), ('S1', 'S3')], 'cup.color', 'bright green', 'S2', 'coaster.color', 'yellow',
        {'S1': 'Bright green mug.', 'S2': 'Bright green mug on a yellow coaster.',
         'S3': 'Bright green mug.', 'S4': 'Original grinder clip retained byte-for-byte.'},
    ),
}


def make_pairs():
    for story_index, (name, spec) in enumerate(STORIES.items()):
        for seed_index, seed in enumerate(SEEDS):
            manifest = deepcopy(spec['manifest'])
            for shot_index, shot in enumerate(manifest['assets']):
                shot['spec']['config']['seed'] = seed + shot_index
            order = POLICIES if (story_index + seed_index) % 2 == 0 else POLICIES[::-1]
            yield dict(story=name, seed=seed, order=order, **dict(spec, manifest=manifest))


def run_matrix(output, backend, timeout, poll):
    pairs = list(make_pairs())
    output.mkdir(parents=True)
    write_json(output / 'protocol.json', dict(
        pairs=pairs, timeout_seconds=timeout, poll_seconds=poll,
        trigger='Apply shared edit after revision 1 publishes. Apply local edit when its '
                'revision 2 job is observed in queue_running, then reopen the SQLite connection.',
        max_inflight=2,
        timing='Descriptive only. The paired order is balanced, with no explicit warm-up.',
        primary_checks=['missed/excess shots', 'missing/extra accepted jobs',
                        'stale publication', 'byte-identical reuse', 'unresolved jobs'],
        visual_rating='Independent reviewer, blinded to policy, views the final S1–S3 clips '
                      'in shuffled order and records pass/fail/uncertain against the '
                      'per-shot criteria. S4 is verified by artifact hash.',
    ))
    report = dict(complete=False, expected_pairs=len(pairs),
                  evidence='remote_comfyui_unvalidated_quality', rows=[])
    write_json(output / 'report.json', report)
    for pair in pairs:
        trace = {key: pair[key] for key in ('manifest', 'edits', 'expected')}
        for policy in pair['order']:
            label = f"{pair['story']}-{pair['seed']}-{policy}"
            print(label, flush=True)
            try:
                row = run_comfy_trial(output / label, backend, policy, timeout, poll, **trace)
            except Exception as exc:
                report['error'] = dict(trial=label, detail=str(exc))
                write_json(output / 'report.json', report)
                raise
            report['rows'].append(dict(row, story=pair['story'], seed=pair['seed']))
            write_json(output / 'report.json', report)
    report['complete'] = True
    report['runtime_checks_passed'] = all(row['runtime_checks_passed'] for row in report['rows'])
    write_json(output / 'report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--timeout', type=float, default=3600, help='seconds per trial')
    parser.add_argument('--poll', type=float, default=2)
    args = parser.parse_args()
    if min(args.timeout, args.poll) <= 0:
        parser.error('timeout and poll must be positive')
    backend = ComfyUI(args.server)
    backend.environment()
    report = run_matrix(args.output, backend, args.timeout, args.poll)
    if not report['runtime_checks_passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
