"""Prospective paired comparison of direct and structured visual dependency decisions."""
import argparse
from collections import Counter
from contextlib import closing
import csv
from pathlib import Path
import shutil

from replay_gaps_3arm import ACTIONS
from run_online import (
    CANDIDATES, ROOT, acquire, check, dependency_result, live_setup, load_json,
    model_identity, stage_inputs,
)
from run_weak_identity import (
    fixed_prompt, make_blind, read_screen, run_policy, stage_sources, weak_manifest,
)

VISUAL = ('direct-worklist', 'egdr-fixed')
POLICIES = (*VISUAL, 'oracle')


def cases():
    stories = (
        ('amplifier-chain', 92511, 'a silver stereo amplifier on a desk',
         'volume knob', 'blue circle', 'blue hexagon', 'a soft-focus headphone stand',
         'A stack of closed CD cases on a desk with no amplifier.', True),
        ('camera-fanout', 92521, 'a black mirrorless camera on a workbench',
         'top mode dial', 'white triangle', 'white diamond', 'a soft-focus camera strap',
         'A lens cap and microfiber cloth on a workbench with no camera.', False),
        ('scale-chain', 92531, 'a cream analog kitchen scale on a countertop',
         'front zero-adjustment knob', 'yellow circle', 'yellow hexagon', 'a soft-focus measuring cup',
         'A folded tea towel and a spoon on a countertop with no scale.', True),
        ('radio-fanout', 92541, 'a charcoal tabletop radio on a wooden shelf',
         'front tuning knob', 'white circle', 'white diamond', 'a soft-focus mug',
         'Two closed books on a wooden shelf with no radio.', False),
    )
    result = {}
    for name, first, host, part, target, distractor, occluder, independent, chain in stories:
        for index, seed in enumerate((first, first + 1)):
            view = ('Film from a shallow oblique front angle while keeping the control legible.'
                    if index == 0 else
                    f'Slow lateral slide. Start with {occluder} partly covering the decal, '
                    'then reveal the decal while keeping the control visible.')
            result[f'{name}-{seed}'] = dict(
                story=name, seed=seed, host=host, part='{{target.color}} ' + part,
                initial='matte black', target_marker=target, distractor_marker=distractor,
                edges=[['S3', 'S4'], ['S4' if chain else 'S3', 'S6']],
                reference_motion={
                    'S4': f'Slow push in. Preserve the same object, body decal and {part} color.',
                    'S6': f'Hold a slightly closer view. Preserve the body decal and {part} color.',
                },
                independent=independent, weak_view=view,
            )
    return result


def compare_observer(args, api, frozen, verifier, case, revision, journal, policy):
    direct = policy == 'direct-worklist'

    def reply(server, model, path, prompt, schema):
        if direct:
            prompt += '\nApply the relevant items in this fixed checklist:\n'
            prompt += '\n'.join(f'- {instruction}' for instruction in ACTIONS.values())
        else:
            prompt = fixed_prompt(case)
        return api['image_reply'](server, model, path, prompt, schema)

    return acquire(args, dict(api, image_reply=reply), frozen, verifier, case, revision, journal,
                   'naive-vlm' if direct else 'egdr')


def schedule(evidence):
    return {s: [(x['phase'], x['image_sha256']) for x in e['calls']]
            for s, e in evidence['shots'].items()}


def paired_inputs(left, right):
    images = schedule(left)
    check(images == schedule(right), 'visual inputs differ between arms')
    check(set(images) == set(CANDIDATES) and all(len(v) == 3 for v in images.values()),
          'expected three calls per candidate per arm')


def source_archive(args, api, source_report, block, case):
    seed = args.source_run / block
    check(load_json(seed / 'manifest.json') == weak_manifest(case), f'changed manifest: {block}')
    archive = load_json(seed / 'source_media/revision-1.json')
    for row in archive['assets']:
        check(api['checksum'](seed / 'source_media' / row['file']) == row['sha256'],
              f'changed clip: {block}/{row["asset"]}')
    for shot, hashes in source_report[block]['screening_inputs'].items():
        check(api['checksum'](args.source_run / 'screen' / block / f'{shot}.png') ==
              hashes['sheet_sha256'], f'changed screening sheet: {block}/{shot}')
    return archive


