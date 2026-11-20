"""Compare strict and linked-only negative votes on a frozen response bank."""

import argparse
from pathlib import Path

from active_budget import FRACTIONS, load, observations, save, score, sha


POLICIES = ('fixed', 'uncertainty')
BUDGETS = (2, 3, 4, 5)


def analyze(protocol_path, directory):
    protocol = load(protocol_path)
    metadata = load(directory / 'metadata.json')
    report = load(directory / 'report.json')
    if (metadata['protocol_sha256'] != sha(protocol_path)
            or metadata['model']['model'] != protocol['model']
            or report['design'] != 'shared-response-bank-replay'):
        raise ValueError('protocol, model, or replay design mismatch')
    if ('rule_model_digest' in protocol
            and metadata['model']['digest'] != protocol['rule_model_digest']):
        raise ValueError('model digest differs from the locked validation protocol')
    cases = protocol['cases']
    statuses, eligibility = report['source_status'], report['eligibility']
    if (set(statuses) != set(cases)
            or set(eligibility) != {block for block, status in statuses.items() if status == 'completed'}
            or set(eligibility.values()) - {'eligible', 'excluded'}):
        raise ValueError('bank does not cover the expected source cohort')
    included = sorted(block for block, status in eligibility.items() if status == 'eligible')
    strict, linked = {}, {}
    for block in sorted(eligibility):
        case = cases[block]
        strict[block], linked[block] = {}, {}
        for shot in case['candidates']:
            strict[block][shot], linked[block][shot] = [], []
            for i in range(1, len(FRACTIONS) + 1):
                path = directory / block / f'{shot}-{i}.json'
                row = load(path)
                if row['prompt'] != case['prompt'] or row['image_sha256'] != sha(path.with_suffix('.png')):
                    raise ValueError(f'frozen response mismatch at {path}')
                if row.get('error'):
                    old = new = ['unknown', 'unknown']
                else:
                    old = observations(row['raw'], protocol['schema'], case)
                    new = observations(row['raw'], protocol['schema'], case,
                                       require_complete_for_distinct=False)
                if row['votes'] != old:
                    raise ValueError(f'archived strict votes differ at {path}')
                strict[block][shot].append(row)
                linked[block][shot].append(dict(row, votes=new))
    budgets = tuple(b for b in BUDGETS if str(b) in report['totals'])
    baseline = score(cases, strict, budgets, POLICIES)
    reported = {block: {int(b): {a: arms[a] for a in POLICIES}
                        for b, arms in by_budget.items() if int(b) in budgets}
                for block, by_budget in report['seeds'].items()}
    if baseline['seeds'] != reported:
        raise ValueError('strict replay drifted from the archived report')
    return dict(protocol_sha256=sha(protocol_path), bank_report_sha256=sha(directory / 'report.json'),
                model=metadata['model'], source_status=statuses, eligibility=eligibility,
                primary_budget=protocol.get('primary_budget', 3),
                strict=score(cases, {k: strict[k] for k in included}, budgets, POLICIES),
                linked_only=score(cases, {k: linked[k] for k in included}, budgets, POLICIES))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol', required=True, type=Path)
    parser.add_argument('--bank', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = analyze(args.protocol, args.bank)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    save(args.output, result)
    budget = result['primary_budget']
    print(f'budget={budget}, saved {args.output}')
    for rule in ('strict', 'linked_only'):
        print(rule, result[rule]['totals'][budget])


if __name__ == '__main__':
    main()
