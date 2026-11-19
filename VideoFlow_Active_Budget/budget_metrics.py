"""Export matched-budget candidate and graph-closure scores."""
import argparse
import csv
import json
from pathlib import Path
from zipfile import ZipFile

from active_budget import closure, load, score
from replay_positions import replay_archive


POLICIES = ('fixed', 'fixed-recheck', 'fixed-s2-first', 'fixed-s3-first',
            'graph-static', 'uncertainty', 'graph-aware')
BUDGETS = (2, 3, 4, 5)


def replay_926(archive, protocol_path):
    # Recheck the archive's protocol, frame digests, parsed votes, and original scores.
    checked = replay_archive(archive, protocol_path)
    cases, banks = {}, {}
    original = load(protocol_path)['cases']
    with ZipFile(archive) as files:
        report, = (entry for entry in files.namelist()
                   if entry.count('/') <= 1 and entry.split('/')[-1] == 'report.json')
        root = report[:-len('report.json')]
        for block, scores in checked['original']['seeds'].items():
            order = [shot for shot, _ in scores[3]['fixed']['trace'][:2]]
            cases[block] = dict(original[block], recheck_order=order)
            banks[block] = {shot: [
                json.loads(files.read(f'{root}{block}/{shot}-{index}.json'))
                for index in range(1, 5)] for shot in order}
    return score(cases, banks, BUDGETS, POLICIES)['seeds']


def selections(rows, cases, budget, policy):
    for block, budgets in rows.items():
        case = cases[block]
        if policy == 'oracle':
            chosen = set(case['expected'])
        elif budget in budgets or str(budget) in budgets:
            policies = budgets[budget] if budget in budgets else budgets[str(budget)]
            chosen = set(policies[policy]['selected'])
        else:
            # At two calls each candidate has exactly one view. Distinct requires
            # two independent negative views, so conservative retention selects both.
            if budget != 2 or len(case['candidates']) != 2:
                raise ValueError('cannot derive missing budget')
            chosen = set(case['candidates'])
        yield case, chosen


def summarize(cohort, rows, cases):
    if not rows:
        raise ValueError(f'empty cohort: {cohort}')
    policies = next(iter(next(iter(rows.values())).values()))
    for budget in BUDGETS:
        for policy in (*policies, 'oracle'):
            exact = missed = extra = tp = planned = missed_render = excess_render = 0
            for case, selected in selections(rows, cases, budget, policy):
                expected = set(case['expected'])
                missed += len(expected - selected)
                extra += len(selected - expected)
                tp += len(expected & selected)
                exact += int(expected == selected)
                selected_closure = closure({'S1'} | selected, case['edges'])
                expected_closure = closure({'S1'} | expected, case['edges'])
                planned += len(selected_closure)
                missed_render += len(expected_closure - selected_closure)
                excess_render += len(selected_closure - expected_closure)
            yield dict(
                cohort=cohort, seeds=len(rows), budget=budget, policy=policy,
                evidence=('oracle-labels' if policy == 'oracle' else
                          'derived-one-view-fallback' if budget == 2 and cohort == '927' else 'replay'),
                precision=tp / (tp + extra) if tp + extra else 0,
                recall=tp / (tp + missed) if tp + missed else 0,
                f1=2 * tp / (2 * tp + extra + missed) if 2 * tp + extra + missed else 0,
                exact=exact, missed=missed, excess=extra, planned_shots=planned,
                missed_planned=missed_render, excess_planned=excess_render)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cohort', required=True)
    parser.add_argument('--protocol', type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--archive-926', type=Path)
    source.add_argument('--report', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.archive_926:
        rows = replay_926(args.archive_926, args.protocol)
    else:
        report = load(args.report)
        if report['design'] != 'shared-response-bank-replay':
            raise ValueError('requires a shared response bank report')
        rows = report['source_eligible']['seeds']
    cells = list(summarize(args.cohort, rows, load(args.protocol)['cases']))
    with args.output.open('w', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=list(cells[0]))
        writer.writeheader()
        writer.writerows(cells)


if __name__ == '__main__':
    main()