def eligibility(args, all_cases, protocol):
    check(load_json(args.source_run / 'protocol.json') == protocol, 'source protocol mismatch')
    source = load_json(args.source_run / 'source-report.json')
    check(set(source) == set(all_cases), 'source run must retain all eight seed records')
    results = read_screen(args.screen, source, all_cases, require_ambiguity=False)
    first_by_story = {}
    for block, row in results.items():
        if row['status'] == 'eligible':
            row['visibility'] = ('partly-readable' if any(row['screening'][s]['panels'] < 4
                                                          for s in CANDIDATES) else 'fully-readable')
            first_by_story.setdefault(all_cases[block]['story'], block)
    return source, results, list(first_by_story.values())


def check_model(args, model):
    check(model_identity(args, model['model']) == model, 'Ollama model changed during comparisons')


def run_selection(args, api, all_cases, protocol, source, results, render_blocks):
    write = api['write_json']
    write(args.output / 'render-blocks.json', render_blocks)
    write(args.output / 'report.json', results)
    if not render_blocks:
        write(args.output / 'summary.json', dict(eligible=0, arms={}))
        return
    model = model_identity(args, protocol['verifier']['model'])
    write(args.output / 'model.json', model)
    for index, (block, case) in enumerate(all_cases.items()):
        row = results[block]
        if row['status'] != 'eligible':
            continue
        archive = source_archive(args, api, source, block, case)
        root = args.output / block
        root.mkdir()
        frozen = root / 'frozen'
        stage_inputs(api, frozen, archive, args.source_run / block / 'source_media')
        observations = {}
        row['arms'] = {}
        row['arm_order'] = list(VISUAL if index % 2 == 0 else VISUAL[::-1])
        for policy in row['arm_order']:
            print(f'{block}: selection {policy}', flush=True)
            e = compare_observer(args, api, frozen, protocol['verifier'], case, 2,
                                 root / f'{policy}.jsonl', policy)
            observations[policy] = e
            write(root / f'{policy}.json', e)
            items = e['shots']
            row['arms'][policy] = dict(
                **dependency_result(items),
                unknown=[s for s, v in items.items() if v['verification'] == 'unknown'],
                errors=sum(v['parse_failures'] + v['request_failures'] for v in items.values()),
                seconds=e['finished_s'] - e['started_s'],
            )
        paired_inputs(*(observations[a] for a in VISUAL))
        row['shared_discovery_decisions'] = all(
            observations[VISUAL[0]]['shots'][s]['first'] == observations[VISUAL[1]]['shots'][s]['first']
            for s in CANDIDATES)
        check_model(args, model)
        row['status'] = 'completed'
        write(args.output / 'report.json', results)
    summary = {}
    for policy in VISUAL:
        rows = [row['arms'][policy] for row in results.values() if row['status'] == 'completed']
        summary[policy] = {k: sum(len(row[k]) for row in rows) for k in ('missed', 'excess', 'unknown')}
        summary[policy]['seeds'] = len(rows)
        summary[policy]['errors'] = sum(row['errors'] for row in rows)
    differences = {}
    for block, row in results.items():
        if row['status'] == 'completed':
            direct, structured = (row['arms'][policy] for policy in VISUAL)
            differences[block] = {k: len(structured[k]) - len(direct[k])
                                  for k in ('missed', 'excess', 'unknown')}
    aggregate = dict(eligible=len(differences), arms=summary,
                     paired_deltas_structured_minus_direct=differences)
    write(args.output / 'summary.json', aggregate)
    print(aggregate, flush=True)


