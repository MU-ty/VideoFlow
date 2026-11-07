"""Audit the 20-trial independent-shot comparison and reconstruct execution costs."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

from recovery_observability_comfy import WORKFLOW_FAULTS, WORKFLOW_POLICIES
from artifacts import write_json
from audit_archived_cost import trial
from summarize_workflow import summarize


def audit(run):
    read = lambda path: (run.parent / path).read_bytes()
    protocol = json.loads((run / 'protocol.json').read_text())
    summary = json.loads((run / 'summary.json').read_text())
    if (protocol['suite'] != 'workflow' or protocol['pilot'] or protocol['repetitions'] != 5
            or not protocol['pair_boundary_audit'] or not summary['complete']):
        raise ValueError('expected a complete five-seed workflow comparison')
    groups = defaultdict(dict)
    with TemporaryDirectory() as temporary:
        db_copy = Path(temporary) / 'trial.db'
        for row in summary['rows']:
            rep, fault, policy = row['repetition'], row['fault'], row['policy']
            name = f'{rep:02d}-{fault}-{policy}-independent-cap3'
            folder = run / name
            report = json.loads((folder / 'report.json').read_text())
            if row != dict(repetition=rep, **report):
                raise ValueError(f'summary disagrees with trial report: {name}')
            passes = [json.loads(line) for line in (folder / 'worker.jsonl').read_text().splitlines()]
            first_probe = next(entry for entry in passes if entry['target_probed'])
            boundary = report['workflow_pair']
            if (passes[0]['tracked_before'] != boundary['first_poll_tracked']
                    or passes[0]['available_slots'] != boundary['first_poll_slots']
                    or first_probe['target_probe_masked'] != boundary['first_s4_probe_masked']
                    or passes[-1]['masked_polls'] != report['masked_polls']
                    or any(p['admitted'] > max(0, 3 - p['tracked_before']) for p in passes)):
                raise ValueError(f'workflow logs disagree with boundary or admission cap: {name}')
            measured = trial(read, f'{run.name}/{name}/project.db', db_copy)
            counts = Counter(job['shot'] for job in measured['jobs'])
            if counts != report['accepted_by_shot']:
                raise ValueError(f'per-shot acceptance count mismatch: {name}')
            obsolete_s4 = [job['backend_span_seconds'] for job in measured['jobs']
                           if job['shot'] == 'S4' and job['original']
                           and job['in_final_manifest'] is False]
            measured['unused_original_s4_seconds'] = sum(obsolete_s4) if measured['published_by_deadline'] else None
            if policy in groups[(rep, fault)]:
                raise ValueError(f'duplicate trial: {name}')
            groups[(rep, fault)][policy] = measured
    start = protocol['start_repetition']
    if set(groups) != {(rep, fault) for rep in range(start, start + 5) for fault in WORKFLOW_FAULTS}:
        raise ValueError('incomplete workflow matrix')
    strata = summarize(summary['rows'], require_boundary=True)
    costs = {}
    for fault in WORKFLOW_FAULTS:
        rows = []
        for rep in range(start, start + 5):
            base, vf = (groups[(rep, fault)][policy] for policy in WORKFLOW_POLICIES)
            rows.append(dict(repetition=rep, baseline=base, videoflow=vf,
                             execution_span_delta_seconds=round(vf['backend_span_seconds'] - base['backend_span_seconds'], 3)))
        costs[fault] = dict(pairs=rows, paired_median_execution_span_delta_seconds=median(
            row['execution_span_delta_seconds'] for row in rows))
    return dict(protocol=protocol, strata=strata, costs=costs,
                timing='Wall spans from provider messages include host work. ACK-reply to start is a signed proxy, not pure queue time.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    write_json(args.output, audit(args.run))


if __name__ == '__main__':
    main()
