"""Execute the primary Round 930 pair from a frozen response bank and Rev.1."""

import argparse
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
from statistics import median
import sys
import time

from active_budget import FRACTIONS, closure, eligibility, load, observations, replay, save, sha

NATURAL = Path(__file__).resolve().parent.parent / 'VideoFlow_Natural_Gate'
ARMS = ('fixed', 'uncertainty')
DECISIONS = {'confirmed': 'match', 'distinct': 'distinct', 'unknown': 'fallback'}


def matched_inputs(args):
    protocol = load(args.protocol)
    source_path = args.source_run / 'source-report.json'
    source = load(source_path)
    if source.keys() != protocol['cases'].keys() or load(args.source_run / 'protocol.json') != protocol:
        raise ValueError('source run and protocol differ')
    included = eligibility(args, protocol, source)
    metadata = load(args.bank / 'metadata.json')
    if (metadata['protocol_sha256'], metadata['source_report_sha256'],
        metadata['screen_sha256'], metadata['model']['model']) != (
            sha(args.protocol), sha(source_path), sha(args.screen), protocol['model']):
        raise ValueError('verification bank provenance mismatch')
    report = load(args.bank / 'report.json')
    frozen = report['source_eligible']['seeds']
    if report['design'] != 'shared-response-bank-replay' or set(frozen) != included:
        raise ValueError('bank and source screen disagree')
    if not included:
        raise ValueError('no eligible paired sources')
    budget = protocol['primary_budget']
    matches = {}
    for block in sorted(included):
        case = protocol['cases'][block]
        root = args.source_run / block
        if not (root / 'source.db').is_file():
            raise ValueError(f'missing Rev.1 database: {block}')
        archive = load(root / 'source_media' / 'revision-1.json')
        origin = {entry['asset']: entry for entry in archive['assets']}
        if archive['revision'] != 1 or set(origin) != {f'S{i}' for i in range(1, 7)}:
            raise ValueError(f'incomplete Rev.1 archive: {block}')
        for shot, entry in origin.items():
            if Path(entry['file']).name != entry['file'] or sha(root / 'source_media' / entry['file']) != entry['sha256']:
                raise ValueError(f'source bytes mismatch: {block}/{shot}')
        bank = {}
        for shot in case['candidates']:
            bank[shot] = []
            for index in range(1, len(FRACTIONS) + 1):
                path = args.bank / block / f'{shot}-{index}.json'
                row = load(path)
                votes = ['unknown', 'unknown'] if row.get('error') else observations(
                    row['raw'], protocol['schema'], case)
                if (row['votes'] != votes or row['prompt'] != case['prompt']
                        or row['clip_sha256'] != origin[shot]['sha256']
                        or row['image_sha256'] != sha(path.with_suffix('.png'))):
                    raise ValueError(f'bank reply provenance mismatch: {path}')
                bank[shot].append(row)
        plans = {}
        for arm in ARMS:
            plan = replay(case, bank, arm, budget, case['recheck_order'])
            if plan != frozen[block][str(budget)][arm]:
                raise ValueError(f'bank replay mismatch: {block}/{arm}')
            plans[arm] = dict(plan, evidence=[
                dict(shot=shot, view=index, **bank[shot][index - 1])
                for shot, index in plan['trace']])
        matches[block] = (case, root, origin, plans)
    return budget, matches