def run_edits(args, api, all_cases, protocol, source, results, render_blocks):
    write = api['write_json']
    selection = args.selection_run
    check(load_json(selection / 'protocol.json') == protocol, 'selection protocol mismatch')
    check((selection / 'locked-source-screen.csv').read_bytes() == args.screen.read_bytes(),
          'source-screen CSV differs from the one locked for selection')
    check(load_json(selection / 'render-blocks.json') == render_blocks, 'render subset changed')
    selected = load_json(selection / 'report.json')
    check(all(selected[b]['status'] == 'completed' for b, v in results.items()
              if v['status'] == 'eligible'), 'complete all eligible selection arms before rendering')
    model = load_json(selection / 'model.json')
    check_model(args, model)
    write(args.output / 'model.json', model)
    backend = api['ComfyUI'](args.server)
    for block, row in results.items():
        if row['status'] == 'eligible' and block not in render_blocks:
            row['status'] = 'selection-only'
    write(args.output / 'report.json', results)
    for index, block in enumerate(render_blocks):
        case, row = all_cases[block], results[block]
        archive = source_archive(args, api, source, block, case)
        root = args.output / block
        root.mkdir()
        shutil.copytree(args.source_run / block / 'source_media', root / 'source_media')
        write(root / 'story.json', case)
        offset = index % len(POLICIES)
        order = POLICIES[offset:] + POLICIES[:offset]
        row.update(status='running', policies=[], arm_order=list(order))
        for policy in order:
            print(f'{block}: rendering {policy}', flush=True)
            row['active_policy'] = policy
            write(args.output / 'report.json', results)
            try:
                api['check_backend'](backend)
                refs = api['ReferenceFrames'](root / f'{policy}-references', backend)
                observer = compare_observer if policy in VISUAL else None
                arm = run_policy(args, api, root / policy, args.source_run / block / 'source.db',
                                 archive, case, protocol['verifier'], refs, backend, policy,
                                 observer=observer)
                if observer is not None:
                    check(arm['vlm_calls'] == 6, 'unequal visual budget')
                    rendered = load_json(root / policy / 'evidence.json')
                    acquired = load_json(selection / block / f'{policy}.json')
                    paired_inputs(rendered, acquired)
                    arm['selected_shots_agree_with_selection_phase'] = (
                        arm['dependency_discovery']['selected'] ==
                        selected[block]['arms'][policy]['selected'])
                    check_model(args, model)
                else:
                    first = arm['first_publication']
                    check(first['includes_all_targets'] and not first['excess'], 'oracle closure mismatch')
                write(root / policy / 'report.json', arm)
            except (OSError, RuntimeError, ValueError) as exc:
                arm = dict(policy=policy, status='failed', error=str(exc))
                print(f'{block}/{policy}: {exc}', flush=True)
            row['policies'].append(arm)
            write(args.output / 'report.json', results)
        row.pop('active_policy')
        row['status'] = ('completed' if all(a['status'] == 'completed' for a in row['policies'])
                         else 'partial')
        write(args.output / 'report.json', results)


