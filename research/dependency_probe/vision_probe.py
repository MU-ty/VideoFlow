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
VISIBILITY_OUTPUT = (
    'Return JSON with decision (yes, no, or unknown), panel (1–4 for yes, '
    'null otherwise), and a short evidence description.'
)
VISUAL_RETRY_SCHEMA = {
    'type': 'object',
    'required': ['decision', 'panel', 'evidence'],
    'additionalProperties': False,
    'properties': {
        'decision': {'enum': ['yes', 'no', 'unknown']},
        'panel': {'enum': [None, 1, 2, 3, 4]},
        'evidence': {'type': 'string'},
    },
}
VERIFY_CHECKS = ('identity', 'part', 'position')
VERIFY_INSTRUCTIONS = (
    'Independently inspect the four original panels for the target in the question. '
    'Check identity: the part belongs to the specified object instance. Check part: '
    'the visible shape, appearance and function match the named part. Check position: '
    'the part is in the specified location or relation. A similar part, a different '
    'object, or an unsupported location is insufficient. Follow any crop allowances '
    'explicitly stated in the question. Do not combine different panels to make a match. '
    'Answer yes, no, or unknown for each check. To answer yes for all three, cite a single '
    'panel (1 top left, 2 top right, 3 bottom left, 4 bottom right) and describe concrete '
    'visual evidence in it. Use null if no panel supports all three checks.\n'
)
VERIFY_SCHEMA = {
    'type': 'object',
    'required': [*VERIFY_CHECKS, 'panel', 'evidence'],
    'additionalProperties': False,
    'properties': {
        **{check: {'enum': ['yes', 'no', 'unknown']} for check in VERIFY_CHECKS},
        'panel': {'enum': [None, 1, 2, 3, 4]},
        'evidence': {'type': 'string'},
    },
}


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


def visual_reply(server, model, edit, sheet, *, visibility_question=None, format_retry=False):
    if visibility_question is None:
        prompt = VISUAL_INSTRUCTIONS + json.dumps({'edit': edit}, ensure_ascii=False)
    else:
        prompt = visibility_question + ' ' + VISIBILITY_OUTPUT
    if format_retry:
        prompt += (
            '\nUse the exact JSON keys decision, panel, and evidence. For yes, use an integer '
            'panel from 1 to 4 and describe the visible evidence from that panel. If you cannot '
            'identify the panel and evidence, use unknown. For no or unknown, use a null panel.'
        )
    return image_reply(server, model, sheet, prompt, VISUAL_RETRY_SCHEMA if format_retry else 'json')


def image_reply(server, model, sheet, prompt, response_format):
    request = Request(server.rstrip('/') + '/api/chat', data=json.dumps({
        'model': model, 'stream': False, 'format': response_format,
        'options': {'temperature': 0, 'seed': 42, 'num_predict': 384},
        'messages': [{'role': 'user', 'content': prompt,
                      'images': [base64.b64encode(sheet.read_bytes()).decode('ascii')]}],
    }).encode(), headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=600) as response:
        return json.load(response)['message']['content']


def parse_visual(raw):
    answer = json.loads(raw)
    if isinstance(answer, dict) and 'evidence' not in answer and 'evidence_description' in answer:
        answer['evidence'] = answer['evidence_description']
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


def parse_verification(raw):
    answer = json.loads(raw)
    if (not isinstance(answer, dict)
            or any(answer.get(key) not in ('yes', 'no', 'unknown') for key in VERIFY_CHECKS)
            or not isinstance(answer.get('evidence'), str) or 'panel' not in answer):
        raise ValueError('invalid verification reply')
    panel = answer['panel']
    if panel is not None and (type(panel) is not int or not 1 <= panel <= len(FRACTIONS)):
        raise ValueError('invalid verification panel')
    matched = all(answer[key] == 'yes' for key in VERIFY_CHECKS)
    if matched and (panel is None or not answer['evidence'].strip()):
        raise ValueError('verification match requires a panel and visible evidence')
    return matched


