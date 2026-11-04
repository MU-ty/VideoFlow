"""Generate paired, label-blind shot predictions with a local Ollama model."""
import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen

DIRECT = (
    "Select every additional shot whose visible content must change to satisfy the edit. "
    "Resolve cross-shot references, visibility, and explicit exclusions. "
    "Use the complete shot list as context."
)
CHECKLIST = (
    "For each unmarked shot, check three conditions before deciding: "
    "(1) the depicted entity is the same target, resolving aliases and references; "
    "(2) the edited attribute is actually visible; "
    "(3) the depiction is in scope and not explicitly excluded. "
    "Select shots only if all three checks pass."
)


def predict(url, model, case, instruction):
    public = {key: case[key] for key in ('edit', 'shots', 'declared')}
    prompt = (
        instruction + " The declared shots are already selected. "
        'Return only a JSON object of the form {"additional":["S3"]}. '
        "Use [] when no more shots need editing.\n" + json.dumps(public, ensure_ascii=False)
    )
    request = Request(url.rstrip('/') + '/api/chat', data=json.dumps({
        'model': model, 'stream': False, 'format': 'json',
        'options': {'temperature': 0, 'seed': 42, 'num_predict': 256},
        'messages': [{'role': 'user', 'content': prompt}],
    }).encode(), headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=600) as response:
        reply = json.load(response)
    raw = reply['message']['content']
    return parse_selection(raw, case), raw


def parse_selection(raw, case):
    try:
        result = json.loads(raw)
        selected = result['additional']
        allowed = set(case['shots'])
        if (not isinstance(result, dict) or set(result) != {'additional'}
                or not isinstance(selected, list)
                or not all(isinstance(shot, str) and shot in allowed for shot in selected)):
            raise ValueError('invalid additional shot list')
    except (KeyError, TypeError, ValueError):
        return None
    declared = set(case['declared'])
    return [shot for shot in dict.fromkeys(selected) if shot not in declared]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, default=Path(__file__).with_name('dev.json'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', default='qwen2.5:14b')
    parser.add_argument('--server', default='http://127.0.0.1:11434')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--resume', type=Path, help='Reuse saved replies and request only missing pairs')
    source.add_argument('--replay', type=Path, help='Reprocess saved responses.jsonl without calling Ollama')
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding='utf-8'))
    methods = {'direct': DIRECT, 'checklist': CHECKLIST}
    replay = {}
    previous = args.replay or args.resume
    if previous:
        rows = [json.loads(line) for line in previous.read_text(encoding='utf-8').splitlines()]
        replay = {(row['case'], row['method']): row for row in rows}
        expected = {(case['id'], method) for case in cases for method in methods}
        if len(rows) != len(replay) or not replay.keys() <= expected:
            parser.error('saved replies contain duplicate or unexpected case-method pairs')
        if args.replay and replay.keys() != expected:
            parser.error('replay requires all case-method pairs')
        if args.resume and any(row['model'] != args.model for row in rows):
            parser.error('resume requires the same model as the saved replies')
    args.output.mkdir(parents=True, exist_ok=False)
    outputs = {method: {} for method in methods}
    errors = 0
    with (args.output / 'responses.jsonl').open('w', encoding='utf-8') as log:
        for index, case in enumerate(cases):
            order = list(methods)
            if index % 2:
                order.reverse()
            for method in order:
                if (case['id'], method) not in replay:
                    selected, raw = predict(args.server, args.model, case, methods[method])
                    model = args.model
                else:
                    row = replay[case['id'], method]
                    raw, model = row['raw'], row['model']
                    selected = parse_selection(raw, case)
                log.write(json.dumps(dict(case=case['id'], method=method, model=model,
                                          valid=selected is not None, raw=raw)) + '\n')
                log.flush()
                if selected is None:
                    errors += 1
                else:
                    outputs[method][case['id']] = selected
                print(case['id'], method, selected if selected is not None else 'invalid', flush=True)
    if errors:
        parser.exit(1, f'{errors} invalid replies. Raw responses saved. Paired scores withheld.\n')
    for method, result in outputs.items():
        (args.output / f'{method}.json').write_text(json.dumps(result, indent=2) + '\n',
                                                   encoding='utf-8')


if __name__ == '__main__':
    main()