def score_ratings(args, api):
    key = load_json(args.source_run / 'KEEP_PRIVATE_key.json')
    fields = ('strict_recolor', 'identity_preserved', 'temporal_consistency', 'unintended_change')
    parsed = []
    for path in args.ratings:
        scores = {}
        with path.open(encoding='utf-8-sig', newline='') as f:
            for row in csv.DictReader(f):
                clip = row['clip_id'].strip()
                check(clip in key and clip not in scores, f'invalid or duplicate clip: {clip}')
                values = tuple(row[k].strip().lower() for k in fields)
                for field, value in zip(fields, values):
                    expected = {'na'} if field == 'strict_recolor' and not key[clip]['target'] else {
                        'yes', 'no', 'unclear'}
                    check(value in expected, f'{clip}/{field}: missing or invalid rating')
                scores[clip] = values
        check(set(scores) == set(key), f'missing ratings in {path.name}')
        parsed.append(scores)

    def totals(scores):
        result = {}
        for clip, meta in key.items():
            row = result.setdefault(meta['block'], {}).setdefault(meta['policy'],
                                                                  {k: Counter() for k in fields})
            for field, value in zip(fields, scores[clip]):
                row[field][value] += 1
        return result

    a, b = parsed
    agreement = {}
    for i, field in enumerate(fields):
        applicable = [clip for clip in key if a[clip][i] != 'na']
        agreement[field] = dict(agreed=sum(a[c][i] == b[c][i] for c in applicable),
                                total=len(applicable),
                                disagreements=[c for c in applicable if a[c][i] != b[c][i]])
    api['write_json'](args.output / 'ratings-summary.json',
                      dict(reviewers=[totals(a), totals(b)], agreement=agreement,
                           identical_input_files=args.ratings[0].read_bytes() == args.ratings[1].read_bytes()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', required=True,
                        choices=('prepare', 'source', 'select', 'edit', 'blind', 'score'))
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--source-run', type=Path)
    parser.add_argument('--screen', type=Path)
    parser.add_argument('--selection-run', type=Path)
    parser.add_argument('--ratings', nargs=2, type=Path)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--ollama', default='http://127.0.0.1:11434')
    parser.add_argument('--timeout', type=float, default=2400)
    parser.add_argument('--poll', type=float, default=2)
    args = parser.parse_args()
    if args.mode in ('select', 'edit') and not (args.source_run and args.screen):
        parser.error('select/edit requires --source-run and --screen')
    if args.mode == 'edit' and args.selection_run is None:
        parser.error('edit requires --selection-run')
    if args.mode in ('blind', 'score') and args.source_run is None:
        parser.error('blind/score requires --source-run')
    if args.mode == 'score' and not args.ratings:
        parser.error('score requires two --ratings files')
    if args.timeout <= 0 or args.poll <= 0:
        parser.error('timeout and poll must be positive')
    args.server = args.server.rstrip('/')
    args.ollama = args.ollama.rstrip('/')
    if args.mode in ('source', 'select', 'edit', 'blind'):
        check(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg and ffprobe are required')
    api = live_setup(ROOT / 'runtime')
    args.output.mkdir(parents=True)
    if args.mode == 'blind':
        make_blind(args, api)
        return
    if args.mode == 'score':
        score_ratings(args, api)
        return
    all_cases = cases()
    protocol = dict(cases=all_cases, verifier=load_json(ROOT / 'verifier_protocol.json'),
                    eligibility='at least one readable linked panel in S1, S2, and S3',
                    visibility='partly-readable when S2 or S3 has fewer than four readable panels',
                    render_subset='first eligible seed per story, frozen before calling the model',
                    ground_truth=dict(S2='distinct', S3='match', targets=['S1', 'S3', 'S4', 'S6']),
                    edit='scarlet red', frame_fractions=[0.10, 0.35, 0.60, 0.85],
                    calls_per_candidate=3, visual_policies=list(VISUAL), edit_policies=list(POLICIES),
                    recheck_instructions=ACTIONS)
    api['write_json'](args.output / 'protocol.json', protocol)
    if args.mode == 'prepare':
        for block, case in all_cases.items():
            manifest = weak_manifest(case)
            with closing(api['AssetRuntime'](':memory:')) as runtime:
                runtime.initialize(manifest)
            api['write_json'](args.output / f'{block}.json', manifest)
        print(f'{len(all_cases)} frozen manifests validated.')
    elif args.mode == 'source':
        backend = api['ComfyUI'](args.server)
        api['check_backend'](backend)
        stage_sources(args, api, all_cases, backend)
    else:
        source, results, render_blocks = eligibility(args, all_cases, protocol)
        shutil.copyfile(args.screen, args.output / 'locked-source-screen.csv')
        if args.mode == 'select':
            run_selection(args, api, all_cases, protocol, source, results, render_blocks)
        elif not render_blocks:
            api['write_json'](args.output / 'report.json', results)
            print('No eligible source seeds. See report.json.')
        else:
            run_edits(args, api, all_cases, protocol, source, results, render_blocks)


if __name__ == '__main__':
    main()
