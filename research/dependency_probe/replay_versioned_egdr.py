"""Compare versioned evidence policies in a deterministic CPU event replay."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from asset_runtime import fingerprint
from planning import execution_plan

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
POLICIES = ('wait_worklist', 'online_worklist', 'egdr_delta')


def source_digests(protocol, fixture_dir):
    # Read the clip bytes used by the earlier press-identity control.
    archive = protocol['source']
    with ZipFile(fixture_dir / archive) as bundle:
        folder = Path(archive).stem
        return {shot: hashlib.sha256(bundle.read(f'{folder}/clips/{shot}.mp4')).hexdigest()
                for shot in protocol['source_shots']}


def replay(protocol, trace, policy, sources):
    shots, hard = protocol['shots'], protocol['edges']
    scope = protocol['scope']
    anchor, key = scope['target'], scope['key']
    story = dict(scenes=[dict(id=shot, cost=protocol['render_ticks'][shot]) for shot in shots],
                 edges=hard)
    initial = {shot: sources[shot] if shot in sources else fingerprint(dict(synthetic=shot))
               for shot in shots}
    committed = initial.copy()
    desired = initial.copy()
    selected, verdicts, jobs, events, publications = set(), {}, [], [], []
    edits = {row['revision']: row for row in trace['edits']}
    revision = 1

    def closure(target, edges, method):
        graph = dict(story, edges=edges)
        return set(execution_plan(graph, dict(target=target, key=key), method).regenerate)

    def refresh():
        # This fixture has one edited key and explicit reference edges. A soft
        # edge propagates the edit value, while reference-only consumers bind
        # the current upstream render identity.
        changed_state = {anchor} | {shot for shot, verdict in verdicts.items() if verdict == 'match'}
        for shot in shots:
            if shot not in selected:
                continue
            references = [(edge['parent'], desired[edge['parent']]) for edge in hard
                          if edge['kind'] == 'reference' and edge['child'] == shot]
            desired[shot] = fingerprint(dict(shot=shot, source=initial[shot], scope=scope,
                                             value=edits[revision]['value'] if shot in changed_state else None,
                                             references=references))

    def plan_from_evidence():
        soft = [dict(parent=anchor, child=shot, kind='state', keys=[key])
                for shot in shots if verdicts.get(shot) == 'match']
        return closure(anchor, hard + soft, 'attr_dag')

    for tick in range(protocol['horizon'] + 1):
        for edit in trace['edits']:
            if edit['tick'] != tick:
                continue
            revision = edit['revision']
            verdicts.clear()
            desired = initial.copy()
            selected = closure(anchor, hard, 'dair') if policy == 'egdr_delta' else plan_from_evidence()
            refresh()
            events.append(dict(tick=tick, action='edit', revision=revision, selected=sorted(selected)))

        for item in trace['evidence']:
            if item['tick'] != tick:
                continue
            shot, decision = item['shot'], item['decision']
            source = sources[item['source_shot']]
            requested = edits[item['revision']]
            request_fingerprint = fingerprint(dict(scope=scope, revision=requested['revision'],
                                                   value=requested['value'], shot=shot, source=source))
            row = dict(tick=tick, action='evidence', revision=item['revision'], shot=shot,
                       decision=decision, source_sha256=source, request_fingerprint=request_fingerprint)
            if item['revision'] != revision:
                row['result'] = 'stale_revision'
            elif shot not in edits[revision]['candidates'] or source != sources.get(shot):
                row['result'] = 'source_or_scope_mismatch'
            elif decision == 'unknown':
                row['result'] = 'unresolved'
            elif shot in verdicts:
                row['result'] = 'duplicate' if decision == verdicts[shot] else 'conflicting_verdict'
            else:
                if decision not in ('match', 'distinct'):
                    raise ValueError('unknown evidence decision')
                verdicts[shot] = decision
                previous = selected.copy()
                if policy != 'egdr_delta':
                    selected = plan_from_evidence()
                elif decision == 'match':
                    selected |= closure(shot, hard, 'dair')
                refresh()
                row.update(result='accepted', added=sorted(selected - previous))
            events.append(row)

        for job in jobs:
            if job['done_tick'] != tick:
                continue
            shot = job['shot']
            if desired[shot] == job['fingerprint']:
                committed[shot] = job['fingerprint']
                job['result'] = 'applied'
            else:
                job['result'] = 'stale'
            events.append(dict(tick=tick, action='complete', shot=shot,
                               submitted_revision=job['revision'], result=job['result']))

        if revision == 1:
            continue
        unresolved = set(edits[revision]['candidates']) - verdicts.keys()
        if not unresolved and committed == desired and not any(row['revision'] == revision for row in publications):
            publication = dict(tick=tick, revision=revision,
                               regenerated=[shot for shot in shots if committed[shot] != initial[shot]])
            publications.append(publication)
            events.append(dict(action='publish', **publication))

        if policy == 'wait_worklist' and unresolved:
            continue
        active = [job for job in jobs if job['done_tick'] > tick]
        for shot in shots:
            if len(active) >= protocol['capacity']:
                break
            if committed[shot] == desired[shot]:
                continue
            if any(job['shot'] == shot and job['fingerprint'] == desired[shot] for job in active):
                continue
            if any(committed[edge['parent']] != desired[edge['parent']] for edge in hard if edge['child'] == shot):
                continue
            job = dict(shot=shot, revision=revision, fingerprint=desired[shot],
                       submit_tick=tick, done_tick=tick + protocol['render_ticks'][shot])
            jobs.append(job)
            active.append(job)
            events.append(dict(tick=tick, action='submit', shot=shot, revision=revision,
                               fingerprint=job['fingerprint'],
                               references={edge['parent']: desired[edge['parent']]
                                           for edge in hard if edge['child'] == shot}))

    final = next((row for row in publications if row['revision'] == revision), None)
    # Labels are used only after the policy finishes.
    actual, gold = set(final['regenerated']) if final else set(), set(trace['gold'])
    return dict(trace=trace['id'], policy=policy, published=final is not None,
                correct_publication=final is not None and actual == gold,
                final_latency_ticks=final['tick'] - edits[revision]['tick'] if final else None,
                missed=sorted(gold - actual) if final else None,
                excess=sorted(actual - gold) if final else None,
                submission_intents=len(jobs),
                final_useful_intents=sum(job.get('result') == 'applied' and
                                         job['fingerprint'] == committed[job['shot']] for job in jobs)
                if final else None,
                stale_completions=sum(job.get('result') == 'stale' for job in jobs),
                rejected_evidence=sum(row.get('result') in ('stale_revision', 'source_or_scope_mismatch')
                                      for row in events),
                publications=publications, events=events)


def run(protocol, fixture_dir):
    sources = source_digests(protocol, fixture_dir)
    rows = [replay(protocol, trace, policy, sources)
            for trace in protocol['traces'] for policy in POLICIES]
    return dict(evidence='synthetic_cpu_replay_with_scripted_verdicts',
                source_sha256=sources, protocol=protocol, rows=rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, default=FIXTURES / 'egdr_versioned_replay_v1.json')
    args = parser.parse_args()
    print(json.dumps(run(json.loads(args.fixture.read_text(encoding='utf-8')), args.fixture.parent), indent=2))