def render_arm(args, api, backend, directory, source, origin, case, evidence, key, log):
    directory.mkdir(parents=True)
    archive_dir = directory / 'media'
    shutil.copytree(source / 'source_media', archive_dir)
    journal = directory / 'events.jsonl'
    db = directory / 'project.db'
    source_uri = (source / 'source.db').resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(source_uri, uri=True)) as original, closing(sqlite3.connect(db)) as fork:
        original.backup(fork)
    refs = api['ReferenceFrames'](directory / 'references', backend)
    save(directory / 'evidence.json', evidence)
    with closing(api['AssetRuntime'](db)) as runtime:
        initial = runtime.snapshot()
        fields = ('revision', 'input_hash', 'output')
        if (initial['revision'] != 1 or initial['timeline'] is None or
                {a['asset']: tuple(a[k] for k in fields) for a in initial['timeline']} !=
                {shot: tuple(a[k] for k in fields) for shot, a in origin.items()}):
            raise ValueError('Rev.1 database does not match the archive')
        candidates = case['candidates']
        runtime.edit(dict(id='budget-930', expected_revision=1, target='S1',
                          key=key, value='scarlet red', evidence_candidates=candidates))
        for shot in candidates:
            votes = [row for row in evidence['evidence'] if row['shot'] == shot]
            source_hash = origin[shot]['input_hash']
            decision = DECISIONS[evidence['states'][shot]]
            proof = api['fingerprint'](dict(revision=2, shot=shot, key=key,
                source_input_hash=source_hash, source_clip_sha256=origin[shot]['sha256'],
                decision=decision, votes=votes))
            result = runtime.resolve_evidence(2, shot, source_hash, proof, decision)
            log(journal, 'evidence_applied', **result)

        accepted = 0
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            events = api['pass_once'](runtime, backend, refs.build, max_submit=0)
            free = max(0, 2 - len(runtime.unresolved_remote()))
            if free:
                for job in runtime.pending()[:free]:
                    for dep in runtime._inputs(job['asset'])['dependencies']:
                        if dep['kind'] == 'reference':
                            refs.stage(dep['output'])
                events += api['pass_once'](api['SubmissionView'](runtime), backend, refs.build, max_submit=free)
            accepted += sum(event['action'] == 'submitted' for event in events)
            log(journal, 'worker', events=events)
            if any(event['action'] in ('failed', 'rejected', 'validation_error') for event in events):
                raise RuntimeError(f'worker failure, see {journal}')
            snapshot = runtime.snapshot()
            if snapshot['timeline'] and not runtime.unresolved_remote():
                break
            time.sleep(args.poll)
        else:
            raise TimeoutError(f'edit did not publish, see {journal}')

        elapsed = runtime.db.execute(
            'SELECT t.published_at-e.applied_at FROM vf_timelines t '
            'JOIN vf_events e ON e.revision=t.revision WHERE t.revision=2').fetchone()[0]
        final = api['archive_current'](runtime, archive_dir, backend.base, probe=True)
        current = {entry['asset']: entry for entry in final['assets']}
        rendered = {shot for shot in current if current[shot]['input_hash'] != origin[shot]['input_hash']}
        expected = closure({'S1', *case['expected']}, case['edges'])
        if rendered != closure({'S1', *evidence['selected']}, case['edges']):
            raise RuntimeError('published updates differ from the chosen closure')
        if any(current[s]['sha256'] != origin[s]['sha256'] for s in current.keys() - rendered):
            raise RuntimeError('unexpected change to reused media bytes')
        edit_jobs = [j for j in snapshot['jobs'] if j['revision'] == 2]
        edit_ids = {j['id'] for j in edit_jobs}
        inspections = [j for j in api['summarize_inspections'](runtime)['jobs'] if j['job'] in edit_ids]
        lineage = api['reference_evidence'](
            [current], {(j['asset'], j['revision']): j for j in snapshot['jobs']}, refs,
            [(parent, child) for parent, child in case['edges'] if child in rendered])
        return dict(expected=sorted(expected), regenerated=sorted(rendered),
                    missed=sorted(expected - rendered), excess=sorted(rendered - expected),
                    first_publication_covers_required=expected <= rendered,
                    first_publication_exact=expected == rendered,
                    edit_to_first_publication_s=elapsed, acknowledged_submissions=accepted,
                    completed_edit_jobs=sum(j['status'] == 'succeeded' for j in edit_jobs),
                    jobs_with_provider_history=sum(j['history_seen'] for j in inspections),
                    cache_sampler_jobs=sum('10' in j['cached_nodes'] for j in inspections),
                    provider_inspections=inspections, lineage=lineage, gpu_seconds=None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('protocol', 'source-run', 'screen', 'bank', 'output'):
        parser.add_argument('--' + flag, required=True, type=Path)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--timeout', type=float, default=2400)
    parser.add_argument('--poll', type=float, default=2)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if args.timeout <= 0 or args.poll <= 0:
        parser.error('timeout and poll must be positive')
    budget, inputs = matched_inputs(args)
    args.output.mkdir(parents=True)
    order = {block: list(ARMS if i % 2 == 0 else ARMS[::-1])
             for i, block in enumerate(inputs)}
    result = dict(budget=budget, execution='frozen-evidence', arm_order=order,
                  protocol_sha256=sha(args.protocol), bank_report_sha256=sha(args.bank / 'report.json'),
                  blocks={}, failures={})
    save(args.output / 'report.json', result)
    if args.prepare_only:
        print(f'Validated {len(inputs)} paired sources and their frozen decisions')
        return
    sys.path.insert(0, str(NATURAL))
    from run_joint import KEY, live_setup, log
    api = live_setup(NATURAL / 'runtime')
    backend = api['ComfyUI'](args.server.rstrip('/'))
    for block, (case, source, origin, plans) in inputs.items():
        result['blocks'][block] = {}
        for arm in order[block]:
            run = args.output / block / arm
            try:
                api['check_backend'](backend)
                print(f'{block}: {arm}', flush=True)
                row = render_arm(args, api, backend, run, source, origin, case, plans[arm], KEY, log)
                save(run / 'summary.json', row)
                result['blocks'][block][arm] = row
            except (OSError, RuntimeError, ValueError) as exc:
                result['failures'][f'{block}/{arm}'] = str(exc)
                save(args.output / 'report.json', result)
                raise
            save(args.output / 'report.json', result)
    pairs = list(result['blocks'].values())
    result['total_completed_edit_jobs_saved'] = sum(
        p['fixed']['completed_edit_jobs'] - p['uncertainty']['completed_edit_jobs'] for p in pairs)
    result['paired_median_publication_difference_s'] = median(
        p['fixed']['edit_to_first_publication_s'] - p['uncertainty']['edit_to_first_publication_s']
        for p in pairs)
    save(args.output / 'report.json', result)


if __name__ == '__main__':
    main()