def collect_selections(case, sheets, server, text_model, vision_model, log, *,
                       visibility_question=None, verify_visual=False):
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
    unknown, retries, invalid = [], [], []
    for shot in sorted(set(case['shots']) - declared):
        raw = visual_reply(server, vision_model, case['edit'], sheets[shot],
                           visibility_question=visibility_question)
        log(dict(case=case['id'], method='vision', shot=shot, attempt=1, raw=raw))
        try:
            reply = parse_visual(raw)
        except ValueError:
            retries.append(shot)
            raw = visual_reply(server, vision_model, case['edit'], sheets[shot],
                               visibility_question=visibility_question, format_retry=True)
            log(dict(case=case['id'], method='vision', shot=shot, attempt=2, raw=raw))
            try:
                reply = parse_visual(raw)
            except ValueError:
                invalid.append(shot)
                continue
        if reply['decision'] == 'yes':
            positives.add(shot)
        elif reply['decision'] == 'unknown':
            unknown.append(shot)
    verified, verification_invalid, verification_unknown = set(), [], []
    if verify_visual:
        prompt = VERIFY_INSTRUCTIONS + visibility_question
        for shot in sorted(positives):
            raw = image_reply(server, vision_model, sheets[shot], prompt, VERIFY_SCHEMA)
            log(dict(case=case['id'], method='vision_verify', shot=shot, raw=raw))
            try:
                matched = parse_verification(raw)
            except ValueError:
                verification_invalid.append(shot)
                continue
            if matched:
                verified.add(shot)
            else:
                verdict = json.loads(raw)
                if all(verdict[key] != 'no' for key in VERIFY_CHECKS):
                    verification_unknown.append(shot)
    if not invalid:
        methods['vision'] = close_selection(public, positives)
        methods['union_plus_vision'] = close_selection(public, union | positives)
        if verify_visual and not verification_invalid:
            methods['vision_verified'] = close_selection(public, verified)
            methods['union_plus_verified'] = close_selection(public, union | verified)
    return methods, dict(visual_positives=sorted(positives), visual_unknown=unknown,
                         visual_retries=retries, visual_invalid=invalid,
                         vision_calls=len(case['shots']) - len(declared) + len(retries),
                         verified_positives=sorted(verified), verification_invalid=verification_invalid,
                         verification_unknown=verification_unknown,
                         verification_calls=len(positives) if verify_visual else 0)


def run_case(case, sheets, server, text_model, vision_model, log, *,
             visibility_question=None, verify_visual=False):
    methods, audit = collect_selections(case, sheets, server, text_model, vision_model, log,
                                        visibility_question=visibility_question,
                                        verify_visual=verify_visual)
    gold, costs = set(case['gold']), case.get('costs', {})
    results = [dict(method=method, selected=sorted(selected), missed=sorted(gold - selected),
                    excess=sorted(selected - gold), cost=sum(costs.get(s, 1) for s in selected))
               for method, selected in methods.items()]
    return dict(case=case['id'], story=case['story'], gold=sorted(gold), results=results, **audit)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='new directory for frames and reports')
    parser.add_argument('--prepare-only', action='store_true', help='extract and hash frames without inference')
    parser.add_argument('--server', default='http://127.0.0.1:11434')
    parser.add_argument('--text-model', default='qwen2.5:14b')
    parser.add_argument('--vision-model', default='qwen2.5vl:7b')
    parser.add_argument('--visibility-questions', type=Path,
                        help='JSON mapping each case ID to a source-only visual question')
    parser.add_argument('--verify-visual', action='store_true',
                        help='check identity, part, and position for each visual positive')
    args = parser.parse_args()
    if args.verify_visual and args.visibility_questions is None:
        parser.error('--verify-visual requires --visibility-questions')
    cases = load_cases(args.cases)
    questions = {}
    if args.visibility_questions is not None:
        questions = json.loads(args.visibility_questions.read_text(encoding='utf-8'))
        if (not isinstance(questions, dict) or set(questions) != {case['id'] for case in cases}
                or not all(isinstance(question, str) and question.strip() for question in questions.values())):
            parser.error('--visibility-questions must contain one nonempty question per case ID')
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
                    vision_model=args.vision_model,
                    visual_instructions=VISIBILITY_OUTPUT if questions else VISUAL_INSTRUCTIONS,
                    visibility_questions=questions,
                    visual_format_retry_limit=1,
                    visual_verification_instructions=VERIFY_INSTRUCTIONS if args.verify_visual else None,
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
                                 args.vision_model, log, visibility_question=questions.get(case['id']),
                                 verify_visual=args.verify_visual))
    invalid = {row['case']: row['visual_invalid'] for row in rows if row['visual_invalid']}
    verification_invalid = {row['case']: row['verification_invalid'] for row in rows
                            if row['verification_invalid']}
    visual_methods = ['vision', 'union_plus_vision']
    if args.verify_visual:
        visual_methods.extend(('vision_verified', 'union_plus_verified'))
    totals = summarize(rows)
    by_story = {story: summarize([row for row in rows if row['story'] == story]) for story in sheets}
    union = totals['checklist_union']
    blocked = visual_methods if invalid else (
        ('vision_verified', 'union_plus_verified') if verification_invalid else ()
    )
    for counts in (totals, *by_story.values()):
        for method in blocked:
            counts.pop(method, None)
    if invalid:
        deltas = None
    else:
        deltas = {method: {metric: totals[method][metric] - union[metric] for metric in ('missed', 'excess')}
                  for method in visual_methods if method not in blocked}
    report = dict(protocol=protocol, cases=rows, totals=totals, by_story=by_story,
                  versus_union=deltas, visual_evaluation_complete=not invalid, visual_invalid=invalid,
                  verification_evaluation_complete=(not invalid and not verification_invalid)
                  if args.verify_visual else None, verification_invalid=verification_invalid)
    (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(totals=totals, versus_union=deltas, visual_invalid=invalid,
                          verification_invalid=verification_invalid), indent=2))


if __name__ == '__main__':
    main()
