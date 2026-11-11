"""Run a matched real-render EGDR pilot from one published source revision."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from artifacts import archive_current, checksum, write_json
from asset_runtime import AssetRuntime, Conflict, fingerprint
from asset_worker import build_h3, pass_once
from comfyui import ComfyUI
from provider_audit import capture_environment, summarize_inspections
from vision_probe import collect_selections, make_sheet
from vision_evidence_reuse import capture_probe, ffmpeg
import egdr_radio_holdout as radio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from video_edit_ablation import asset, wait_for


POLICIES = {
    'declared': 'attr_dag',
    'checklist': 'checklist_union',
    'raw_union': 'union_plus_vision',
    'egdr': 'union_plus_verified',
    'egdr_conservative': 'union_plus_unresolved',
    'full': 'full',
    'worklist': 'attr_dag',
    'online_egdr': 'online_egdr',
    'oracle_egdr': 'oracle_egdr',
}
DEFERRED_POLICIES = {'online_egdr', 'oracle_egdr'}
KEY = 'target.color'
ENTITY = 'ribbed amber handwheel on the cream press with two teal stripes'


def pilot_inputs(seed):
    base = 'a cream bench press with two teal stripes and a large ribbed amber handwheel'
    shots = {
        'S1': f'Steady side close-up of {base}. A technician turns the handwheel.',
        'S2': f'Medium shot of {base} on a workbench. A technician lowers the press ram.',
        'S3': 'Front close-up of a different cream press with an orange diamond decal, '
              'no teal stripes, and an amber handwheel.',
        'S4': 'A static shot of a closed gray toolbox on another bench. No press is visible.',
    }
    edit = ('Recolor only the ribbed amber handwheel of the cream press with two teal stripes '
            'to cobalt blue wherever visible. Keep the orange-diamond press and all other '
            'objects unchanged. Do not add an absent press or handwheel.')
    case = dict(id='striped-press', entity=ENTITY, attribute='color', edit=edit,
                shots=shots, declared=['S1'])
    question = ('Do any of the four original panels show a ribbed amber handwheel '
                'belonging to the cream press with two teal stripes? The orange-diamond '
                'press does not count.')
    return color_manifest(shots, seed), case, question


def color_manifest(shots, seed):
    # All arms share the same editable prompt bindings and initial values.
    return dict(assets=[
        asset(shot, prompt + ' Apply the following appearance instruction where it applies. {{target.color}}',
              {KEY: 'Preserve the described original appearance.'}, seed + index)
        for index, (shot, prompt) in enumerate(shots.items())
    ])


def lamp_inputs(seed):
    target = 'the cream desk lamp with one large black hexagon on its cream base'
    shots = {
        'S1': f'Steady frontal close-up of {target} and its solid silver metal dome shade. '
              'Both the shade and the black hexagon are fully visible on a wooden desk.',
        'S2': 'Steady close-up of a different cream desk lamp with one large orange triangle '
              'on its cream base and a solid silver metal dome shade. Both are visible.',
        'S3': f'Steady three-quarter view of {target} and its solid silver metal dome shade. '
              'Both the shade and the black hexagon remain visible on a wooden desk.',
        'S4': 'Steady close-up of a closed black tool case alone on a shelf. No lamp is visible.',
    }
    edit = (
        f'Recolor only the silver metal dome shade of {target} to cobalt blue wherever visible. '
        'Preserve the cream base and black hexagon. Keep the orange-triangle lamp and all other '
        'objects unchanged. Do not add a lamp where none is visible.'
    )
    case = dict(id='hexagon-lamp', entity=f'silver metal dome shade of {target}',
                attribute='color', edit=edit, shots=shots, declared=['S1'])
    question = (
        'In any single source panel, do you see a solid silver metal dome shade on a cream '
        'desk lamp whose own cream base has a large black hexagon? The orange-triangle lamp '
        'and a shelf without a lamp do not count.'
    )
    return color_manifest(shots, seed), case, question


def radio_inputs(seed):
    target = 'a mint-green portable field radio with a large white X on its own front face'
    part = 'a thick yellow carry handle above the radio'
    shots = {
        'S1': f'Steady frontal close-up of {target} with {part}. '
              'Keep the entire handle and white X visible on a wooden workbench.',
        'S2': 'Steady frontal view of a different mint-green field radio with a large white '
              'circle on its front face and a thick yellow carry handle on top. Both are visible.',
        'S3': f'Steady three-quarter view of {target} with {part} on a wooden shelf. '
              'Keep the entire handle and white X visible.',
        'S4': 'Steady close-up of a closed maroon notebook alone on a shelf. No radio is visible.',
    }
    edit = (
        f'Recolor only the thick yellow carry handle of {target} to cobalt blue wherever visible. '
        'Preserve the mint-green body and white X. Leave the circle-marked radio and notebook '
        'unchanged. Do not add radios or handles where absent.'
    )
    question = (
        'In any single source panel, is there a thick yellow carry handle visibly attached '
        'to a mint-green field radio whose own front face has a large white X? '
        'A white circle or a mark on another object does not count.'
    )
    return color_manifest(shots, seed), dict(
        id=radio.STORY, entity=f'carry handle of {target}', attribute='color',
        edit=edit, shots=shots, declared=['S1'],
    ), question


def lunchbox_inputs(seed):
    target = 'a deep-purple hard-shell lunchbox with one large white five-point star on its front'
    part = 'a thick yellow carry handle attached to the top'
    shots = {
        'S1': f'Locked frontal product shot of {target} with {part}. '
              'Show the entire handle and front decal against a seamless light gray backdrop.',
        'S2': 'Locked frontal product shot of a deep-purple hard-shell lunchbox with one large '
              f'white triangle on its front and {part}. Show the entire handle and front decal '
              'against a seamless light gray backdrop.',
        'S3': f'Locked three-quarter product shot of {target} with {part}. '
              'Show the entire handle and front decal against a seamless light gray backdrop.',
        'S4': 'Locked product shot of a single empty white ceramic bowl centered on a seamless '
              'pale gray studio sweep. The smooth backdrop and tabletop fill the entire frame.',
    }
    edit = (
        f'Recolor only the thick yellow carry handle of {target} to cobalt blue wherever visible. '
        'Preserve the purple shell and white star. Keep the triangle-marked lunchbox, bowl '
        'and other contents unchanged. Do not add a lunchbox or handle where absent.'
    )
    question = (
        'In any single source panel, is there a thick yellow carry handle visibly attached '
        'to a deep-purple lunchbox whose own front face has a large white five-point star? '
        'A white triangle or a mark on another object does not count.'
    )
    return color_manifest(shots, seed), dict(
        id=radio.LUNCHBOX_STORY, entity=f'carry handle of {target}', attribute='color',
        edit=edit, shots=shots, declared=['S1'],
    ), question


def canister_inputs(seed):
    body = 'a teal cylindrical storage canister with a broad flat yellow lid'
    target = f'{body} and one large white diamond on its teal body'
    framing = ('The entire canister is centered and occupies about half the image height. '
               'Leave generous space above the lid and around the canister from the first frame. '
               'Seamless light-gray background. Camera and object stay still.')
    shots = {
        'S1': f'Locked medium product shot from slightly above the front of {target}. {framing}',
        'S2': f'Locked medium product shot of {body} and one large white circle on its teal body. {framing}',
        'S3': f'Locked three-quarter medium product shot from slightly above {target}. {framing}',
        'S4': 'Locked medium product shot of one smooth gray stone centered on a seamless pale '
              'gray studio sweep. The smooth backdrop and tabletop fill the entire frame.',
    }
    edit = (f'Recolor only the broad flat yellow lid of {target} to cobalt blue wherever visible. '
            'Preserve the teal body and white diamond. Keep the circle-marked canister, stone '
            'and other contents unchanged. Do not add a canister or lid where absent.')
    question = ('In any single source panel, is a broad flat yellow lid visibly on a teal '
                'canister whose own body has a large white diamond? '
                'A white circle or a mark on another object does not count.')
    return color_manifest(shots, seed), dict(
        id=radio.CANISTER_STORY, entity=f'lid of {target}', attribute='color',
        edit=edit, shots=shots, declared=['S1'],
    ), question


STORIES = {'striped-press': pilot_inputs, 'hexagon-lamp': lamp_inputs,
           radio.STORY: radio_inputs, radio.LUNCHBOX_STORY: lunchbox_inputs,
           radio.CANISTER_STORY: canister_inputs}
LAMP_ORACLE = {
    'source': {
        'S1': 'Silver dome shade and black hexagon visibly belong to the same lamp',
        'S2': 'Different silver-shaded lamp marked by an orange triangle',
        'S3': 'Silver dome shade and black hexagon visibly belong to the same lamp',
        'S4': 'Closed black tool case with no lamp',
    },
    'final': {
        'S1': 'Scarlet dome shade, cream base and black hexagon retained',
        'S2': 'Silver shade and orange triangle retained',
        'S3': 'Scarlet dome shade, cream base and black hexagon retained',
        'S4': 'Closed tool case retained, no lamp added',
    },
    'target_shots': ['S1', 'S3'],
    'declared_shots': ['S1'],
}


# Frame-0 contours measured once on the accepted seed-88101 source clips.
LAMP_REFERENCE_REGIONS = {
    'S1': (
        '10181e006a1a0d9eb0e7449fd4f4cdf0f33c4117eae776b2c4fac26bcb85759f',
        ((244, 201), (245, 186), (248, 171), (256, 152), (270, 137), (287, 123),
         (308, 109), (331, 98), (355, 90), (383, 85), (411, 82), (447, 82),
         (475, 84), (501, 89), (526, 97), (550, 107), (571, 121), (588, 137),
         (600, 154), (608, 174), (612, 201), (566, 200), (513, 199), (462, 198),
         (407, 198), (354, 199), (295, 200)),
    ),
    'S3': (
        'b6fbd63b16fda9d20dc4c6b4ae41b62aa1e1070aaa2fae88208367f8828b19cd',
        ((252, 242), (254, 222), (259, 201), (269, 179), (283, 158), (301, 138),
         (322, 118), (347, 100), (372, 87), (398, 77), (427, 72), (454, 71),
         (482, 74), (506, 81), (532, 92), (554, 107), (573, 125), (590, 147),
         (601, 169), (610, 193), (616, 217), (618, 246), (577, 249), (530, 250),
         (473, 250), (409, 250), (351, 248), (298, 246)),
    ),
}


# Post-hoc source-anchored diagnostic only. The original frame-0 eligibility failed.
LUNCHBOX_DIAGNOSTIC_SCOPE = 'post_hoc_reference_frame_sensitivity'
LUNCHBOX_DIAGNOSTIC_FRAMES = {'S1': 44, 'S3': 0}
LUNCHBOX_DIAGNOSTIC_SHA256 = {'S1': 'ae9a31b283c65ca70316f0c2dcd61f24fe3bc682beb441ffd571ac70eea99e34',
 'S2': '0ee9e1262c7f232d354d21429ff9a3d984b3019634216110ee7b4f96cfa77cdf',
 'S3': '17bb3a8d5ea30e057611241019227d8f35dce1daab35eebd78b68fdc104fa79f',
 'S4': '16d30582fdc9c1a6e12d1239e5571b9468f91d948b981cd832565bfe275e04b7'}
LUNCHBOX_DIAGNOSTIC_OUTLINES = {
    'S1': ((257,103), (258,88), (261,74), (267,61), (277,49), (291,39), (310,31),
           (332,24), (359,17), (392,12), (420,9), (448,9), (480,12), (509,17),
           (540,24), (563,33), (581,43), (592,55), (599,68), (605,85), (608,103),
           (607,144), (606,180), (605,216), (603,225), (598,228), (594,226),
           (594,190), (594,155), (595,116), (595,98), (592,80), (587,68),
           (579,59), (568,55), (555,53), (545,57), (533,58), (525,57), (514,52),
           (503,51), (491,55), (479,55), (470,54), (462,50), (452,49),
           (442,54), (430,55), (418,54), (408,49), (398,49), (387,52), (378,55),
           (366,54), (357,50), (348,50), (338,54), (326,57), (315,55), (305,51),
           (293,53), (281,61), (274,72), (271,87), (269,104), (269,133), (268,164),
           (269,196), (269,225), (262,225), (258,222), (257,211)),
    'S3': ((249,146), (251,128), (257,114), (265,102), (278,90), (294,79), (313,68),
           (337,57), (365,48), (394,40), (425,35), (455,34), (478,35), (495,40),
           (509,50), (520,63), (529,79), (536,97), (542,118), (544,132), (535,132),
           (525,124), (511,121), (506,106), (500,91), (493,77), (483,66), (472,59),
           (462,57), (454,59), (440,64), (425,70), (409,75), (389,83), (369,88),
           (351,94), (334,98), (316,99), (305,101), (294,108), (287,119), (283,132),
           (281,149), (282,168), (284,189), (288,211), (293,232), (297,255),
           (299,278), (297,292), (291,300), (282,303), (272,299), (266,290),
           (264,280), (263,261), (261,242), (257,220), (254,198), (251,179), (249,158)),
}


def make_conditioned_reference(clip, output, shot, expected, outline, region_name, frame_index=0):
    from run_egdr_reference_pair import HEIGHT, WIDTH, ffmpeg as rgb_ffmpeg, polygon_spans

    if checksum(clip) != expected:
        raise ValueError(f'{shot}: reference contour is calibrated to different source bytes')
    selector = ('-vf', rf'select=eq(n\,{frame_index})') if frame_index else ()
    raw = rgb_ffmpeg('-i', clip, '-map', '0:v:0', *selector, '-frames:v', 1, '-pix_fmt', 'rgb24',
                     '-threads', 1, '-f', 'rawvideo', 'pipe:1')
    if len(raw) != WIDTH * HEIGHT * 3:
        raise ValueError(f'{shot}: expected a {WIDTH}x{HEIGHT} frame at index {frame_index}')
    edited = bytearray(raw)
    changed = 0
    for y in range(HEIGHT):
        for start, end in polygon_spans(outline, y + 0.5):
            for x in range(int(start + 0.5), int(end + 0.5)):
                i = (y * WIDTH + x) * 3
                r, g, b = raw[i:i + 3]
                light = (r + 2 * g + b) // 4
                red = (min(255, light * 5 // 4), light * 3 // 16, light * 4 // 16)
                changed += tuple(raw[i:i + 3]) != red
                edited[i:i + 3] = bytes(red)
    if not changed:
        raise ValueError(f'{shot}: reference contour changed zero pixels')
    rgb_ffmpeg('-f', 'rawvideo', '-pixel_format', 'rgb24', '-video_size', f'{WIDTH}x{HEIGHT}',
               '-i', 'pipe:0', '-frames:v', 1, '-threads', 1, output, data=edited)
    return dict(sha256=checksum(output), source_shot=shot, source_video_sha256=expected,
                frame_index=frame_index, decoded_source_frame_sha256=hashlib.sha256(raw).hexdigest(),
                changed_pixels=changed, file=output.name, **{region_name: outline})


def prepare_conditioned_builder(root, source, backend, regions, story, *, reference_frames=None):
    from recovery_reference_comfy import check_backend, upload_frame
    from run_egdr_reference_pair import condition

    check_backend(backend)
    references = {}
    reference_frames = reference_frames or {}
    lamp = story == 'hexagon-lamp'
    region_name = {
        'hexagon-lamp': 'shade_outline',
        radio.CANISTER_STORY: 'lid_outline',
    }.get(story, 'handle_outline')
    reference_name = {
        radio.STORY: 'radio', radio.LUNCHBOX_STORY: 'lunchbox', radio.CANISTER_STORY: 'canister',
    }.get(story, 'lamp')
    for shot, (digest, outline) in regions.items():
        path = root / f'{shot}-{reference_name}-reference.png'
        references[shot] = make_conditioned_reference(
            root / 'source' / source[shot]['file'], path, shot, digest, outline, region_name,
            frame_index=reference_frames.get(shot, 0))
    for shot, entry in references.items():
        entry['provider_image'] = upload_frame(backend, root / entry['file'], entry['sha256'])
    write_json(root / 'reference_plan.json', references)

    def build(inputs, job):
        graph = build_h3(inputs, job)
        entry = references.get(inputs['asset'])
        if entry is None:
            return graph
        prompt = graph['5']['inputs']['prompt']
        scene, separator, _ = prompt.partition(' Apply the following appearance instruction')
        if not separator:
            raise ValueError('missing appearance slot')
        if lamp:
            scene = (scene.replace('solid silver metal dome shade', 'scarlet red metal dome shade')
                     + ' Keep the shade scarlet red from the first visible frame through the end. '
                       'Preserve the cream base, black hexagon, desk, and background.')
        elif story == radio.CANISTER_STORY:
            scene = (scene.replace('broad flat yellow lid', 'broad flat scarlet red lid')
                     + ' Keep the entire lid solid scarlet red through the end. '
                       'Preserve the teal body, white diamond, and background.')
        else:
            preserved = ('the purple shell, white star and background'
                         if story == radio.LUNCHBOX_STORY else 'the mint-green body, white X, and background')
            scene = (scene.replace('thick yellow carry handle', 'thick scarlet red carry handle')
                     + ' Keep the carry handle scarlet red from the first visible frame through the end. '
                     + f'Preserve {preserved}.')
        graph['5']['inputs']['prompt'] = scene
        return condition(graph, entry['provider_image'])
    return build


def wait_conditioned(folder, runtime, backend, build, predicate, timeout, poll):
    from run_egdr_conditioned_replay import step

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        step(folder, runtime, backend, build, max(0, 2 - len(runtime.unresolved_remote())))
        if predicate():
            return
        time.sleep(poll)
    raise TimeoutError(f'timed out: {folder}')


def apply_policy(runtime, case, selected, source, evidence, policy, expected_revision=1, *, continuous=False):
    target = case.get('target', 'S1')
    event_id = case['id'] if expected_revision == 1 else f"{case['id']}-{expected_revision}"
    event = dict(id=event_id, expected_revision=expected_revision, target=target, key=KEY,
                 value=case['edit'], scope='local')
    if policy in DEFERRED_POLICIES:
        event['evidence_candidates'] = sorted(set(source) - {target})
    elif policy == 'full' and continuous:
        # Assign the edit value to every authored binding without introducing
        # artificial readiness edges in the full-regeneration baseline.
        event.update(scope='global', method='full')
    else:
        repairs = [
            dict(child=shot, source_input_hash=source[shot]['input_hash'],
                 evidence_sha256=fingerprint(dict(shot=shot, policy=policy, evidence=evidence)))
            for shot in sorted(set(selected) - {target})
        ]
        event.update(state_repairs=repairs, method='attr_dag' if policy == 'worklist' else 'dair')
    return runtime.edit(event)


def submit_anchor(folder, runtime, backend, epoch, build=build_h3):
    events = pass_once(runtime, backend, build=build, max_submit=1)
    with (folder / 'worker.jsonl').open('a', encoding='utf-8') as out:
        out.write(json.dumps(dict(at=time.time(), events=events)) + '\n')
    if not any(j['asset'] == 'S1' and j['revision'] == epoch and j['status'] == 'submitted'
               for j in runtime.active()):
        raise RuntimeError(f'S1 epoch {epoch} did not reach the acknowledgement barrier')


def source_probes(root, sheets, case, question, args, log):
    probes = {}
    for shot, sheet in sheets.items():
        if shot in case['declared']:
            continue
        panels = []
        for i, (x, y) in enumerate(((0, 0), (768, 0), (0, 432), (768, 432)), 1):
            tile = root / f'{shot}-panel-{i}.png'
            ffmpeg('-i', sheet, '-vf', f'crop=768:432:{x}:{y}', '-frames:v', 1, '-threads', 1, tile)
            panels.append(tile)
        probes[shot] = capture_probe(args.ollama, args.vision_model, question, panels, root / f'probe-{shot}', log)
    return probes


def bind_oracle(labels, source, case):
    """Accept only complete human labels tied to the exact pre-edit clips."""
    if not isinstance(labels, dict) or set(labels) != {'target_shots', 'source_sha256'}:
        raise ValueError('oracle labels require target_shots and source_sha256')
    if labels['source_sha256'] != {shot: row['sha256'] for shot, row in source.items()}:
        raise ValueError('oracle labels do not match the frozen source bytes')
    targets = labels['target_shots']
    if (not isinstance(targets, list) or any(not isinstance(shot, str) for shot in targets)
            or len(set(targets)) != len(targets)
            or not set(case['declared']) <= set(targets) <= set(source)):
        raise ValueError('oracle targets must be unique source shots including every declared target')
    return {
        shot: ('match' if shot in targets else 'distinct',
               fingerprint(dict(kind='human_oracle', shot=shot, labels=labels)))
        for shot in sorted(set(source) - set(case['declared']))
    }


def run(args):
    backend = ComfyUI(args.server)
    queue = backend._request('/queue')
    if any(queue.get(key) for key in ('queue_running', 'queue_pending')):
        raise RuntimeError('the experiment requires an idle, dedicated ComfyUI queue')
    manifest, case, question = STORIES[args.story](args.seed)
    if args.frame_notes:
        notes = json.loads(args.frame_notes.read_text(encoding='utf-8'))
        if set(notes) != set(case['shots']) or any(not isinstance(note, str) or not note.strip()
                                                 for note in notes.values()):
            raise ValueError('provide one nonempty frame note per shot')
        case['shots'] = {shot: prompt + ' Frame observation: ' + notes[shot]
                         for shot, prompt in case['shots'].items()}
    args.output.mkdir(parents=True)
    if args.story == 'hexagon-lamp':
        # Keep the predeclared evaluation labels out of all model inputs.
        write_json(args.output / 'visual_oracle.json', LAMP_ORACLE)
    protocol_fields = ({
        'holdout_protocol_sha256': checksum(radio.HOLDOUTS[args.story][1]),
        'reference_regions_sha256': checksum(args.reference_regions) if args.reference_regions else None,
        'oracle_labels_sha256': checksum(args.oracle_labels) if args.oracle_labels else None,
    }
                       if args.story in radio.HOLDOUTS else {})
    if args.lunchbox_later_reference:
        protocol_fields.update(analysis_scope=LUNCHBOX_DIAGNOSTIC_SCOPE,
                               original_source_eligible=False,
                               original_source_failure='S1 grip cropped at frame 0',
                               reference_frame_indices=LUNCHBOX_DIAGNOSTIC_FRAMES)
    write_json(args.output / 'protocol.json', dict(story=args.story, seed=args.seed, manifest=manifest, case=case,
               question=question, policies=args.policies, text_model=args.text_model,
               vision_model=args.vision_model, frame_notes=bool(args.frame_notes), timeout=args.timeout,
               server=backend.base, continuous=args.continuous,
               lamp_reference_frames=args.lamp_reference_frames, oracle_labels=bool(args.oracle_labels or args.lunchbox_later_reference),
               **protocol_fields))
    if args.source_run:
        source_protocol = json.loads((args.source_run / 'protocol.json').read_text(encoding='utf-8'))
        if source_protocol['manifest'] != manifest or source_protocol['server'] != backend.base:
            raise ValueError('source run must use the same seed, manifest and backend')
        if not (args.source_run / 'source.db').is_file():
            raise ValueError('source database is missing')
    original = AssetRuntime((args.source_run or args.output) / 'source.db')
    try:
        if not args.source_run:
            original.initialize(manifest)
            capture_environment(original, backend)
            wait_for(args.output, original, backend, 'videoflow', lambda: original.snapshot()['timeline'],
                     time.monotonic() + args.timeout, args.poll)
        source_snapshot = original.snapshot()
        if source_snapshot['revision'] != 1 or not source_snapshot['timeline']:
            raise ValueError('source run must have published revision 1')
        before = archive_current(original, args.output / 'source', backend.base, probe=True)
        if args.source_run:
            if args.story in radio.HOLDOUTS:
                radio.check_protocol(args.source_run)
            frozen = json.loads((args.source_run / 'source' / 'revision-1.json').read_text())
            if (before['timeline_hash'] != frozen['timeline_hash']
                    or {row['asset']: row['sha256'] for row in before['assets']}
                    != {row['asset']: row['sha256'] for row in frozen['assets']}):
                raise RuntimeError('provider source bytes differ from the frozen run')
        source = {row['asset']: row for row in before['assets']}
        oracle_verdicts = {}
        if args.lunchbox_later_reference:
            labels = dict(target_shots=list(radio.TARGETS), source_sha256=LUNCHBOX_DIAGNOSTIC_SHA256)
        else:
            labels = json.loads(args.oracle_labels.read_text(encoding='utf-8')) if args.oracle_labels else None
        if labels is not None:
            oracle_verdicts = bind_oracle(labels, source, case)
            write_json(args.output / 'oracle_labels.json', labels)
        sheets = {shot: args.output / 'source' / f'{shot}.jpg' for shot in source}
        frames = {shot: make_sheet(args.output / 'source' / row['file'], sheets[shot])
                  for shot, row in source.items()}
        if any(frames[shot]['clip_sha256'] != row['sha256'] for shot, row in source.items()):
            raise RuntimeError('evidence frames do not match the frozen source clips')
        if args.source_only:
            return
        regions = LAMP_REFERENCE_REGIONS if args.lamp_reference_frames else {}
        reference_frames = None
        if args.lunchbox_later_reference:
            regions = {shot: (LUNCHBOX_DIAGNOSTIC_SHA256[shot], outline)
                       for shot, outline in LUNCHBOX_DIAGNOSTIC_OUTLINES.items()}
            reference_frames = LUNCHBOX_DIAGNOSTIC_FRAMES
        elif args.reference_regions:
            regions = json.loads(args.reference_regions.read_text(encoding='utf-8'))
            if set(regions) != set(radio.TARGETS):
                raise ValueError('expected the two preregistered conditioned target shots')
        final_builder = (prepare_conditioned_builder(
            args.output, source, backend, regions, args.story, reference_frames=reference_frames)
                         if regions else build_h3)
        raw = []
        started = time.monotonic()
        if args.continuous:
            with (args.output / 'responses.jsonl').open('x', encoding='utf-8') as responses:
                probes = (source_probes(args.output, sheets, case, question, args, responses)
                          if 'online_egdr' in args.policies else {})
                responses.flush()
            raw = [json.loads(line) for line in (args.output / 'responses.jsonl').read_text().splitlines()]
            choices = dict(attr_dag=set(case['declared']), full=set(source),
                           online_egdr=set(case['declared']), oracle_egdr=set(case['declared']))
            audit = dict(probes=probes)
        else:
            with (args.output / 'responses.jsonl').open('x', encoding='utf-8') as responses:
                def log(row):
                    raw.append(row)
                    responses.write(json.dumps(row, ensure_ascii=False) + '\n')
                    responses.flush()

                choices, audit = collect_selections(
                    case, sheets, args.ollama, args.text_model, args.vision_model, log,
                    visibility_question=question, verify_visual=True,
                )
            if 'union_plus_verified' in choices:
                choices['union_plus_unresolved'] = (choices['union_plus_verified']
                                                    | set(audit['visual_unknown'])
                                                    | set(audit['verification_unknown']))
            if audit['visual_invalid'] or audit['verification_invalid']:
                raise RuntimeError('invalid evidence replies; inspect responses.jsonl before rendering arms')
        evidence_seconds = time.monotonic() - started
        write_json(args.output / 'evidence.json', dict(frames=frames, audit=audit,
                   paired_probe_wall_seconds=evidence_seconds, responses_sha256=fingerprint(raw),
                   selections={key: sorted(value) for key, value in choices.items()}))
        evidence = dict(frames=frames, responses=raw, case=case, question=question,
                        text_model=args.text_model, vision_model=args.vision_model)
        vlm_decisions = {'yes': 'match', 'no': 'distinct', 'unknown': 'fallback'}
        vlm_verdicts = {
            shot: (vlm_decisions[probe['decision']],
                   fingerprint(dict(shot=shot, probe=probe, frame=frames[shot], evidence=evidence)))
            for shot, probe in probes.items()
        } if args.continuous else {}
        rows = []
        for policy in args.policies:
            folder = args.output / policy
            folder.mkdir()
            runtime = AssetRuntime(folder / 'project.db')
            try:
                original.db.backup(runtime.db)
                selected = choices[POLICIES[policy]]
                first = plan = apply_policy(runtime, case, selected, source, evidence, policy,
                                            continuous=args.continuous)
                repairs, obsolete_evidence_rejected = [], False
                if args.continuous:
                    submit_anchor(folder, runtime, backend, 2)
                    runtime.close()
                    runtime = AssetRuntime(folder / 'project.db')
                    red = dict(case, edit=case['edit'].replace('cobalt blue', 'scarlet red'))
                    plan = apply_policy(runtime, red, selected, source, evidence, policy,
                                        expected_revision=2, continuous=True)
                    submit_anchor(folder, runtime, backend, 3, final_builder)
                    if policy in DEFERRED_POLICIES:
                        verdicts = oracle_verdicts if policy == 'oracle_egdr' else vlm_verdicts
                        for shot, (decision, proof) in verdicts.items():
                            if not obsolete_evidence_rejected:
                                try:
                                    runtime.resolve_evidence(2, shot, source[shot]['input_hash'], proof, decision)
                                except Conflict:
                                    obsolete_evidence_rejected = True
                                else:
                                    raise RuntimeError('superseded evidence was accepted')
                            repairs.append(runtime.resolve_evidence(
                                3, shot, source[shot]['input_hash'], proof, decision))
                    write_json(folder / 'repair_trace.json', dict(first=first, second=plan, repairs=repairs,
                               obsolete_evidence_rejected=obsolete_evidence_rejected))
                def published():
                    if args.continuous and runtime.unresolved_remote():
                        return None
                    return runtime.snapshot()['timeline']

                if regions:
                    wait_conditioned(folder, runtime, backend, final_builder, published, args.timeout, args.poll)
                else:
                    wait_for(folder, runtime, backend, 'videoflow', published,
                             time.monotonic() + args.timeout, args.poll)
                after = archive_current(runtime, folder / 'media', backend.base, probe=True)
                reused = sorted(row['asset'] for row in after['assets']
                                if all(row[field] == source[row['asset']][field]
                                       for field in ('input_hash', 'sha256', 'output')))
                final_selection = set(plan['regenerate']).union(*(r['regenerate'] for r in repairs))
                if set(reused) != set(source) - final_selection:
                    raise RuntimeError('published reuse disagrees with the committed plan')
                snapshot = runtime.snapshot()
                jobs = [job for job in snapshot['jobs'] if job['revision'] > 1]
                history = summarize_inspections(runtime)
                measured_jobs = [job for job in jobs if job['submission_intents']] if args.continuous else jobs
                job_ids = {job['id'] for job in measured_jobs}
                edited_history = [item for item in history['jobs'] if item['job'] in job_ids]
                spans = [item['execution_span_seconds'] for item in edited_history
                         if item['execution_span_seconds'] is not None]
                revision = plan['revision']
                edited_at = runtime.db.execute(
                    'SELECT applied_at FROM vf_events WHERE revision=?', (revision,)).fetchone()[0]
                published_at = runtime.db.execute(
                    'SELECT published_at FROM vf_timelines WHERE revision=?', (revision,)).fetchone()[0]
                row = dict(policy=policy, plan=plan, reused=reused, video_outputs=len(jobs),
                           submission_intents=sum(job['submission_intents'] for job in jobs),
                           publication_seconds_after_edit=published_at - edited_at,
                           execution_spans_observed=len(spans),
                           provider_execution_span_seconds=sum(spans) if len(spans) == len(measured_jobs) else None,
                           provider_history=history, quality='requires_independent_review')
                if args.continuous:
                    publications = [r[0] for r in runtime.db.execute('SELECT revision FROM vf_timelines ORDER BY revision')]
                    events = [event for line in (folder / 'worker.jsonl').read_text().splitlines()
                              for event in json.loads(line)['events']]
                    stale = [event['job'] for event in events if event.get('result') == 'stale']
                    if publications != [1, 3] or not stale:
                        raise RuntimeError('continuous arm did not exercise obsolete completion and revision-3 publication')
                    row.update(repairs=repairs, final_selection=sorted(final_selection), publications=publications,
                               scheduled_jobs=len(jobs), video_outputs=sum(j['remote_state'] == 'succeeded'
                                                                          for j in measured_jobs),
                               stale_jobs=stale, obsolete_evidence_rejected=obsolete_evidence_rejected)
                if regions:
                    references = json.loads((args.output / 'reference_plan.json').read_text())
                    bindings = {}
                    for job in jobs:
                        if job['status'] != 'succeeded':
                            continue
                        workflow = json.loads(job['execution_json'])
                        expected = references.get(job['asset'])
                        if expected is None:
                            if 'first_frame' in workflow['5']['inputs']:
                                raise RuntimeError(f"unexpected reference for {job['asset']}")
                            continue
                        if (workflow['5']['inputs'].get('first_frame') != ['15', 0]
                                or workflow['15']['inputs']['image'] != expected['provider_image']):
                            raise RuntimeError(f"wrong final reference for {job['asset']}")
                        bindings[job['asset']] = expected['provider_image']
                    if set(bindings) != final_selection.intersection(references):
                        raise RuntimeError('conditioned final renders disagree with the committed plan')
                    row['reference_bindings'] = bindings
                if args.story == radio.CANISTER_STORY:
                    uses_vlm = policy == 'online_egdr'
                    row['vlm_calls'] = sum(p['calls'] for p in probes.values()) if uses_vlm else 0
                    row['vlm_wall_seconds'] = evidence_seconds if uses_vlm else 0
                    row['gpu_seconds'] = history['gpu_seconds']
                write_json(folder / 'report.json', row)
                rows.append(row)
            finally:
                runtime.close()
        report = dict(evidence='real_H3_unreviewed_quality',
                      source_timeline_hash=before['timeline_hash'], rows=rows)
        if args.story == radio.CANISTER_STORY:
            report.update(analysis_scope='prespecified_single_story',
                          reference_frame_indices=dict.fromkeys(radio.TARGETS, 0))
        if args.lunchbox_later_reference:
            report.update(analysis_scope=LUNCHBOX_DIAGNOSTIC_SCOPE, original_source_eligible=False,
                          reference_frame_indices=LUNCHBOX_DIAGNOSTIC_FRAMES)
        write_json(args.output / 'report.json', report)
    finally:
        original.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--ollama', default='http://127.0.0.1:11434')
    parser.add_argument('--text-model', default='qwen2.5:14b')
    parser.add_argument('--vision-model', default='qwen2.5vl:7b')
    parser.add_argument('--policies', nargs='+', choices=tuple(POLICIES),
                        default=['declared', 'checklist', 'raw_union', 'egdr', 'full'])
    parser.add_argument('--oracle-labels', type=Path,
                        help='pre-edit human target labels and SHA-256 hashes for every source shot')
    parser.add_argument('--continuous', action='store_true', help='run the two-edit deferred-repair experiment')
    parser.add_argument('--story', choices=tuple(STORIES), default='striped-press')
    parser.add_argument('--seed', type=int, default=88001)
    parser.add_argument('--lamp-reference-frames', action='store_true',
                        help='use the verified seed-88101 shade contours for final target renders')
    parser.add_argument('--reference-regions', type=Path,
                        help='verified holdout source digests and first-frame handle contours')
    parser.add_argument('--lunchbox-later-reference', action='store_true',
                        help='run the post-hoc, hash-bound frame-44 reference diagnostic on the original source')
    parser.add_argument('--frame-notes', type=Path,
                        help='optional independent observations from this seed, keyed by shot')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--source-only', action='store_true',
                        help='generate and archive only the shared pre-edit source')
    source.add_argument('--source-run', type=Path, help='reuse a completed --source-only run')
    parser.add_argument('--timeout', type=float, default=1800)
    parser.add_argument('--poll', type=float, default=3)
    args = parser.parse_args()
    if not all(0 < value < float('inf') for value in (args.poll, args.timeout)):
        parser.error('timeout and poll must be finite and positive')
    if args.lamp_reference_frames and not (args.story == 'hexagon-lamp' and args.seed == 88101
                                            and args.source_run and args.continuous):
        parser.error('--lamp-reference-frames requires --story hexagon-lamp --seed 88101 --source-run and --continuous')
    if args.lunchbox_later_reference and not (
        args.story == radio.LUNCHBOX_STORY and args.source_run and not args.oracle_labels
        and not args.reference_regions
    ):
        parser.error('--lunchbox-later-reference requires the original lunchbox source and its built-in labels')
    if args.story in radio.HOLDOUTS:
        seed, _ = radio.HOLDOUTS[args.story]
        if (args.seed, args.timeout, args.poll) != (seed, 1800, 3) or args.frame_notes:
            parser.error('the holdout requires its frozen seed, timeout, poll and no frame notes')
        reference_ready = args.lunchbox_later_reference or (args.oracle_labels and args.reference_regions)
        if not args.source_only and not (args.source_run and args.continuous and reference_ready
                                         and tuple(args.policies) == radio.POLICIES
                                         and args.vision_model == 'qwen2.5vl:7b-ctx8k'):
            parser.error('the holdout requires the frozen four-arm conditioned protocol')
    elif args.reference_regions:
        parser.error('--reference-regions is only supported by the conditioned holdouts')
    continuous_policies = {'worklist', 'full'} | DEFERRED_POLICIES
    if args.continuous and not set(args.policies) <= continuous_policies:
        parser.error('--continuous supports worklist, online_egdr, oracle_egdr, and full')
    if not args.continuous and DEFERRED_POLICIES.intersection(args.policies):
        parser.error('online_egdr and oracle_egdr require --continuous')
    if 'oracle_egdr' in args.policies:
        if not (args.oracle_labels or args.lunchbox_later_reference) or not args.source_run:
            parser.error('oracle_egdr requires --oracle-labels and a frozen --source-run')
    elif args.oracle_labels:
        parser.error('--oracle-labels requires oracle_egdr in --policies')
    if len(set(args.policies)) != len(args.policies):
        parser.error('policies must be unique')
    if not all(shutil.which(command) for command in ('ffmpeg', 'ffprobe')):
        parser.error('ffmpeg and ffprobe must be available')
    run(args)


if __name__ == '__main__':
    main()
