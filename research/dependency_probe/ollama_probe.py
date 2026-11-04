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
    try:
        result = json.loads(raw)
        selected = result['additional']
        allowed = set(case['shots']) - set(case['declared'])
        if (not isinstance(result, dict) or set(result) != {'additional'}
                or not isinstance(selected, list)
                or not all(isinstance(shot, str) and shot in allowed for shot in selected)
                or len(set(selected)) != len(selected)):
            raise ValueError('invalid additional shot list')
    except (KeyError, TypeError, ValueError):
        return None, raw
    return selected, raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, default=Path(__file__).with_name('dev.json'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', default='qwen2.5:14b')
    parser.add_argument('--server', default='http://127.0.0.1:11434')
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding='utf-8'))
    args.output.mkdir(parents=True, exist_ok=False)
    methods = {'direct': DIRECT, 'checklist': CHECKLIST}
    outputs = {method: {} for method in methods}
    errors = 0
    with (args.output / 'responses.jsonl').open('w', encoding='utf-8') as log:
        for index, case in enumerate(cases):
            order = list(methods)
            if index % 2:
                order.reverse()
            for method in order:
                selected, raw = predict(args.server, args.model, case, methods[method])
                log.write(json.dumps(dict(case=case['id'], method=method, model=args.model,
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
