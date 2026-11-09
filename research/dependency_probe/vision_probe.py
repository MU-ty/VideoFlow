"""Compare direct visual shot selection with the two-input Checklist union."""
import argparse
import base64
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
from urllib.request import Request, urlopen

from egdr import close_selection, public_case, shot_sources, summarize
from ollama_probe import predict

FRACTIONS = (0.10, 0.35, 0.60, 0.85)
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*')
VISUAL_INSTRUCTIONS = (
    'All four panels show the same shot BEFORE any edit. Answer a visibility question about '
    'those original frames: is the exact object instance and part, action or relation named '
    'as the edit target visible in at least one panel? The edit is a future instruction. '
    'Do not look for the requested outcome or decide whether the edit already happened. '
    'For example, if the instruction is to turn a green cap orange, seeing that green cap '
    'on the specified object is sufficient for yes. Seeing a similar cap on another object '
    'is not a match. Excluded objects can coexist in the shot. '
    'Answer no if the target is absent, off-screen or occluded in all four panels. '
    'Use unknown only if the frames are unreadable or a visible candidate cannot be identified. '
    'Panels are numbered 1 top left, 2 top right, 3 bottom left, 4 bottom right. '
    'Return JSON with keys decision (yes, no, unknown), panel (1 to 4 for yes, null otherwise), '
    'and evidence (a short description of the original-frame evidence).\n'
)


def load_cases(path):
    cases = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(cases, list) or not cases:
        raise ValueError('expected a nonempty list of cases')
    seen = set()
    story_clips = {}
    for case in cases:
        case_id, story = case['id'], case['story']
        if not all(isinstance(s, str) and ID.fullmatch(s) for s in (case_id, story)):
            raise ValueError('case and story IDs must be safe path components')
        if case_id in seen:
            raise ValueError(f'duplicate case ID: {case_id}')
        seen.add(case_id)
        shots = set(case['shots'])
        if not shots or not all(ID.fullmatch(s) for s in shots) or shots != set(case['clips']):
            raise ValueError(f'{case_id}: every shot needs one clip and one description')
        if any(not shot_sources(desc, True).get('observation') for desc in case['shots'].values()):
            raise ValueError(f'{case_id}: every shot needs an independent Frame observation note')
        clips = {shot: (path.parent / file).resolve(strict=True) for shot, file in case['clips'].items()}
        if not all(file.is_file() for file in clips.values()):
            raise ValueError(f'{case_id}: clips must be files')
        if story_clips.setdefault(story, clips) != clips:
            raise ValueError(f'{case_id}: cases from the same story must use the same pre-edit clips')
        case['clips'] = clips
        public = planning_input(case)
        gold = set(case['gold'])
        if not set(case['declared']) <= gold <= shots or close_selection(public, gold) != gold:
            raise ValueError(f'{case_id}: gold must include declared shots and be closed over hard edges')
    return cases


def planning_input(case):
    return public_case(case) | {key: case[key] for key in ('edges', 'costs') if key in case}


def make_sheet(clip, sheet):
    probe = subprocess.check_output([
        'ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
        'stream=duration:format=duration', '-of', 'json', str(clip),
    ], text=True)
    info = json.loads(probe)
    stream, = info['streams']
    duration = float(stream.get('duration') or info['format']['duration'])
    if not 0 < duration < float('inf'):
        raise ValueError(f'invalid video duration: {clip}')
    times = [round(duration * fraction, 6) for fraction in FRACTIONS]
    cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-filter_complex_threads', '1']
    for time in times:
        cmd.extend(['-ss', str(time), '-i', str(clip)])
    panels = ';'.join(
        f'[{i}:v:0]scale=768:432:force_original_aspect_ratio=decrease,'
        f'pad=768:432:(ow-iw)/2:(oh-ih)/2,setsar=1[p{i}]' for i in range(len(times))
    )
    subprocess.run(cmd + [
        '-filter_complex', panels + ';[p0][p1][p2][p3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0',
        '-frames:v', '1', '-threads', '1', '-q:v', '2', '-update', '1', str(sheet),
    ], check=True)
    return dict(sample_seconds=times, clip_sha256=sha256(clip.read_bytes()).hexdigest(),
                sheet_sha256=sha256(sheet.read_bytes()).hexdigest())


def visual_reply(server, model, edit, sheet):
    prompt = VISUAL_INSTRUCTIONS + json.dumps({'edit': edit}, ensure_ascii=False)
    request = Request(server.rstrip('/') + '/api/chat', data=json.dumps({
        'model': model, 'stream': False, 'format': 'json',
        'options': {'temperature': 0, 'seed': 42, 'num_predict': 384},
        'messages': [{'role': 'user', 'content': prompt,
                      'images': [base64.b64encode(sheet.read_bytes()).decode('ascii')]}],
    }).encode(), headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=600) as response:
        return json.load(response)['message']['content']


