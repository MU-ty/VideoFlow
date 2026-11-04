"""Score affected-shot selection under missing dependency annotations."""
import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from attribute_baseline import attribute_plan


def declared_closure(case):
    anchor, *children = case['declared']
    story = {
        'scenes': [dict(id=shot, cost=1) for shot in case['shots']],
        'edges': [dict(parent=anchor, child=child, kind='state', keys=['edit'])
                  for child in children],
    }
    return set(attribute_plan(story, dict(target=anchor, key='edit')).regenerate)


def has_phrase(description, phrase):
    return re.search(r'\b' + re.escape(phrase) + r'\b', description, re.IGNORECASE) is not None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, default=Path(__file__).with_name('dev.json'))
    parser.add_argument('--predictions', type=Path,
                        help='JSON mapping case ID to a list of newly proposed shot IDs')
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text())
    predictions = json.loads(args.predictions.read_text()) if args.predictions else None
    if predictions is not None and set(predictions) != {case['id'] for case in cases}:
        parser.error('predictions must cover every case exactly by ID')

    totals = {}
    rows = []
    for case in cases:
        available = set(case['shots'])
        gold = set(case['gold'])
        declared = declared_closure(case)
        if not declared <= gold <= available:
            parser.error(f"{case['id']}: expected omission-only annotations")
        lexical = {shot for shot, description in case['shots'].items()
                   if has_phrase(description, case['entity'])
                   and has_phrase(description, case['attribute'])}
        methods = {'attr_dag': declared, 'keyword_expand': declared | lexical,
                   'full': available}
        if predictions is not None:
            proposed = predictions[case['id']]
            if (not isinstance(proposed, list)
                    or not all(isinstance(shot, str) and shot in available for shot in proposed)):
                parser.error(f"{case['id']}: predictions must be a list of valid shot IDs")
            methods['external_expand'] = declared | set(proposed)
        for method, selected in methods.items():
            row = dict(case=case['id'], method=method, selected=sorted(selected),
                       missed=sorted(gold - selected), excess=sorted(selected - gold))
            rows.append(row)
            total = totals.setdefault(method, dict(missed=0, excess=0, exact_cases=0))
            total['missed'] += len(row['missed'])
            total['excess'] += len(row['excess'])
            total['exact_cases'] += int(selected == gold)
    print(json.dumps(dict(rows=rows, totals=totals), indent=2))


if __name__ == '__main__':
    main()
