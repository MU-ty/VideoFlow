"""Replay recorded visual replies or capture a paired evidence reuse experiment."""
import argparse
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from zipfile import ZipFile

from vision_probe import (
    VERIFY_SCHEMA, image_reply, make_sheet, parse_verification,
    parse_visual, visual_reply,
)

POLICIES = ('fresh', 'exact_source', 'witness_reuse')
FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def digest(data):
    return sha256(data).hexdigest()


def compare(edits, *, evidence):
    """All arms consume the same captured answers on a cache miss."""
    whole = {policy: set() for policy in POLICIES}
    witnesses = set()
    rows = []
    for edit in edits:
        for policy in POLICIES:
            selected, unresolved, hits = set(), set(), {}
            calls = 0
            for shot, probe in edit['probes'].items():
                if probe.get('witness') is not None and (probe['decision'] != 'yes'
                                                        or probe['witness'] not in probe['panels']):
                    raise ValueError('a witness must be a positive bound to a supplied panel')
                scope = (edit['story'], edit['question'], shot)
                source = (scope, tuple(probe['panels']))
                if policy != 'fresh' and source in whole[policy]:
                    decision = 'yes'
                    hits[shot] = 'exact_source'
                elif policy == 'witness_reuse' and any(
                    (scope, panel) in witnesses for panel in probe['panels']
                ):
                    decision = 'yes'
                    hits[shot] = 'verified_panel'
                else:
                    decision = probe['decision']
                    calls += probe['calls']
                    if decision == 'yes' and policy != 'fresh':
                        whole[policy].add(source)
                        if policy == 'witness_reuse' and probe.get('witness') is not None:
                            witnesses.add((scope, probe['witness']))
                if decision == 'yes':
                    selected.add(shot)
                elif decision == 'unknown':
                    unresolved.add(shot)
            gold = set(edit['gold'])
            rows.append(dict(story=edit['story'], edit=edit['id'], condition=edit['condition'],
                             policy=policy, selected=sorted(selected), cache_hits=hits,
                             model_calls=calls, missed=sorted(gold - selected),
                             excess=sorted(selected - gold), unresolved=sorted(unresolved)))
    totals = {}
    for policy in POLICIES:
        group = [row for row in rows if row['policy'] == policy]
        totals[policy] = dict(model_calls=sum(row['model_calls'] for row in group),
                              cache_hits=sum(len(row['cache_hits']) for row in group),
                              missed=sum(len(row['missed']) for row in group),
                              excess=sum(len(row['excess']) for row in group),
                              unresolved=sum(len(row['unresolved']) for row in group))
    return dict(evidence=evidence, totals=totals, rows=rows, edits=edits)


def archive_edits(report_path, responses_path, sheets):
    report = json.loads(report_path.read_text(encoding='utf-8'))
    if not report['visual_evaluation_complete'] or not report['verification_evaluation_complete']:
        raise ValueError('a complete visual verification report is required')
    grouped = defaultdict(list)
    for line in responses_path.read_text(encoding='utf-8').splitlines():
        row = json.loads(line)
        if row['method'] in ('vision', 'vision_verify'):
            grouped[row['case'], row['shot']].append(row)
    protocol = report['protocol']
    for story, shots in protocol['frames'].items():
        for shot, frame in shots.items():
            sheet = sheets / story / f'{shot}.jpg'
            if digest(sheet.read_bytes()) != frame['sheet_sha256']:
                raise ValueError(f'archived frame hash mismatch: {sheet}')
    for case in report['cases']:
        case_id, story = case['case'], case['story']
        probes = {}
        for shot, frame in protocol['frames'][story].items():
            log = grouped[case_id, shot]
            detector = [r['raw'] for r in log if r['method'] == 'vision']
            verifier = [r['raw'] for r in log if r['method'] == 'vision_verify']
            decision = parse_visual(detector[-1])['decision']
            if decision == 'yes':
                decision = 'yes' if parse_verification(verifier[-1]) else 'unknown'
            probes[shot] = dict(panels=[frame['sheet_sha256']], decision=decision, calls=len(log))
        if {s for s, p in probes.items() if p['decision'] == 'yes'} != set(case['verified_positives']):
            raise ValueError(f'{case_id}: raw verification replies disagree with report')
        edit = dict(id=case_id, story=story, question=protocol['visibility_questions'][case_id],
                    gold=case['gold'], probes=probes, condition='archived_original')
        yield edit
        if case_id in ('H1', 'H4'):
            # The archives contain one observation. Replay it on unchanged
            # evidence for a second destination color, without fabricating a call.
            yield dict(edit, id=f'{case_id}-color-only', condition='color_only_replay')


def ffmpeg(*args):
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', *map(str, args)],
                   check=True)