def parse_visual(raw):
    answer = json.loads(raw)
    if (not isinstance(answer, dict) or answer.get('decision') not in ('yes', 'no', 'unknown')
            or not isinstance(answer.get('evidence'), str) or 'panel' not in answer):
        raise ValueError('invalid visual reply')
    panel = answer['panel']
    if answer['decision'] == 'yes':
        if type(panel) is not int or not 1 <= panel <= len(FRACTIONS) or not answer['evidence'].strip():
            raise ValueError('positive reply requires a panel and visible evidence')
    elif panel is not None:
        raise ValueError('non-positive reply requires a null panel')
    return answer


def run_case(case, sheets, server, text_model, vision_model, log):
    public = planning_input(case)
    declared = close_selection(public, set())
    methods = {'attr_dag': declared, 'full': set(case['shots'])}
    for observed in (False, True):
        method = 'checklist_observed' if observed else 'checklist_text'
        shots = {shot: ' '.join(shot_sources(desc, observed).values())
                 for shot, desc in case['shots'].items()}
        proposed, raw = predict(server, text_model, dict(edit=case['edit'], shots=shots,
                                                        declared=sorted(declared)), 'checklist')
        log(dict(case=case['id'], method=method, raw=raw))
        if proposed is None:
            raise ValueError(f"{case['id']}: invalid {method} reply")
        methods[method] = close_selection(public, set(proposed))
    union = methods['checklist_text'] | methods['checklist_observed']
    methods['checklist_union'] = close_selection(public, union)

    positives = set()
    unknown = []
    for shot in sorted(set(case['shots']) - declared):
        raw = visual_reply(server, vision_model, case['edit'], sheets[shot])
        log(dict(case=case['id'], method='vision', shot=shot, raw=raw))
        reply = parse_visual(raw)
        if reply['decision'] == 'yes':
            positives.add(shot)
        elif reply['decision'] == 'unknown':
            unknown.append(shot)
    methods['vision'] = close_selection(public, positives)
    methods['union_plus_vision'] = close_selection(public, union | positives)
    gold, costs = set(case['gold']), case.get('costs', {})
    results = [dict(method=method, selected=sorted(selected), missed=sorted(gold - selected),
                    excess=sorted(selected - gold), cost=sum(costs.get(s, 1) for s in selected))
               for method, selected in methods.items()]
    return dict(case=case['id'], story=case['story'], gold=sorted(gold), results=results,
                visual_positives=sorted(positives), visual_unknown=unknown,
                vision_calls=len(case['shots']) - len(declared))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='new directory for frames and reports')
    parser.add_argument('--prepare-only', action='store_true', help='extract and hash frames without inference')
    parser.add_argument('--server', default='http://127.0.0.1:11434')
    parser.add_argument('--text-model', default='qwen2.5:14b')
    parser.add_argument('--vision-model', default='qwen2.5vl:7b')
    args = parser.parse_args()
    cases = load_cases(args.cases)
    args.output.mkdir(parents=True)
    sheets, frames = {}, {}
    for case in cases:
        story = case['story']
        if story not in sheets:
            folder = args.output / 'frames' / story
            folder.mkdir(parents=True)
            sheets[story] = {shot: folder / f'{shot}.jpg' for shot in case['clips']}
            frames[story] = {shot: make_sheet(clip, sheets[story][shot])
                             for shot, clip in case['clips'].items()}
    protocol = dict(dataset=args.cases.name, dataset_sha256=sha256(args.cases.read_bytes()).hexdigest(),
                    frame_fractions=FRACTIONS, frames=frames, text_model=args.text_model,
                    vision_model=args.vision_model, visual_instructions=VISUAL_INSTRUCTIONS,
                    prepare_only=args.prepare_only)
    (args.output / 'protocol.json').write_text(json.dumps(protocol, indent=2) + '\n', encoding='utf-8')
    if args.prepare_only:
        return
    rows = []
    with (args.output / 'responses.jsonl').open('x', encoding='utf-8') as output:
        def log(row):
            output.write(json.dumps(row, ensure_ascii=False) + '\n')
            output.flush()

        for case in cases:
            print(f"{case['story']}/{case['id']}", flush=True)
            rows.append(run_case(case, sheets[case['story']], args.server, args.text_model,
                                 args.vision_model, log))
    totals = summarize(rows)
    by_story = {story: summarize([row for row in rows if row['story'] == story]) for story in sheets}
    union = totals['checklist_union']
    deltas = {method: {metric: totals[method][metric] - union[metric] for metric in ('missed', 'excess')}
              for method in ('vision', 'union_plus_vision')}
    report = dict(protocol=protocol, cases=rows, totals=totals, by_story=by_story, versus_union=deltas)
    (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(totals=totals, versus_union=deltas), indent=2))


if __name__ == '__main__':
    main()
