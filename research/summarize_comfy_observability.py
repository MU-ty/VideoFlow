"""Combine disjoint live trial batches and report censored outcomes separately."""
import argparse
import json
from pathlib import Path
from statistics import median, quantiles


def load_batch(root):
    protocol = json.loads((root / 'protocol.json').read_text())
    summary = json.loads((root / 'summary.json').read_text())
    if (protocol['evidence'] != 'real_comfyui_client_transport_fault_injection'
            or protocol['pilot'] or not summary['complete']):
        raise ValueError(f'expected a complete measured live batch: {root}')
    suite = protocol.get('suite', 'primary')
    cells = [tuple(c) if len(c) == 4 else (*c, protocol['capacity'], 'independent')
             for c in protocol['cells']]
    start = protocol.get('start_repetition', 1)
    expected = [(rep, *cell) for rep in range(start, start + protocol['repetitions'])
                for cell in (cells if rep % 2 else reversed(cells))]
    rows = summary['rows']
    actual = [(r['repetition'], r['fault'], r['policy'], r.get('recovery_cap', protocol['capacity']),
               r.get('story', 'independent')) for r in rows]
    if actual != expected or len(set(actual)) != len(actual):
        raise ValueError(f'trial identity or order differs from protocol: {root}')
    for row in rows:
        if any(row[key] != protocol[key] for key in ('horizon_seconds', 'poll_seconds')):
            raise ValueError(f'trial timing differs from protocol: {root}')
    settings = (protocol['horizon_seconds'], protocol['poll_seconds'], protocol['seed_offset'],
                protocol.get('transient_mask_seconds'), protocol.get('backoff_max_seconds'))
    return suite, settings, zip(actual, rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('batches', nargs='+', type=Path)
    args = parser.parse_args()
    groups = {}
    settings_by_suite = {}
    for root in args.batches:
        suite, settings, trials = load_batch(root)
        if settings_by_suite.setdefault(suite, settings) != settings:
            raise ValueError(f'incompatible timing or seed protocol within {suite}')
        for (rep, fault, policy, cap, story), row in trials:
            group = groups.setdefault((suite, fault, policy, cap, story), {})
            if rep in group:
                raise ValueError(f'duplicate repetition {rep} for {(suite, fault, policy, cap, story)}')
            arrivals = [entry['seconds_from_edit_barrier'] for entry in row['upstream_acceptances'].values()
                        if entry['seconds_from_edit_barrier'] >= 0]
            group[rep] = dict(
                repetition=rep,
                publication_seconds=row['correct_publication_seconds'],
                acceptances=row['accepted_count'],
                unresolved=len(row['tracked_unresolved']),
                stale_publications=len(row['stale_publications']),
                peak_tracked_unresolved=row['peak_tracked_unresolved'],
                original_s6_reused=row['original_s6_reused'],
                first_post_edit_acceptance_seconds=min(arrivals) if arrivals else None,
            )
    cells = []
    for (suite, fault, policy, cap, story), runs in sorted(groups.items()):
        trials = [runs[rep] for rep in sorted(runs)]
        times = [r['publication_seconds'] for r in trials if r['publication_seconds'] is not None]
        q = quantiles(times, method='inclusive') if len(times) > 1 else None
        cells.append(dict(suite=suite, story=story, recovery_cap=cap, fault=fault, policy=policy,
                          published=len(times), trials=len(trials), publication_rate=len(times)/len(trials),
                          median_seconds=median(times) if times else None,
                          q1_seconds=q[0] if q else None, q3_seconds=q[2] if q else None,
                          iqr_seconds=q[2]-q[0] if q else None,
                          acceptances_per_trial=[r['acceptances'] for r in trials],
                          unresolved_per_trial=[r['unresolved'] for r in trials],
                          total_stale_publications=sum(r['stale_publications'] for r in trials),
                          raw=trials))
    paired = []
    for (suite, fault, policy, cap, story), runs in sorted(groups.items()):
        control = ('replace_after_3' if suite in ('backoff', 'ordered_recovery')
                   else 'project_version')
        control_runs = groups.get((suite, fault, control, cap, story), {})
        if control == policy:
            continue
        for rep in sorted(runs.keys() & control_runs.keys()):
            a, b = runs[rep], control_runs[rep]
            both = a['publication_seconds'] is not None and b['publication_seconds'] is not None
            paired.append(dict(suite=suite, story=story, recovery_cap=cap, fault=fault, policy=policy,
                               control=control, repetition=rep,
                               publication_seconds_delta=a['publication_seconds']-b['publication_seconds'] if both else None,
                               acceptances_delta=a['acceptances']-b['acceptances']))
    print(json.dumps(dict(cells=cells, paired=paired), indent=2))


if __name__ == '__main__':
    main()