def press_panels(root):
    shots = ('S1', 'S2', 'S3', 'S4')
    archive = FIXTURES / 'egdr-press-control-v1.zip'
    panels = {}
    with ZipFile(archive) as bundle:
        for shot in shots:
            clip = root / f'{shot}.mp4'
            clip.write_bytes(bundle.read(f'{archive.stem}/clips/{shot}.mp4'))
            sheet = root / f'{shot}.jpg'
            make_sheet(clip, sheet)
            tiles = []
            for i, (x, y) in enumerate(((0, 0), (768, 0), (0, 432), (768, 432)), 1):
                tile = root / f'{shot}-{i}.png'
                ffmpeg('-i', sheet, '-vf', f'crop=768:432:{x}:{y}', '-frames:v', 1,
                       '-threads', 1, tile)
                tiles.append(tile)
            panels[shot] = tuple(tiles)
    gray = root / 'occluded.png'
    ffmpeg('-f', 'lavfi', '-i', 'color=c=gray:s=768x432', '-frames:v', 1, '-threads', 1, gray)
    return panels, gray


def press_trace(panels, gray):
    a = ('In any one panel, is the ribbed handwheel mounted on the cream bench press '
         'with two teal stripes visible? The press with an orange diamond does not count.')
    b = ('In any one panel, is the ribbed handwheel mounted on the cream bench press '
         'with an orange diamond visible? The press with teal stripes does not count.')
    changed = dict(panels, S1=(*panels['S1'][:3], panels['S4'][3]),
                   S2=(*panels['S2'][:3], panels['S4'][3]))
    replaced = dict(changed, S3=panels['S2'])
    for name, question, color, frames, gold in (
        ('initial', a, 'blue', panels, ['S1', 'S2']),
        ('color_only', a, 'red', panels, ['S1', 'S2']),
        ('target_switch', b, 'green', panels, ['S3']),
        ('one_panel_replaced', a, 'purple', changed, ['S1', 'S2']),
        ('source_replaced', a, 'yellow', replaced, ['S1', 'S2', 'S3']),
        ('occluded', a, 'black', dict(replaced, S2=(gray,) * 4), ['S1', 'S3']),
    ):
        yield dict(id=name, condition=name, story='press_reuse_control', question=question,
                   color=color, gold=gold), frames


def capture_probe(server, model, question, panels, output, log):
    sheet = output.with_suffix('.png')
    args = [arg for tile in panels for arg in ('-i', tile)]
    ffmpeg(*args, '-filter_complex_threads', 1, '-filter_complex',
           '[0:v][1:v][2:v][3:v]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0',
           '-frames:v', 1, '-threads', 1, sheet)
    calls = 0

    def record(stage, raw):
        nonlocal calls
        calls += 1
        log.write(json.dumps(dict(probe=output.name, stage=stage, raw=raw)) + '\n')
        log.flush()

    for retry in (False, True):
        raw = visual_reply(server, model, '', sheet, visibility_question=question, format_retry=retry)
        record('detect_retry' if retry else 'detect', raw)
        try:
            answer = parse_visual(raw)
            break
        except ValueError:
            if retry:
                raise ValueError(f'invalid detector output for {output.name}') from None

    hashes = [digest(tile.read_bytes()) for tile in panels]
    witness = None
    decision = answer['decision']
    if decision == 'yes':
        index = answer['panel'] - 1
        prompt = ('Inspect this single source frame. Check identity, part, and position separately. '
                  'Return JSON with identity, part, and position as yes, no, or unknown. '
                  'If all three are yes, use panel 1 and describe the visible evidence. '
                  'Otherwise use a null panel. '
                  + question.replace('In any one panel, ', 'In this frame, '))
        raw = image_reply(server, model, panels[index], prompt, VERIFY_SCHEMA)
        record('verify_isolated_panel', raw)
        verdict = parse_verification(raw)
        if verdict and json.loads(raw)['panel'] != 1:
            raise ValueError(f'invalid isolated-panel number for {output.name}')
        decision = 'yes' if verdict else 'unknown'
        if verdict:
            witness = hashes[index]
    return dict(panels=hashes, decision=decision, calls=calls, witness=witness)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    archive = modes.add_parser('archive')
    archive.add_argument('--report', required=True, type=Path)
    archive.add_argument('--responses', required=True, type=Path)
    archive.add_argument('--sheets', required=True, type=Path)
    live = modes.add_parser('live')
    live.add_argument('--output', required=True, type=Path)
    live.add_argument('--server', default='http://127.0.0.1:11434')
    live.add_argument('--model', default='qwen2.5vl:7b-ctx8k')
    args = parser.parse_args()
    if args.mode == 'archive':
        edits = list(archive_edits(args.report, args.responses, args.sheets))
        print(json.dumps(compare(edits, evidence='archived_vision_replies_counterfactual_replay'), indent=2))
        return

    args.output.mkdir(parents=True)
    panels, gray = press_panels(args.output)
    edits = []
    with (args.output / 'responses.jsonl').open('w', encoding='utf-8') as log:
        for edit, frames in press_trace(panels, gray):
            print(edit['id'], flush=True)
            edit['probes'] = {}
            for shot, tiles in frames.items():
                edit['probes'][shot] = capture_probe(
                    args.server, args.model, edit['question'], tiles,
                    args.output / f"{edit['id']}-{shot}", log)
            edits.append(edit)
    report = compare(edits, evidence='live_paired_vision_capture')
    report['vision_model'] = args.model
    (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report['totals'], indent=2))


if __name__ == '__main__':
    main()
