"""Summarize matched workflow comparisons while retaining censored outcomes."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
from statistics import median

from recovery_observability_comfy import WORKFLOW_POLICIES
from artifacts import write_json


def difference(left, right):
    return None if left is None or right is None else left - right


def summarize(rows):
    grouped = defaultdict(dict)
    for row in rows:
        key = (row['story'], row['recovery_cap'], row['fault'], row['repetition'])
        pair = grouped[key]
        if row['policy'] in pair:
            raise ValueError(f'duplicate policy row in {key}')
        pair[row['policy']] = row
    strata = defaultdict(list)
    for (story, cap, fault, repetition), pair in sorted(grouped.items()):
        if set(pair) != set(WORKFLOW_POLICIES):
            raise ValueError(f'incomplete or unexpected workflow pair: {(story, cap, fault, repetition)}')
        baseline, videoflow = [pair[policy] for policy in WORKFLOW_POLICIES]
        if any(videoflow[field] != baseline[field]
               for field in ('poll_seconds', 'horizon_seconds', 'pre_edit_inflight', 'seed_offset', 'transient_mask_seconds')):
            raise ValueError('workflow pair uses mismatched execution settings')
        def span(row):
            durations = [job['execution_span_seconds'] for job in row['provider_times']]
            if len(durations) == row['accepted_count'] and all(d is not None for d in durations):
                return sum(durations)
            return None

        def outcome(row):
            latency = row['correct_publication_seconds']
            return dict(published_by_deadline=latency is not None, publication_seconds=latency,
                        stale_publications=len(row['stale_publications']), acceptances=row['accepted_count'],
                        s4_acceptances=row['accepted_by_shot'].get('S4', 0),
                        original_s6_reused=row['original_s6_reused'], masked_polls=row['masked_polls'],
                        backend_execution_span_seconds=span(row))

        base_outcome, vf_outcome = outcome(baseline), outcome(videoflow)
        strata[(story, cap, fault)].append(dict(
            repetition=repetition, baseline=base_outcome, videoflow=vf_outcome,
            publication_seconds_delta=difference(vf_outcome['publication_seconds'], base_outcome['publication_seconds']),
            acceptances_delta=vf_outcome['acceptances']-base_outcome['acceptances'],
            backend_execution_span_seconds_delta=difference(vf_outcome['backend_execution_span_seconds'],
                                                           base_outcome['backend_execution_span_seconds'])))
    result = []
    for (story, cap, fault), pairs in sorted(strata.items()):
        published_deltas = [row['publication_seconds_delta'] for row in pairs
                            if row['publication_seconds_delta'] is not None]
        result.append(dict(story=story, cap=cap, fault=fault, pair_count=len(pairs),
                           baseline_published=sum(row['baseline']['published_by_deadline'] for row in pairs),
                           videoflow_published=sum(row['videoflow']['published_by_deadline'] for row in pairs),
                           both_published=len(published_deltas),
                           median_publication_seconds_delta=median(published_deltas) if published_deltas else None,
                           pairs=pairs))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    protocol = json.loads((args.run / 'protocol.json').read_text())
    raw = json.loads((args.run / 'summary.json').read_text())
    if not raw['complete'] or not raw['rows'] or protocol['suite'] not in ('workflow', 'workflow_capacity'):
        parser.error('need a completed workflow suite')
    expected = protocol['repetitions'] * len(protocol['cells'])
    if len(raw['rows']) != expected:
        parser.error(f'protocol expected {expected} rows')
    write_json(args.output, dict(evidence=protocol['evidence'], pilot=protocol['pilot'], protocol=protocol,
                                 delta='VideoFlow minus workflow strategy',
                                 timing='backend execution spans include host work, not GPU device time',
                                 strata=summarize(raw['rows'])))


if __name__ == '__main__':
    main()
