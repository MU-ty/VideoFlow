"""Render frozen pre-edit stories and export a frame pack for independent review."""
import argparse
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import time
from zipfile import ZIP_DEFLATED, ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from video_edit_ablation import (
    AssetRuntime, ComfyUI, archive_current, asset, capture_environment, wait_for, write_json,
)
from vision_probe import FRACTIONS, make_sheet


SUITCASE = 'a teal hard-shell suitcase with a large white triangle decal and a yellow front clasp'
SYNTH = 'a compact cream synthesizer with a black stripe, red pitch wheel, and silver master knob'
STORIES = {
    'station_luggage': [
        f'Front-facing medium shot of {SUITCASE} beside a ticket counter. '
        'The clasp and decal are clearly visible. An attendant sets the suitcase upright.',
        f'Close-up of an attendant pressing the yellow front clasp on {SUITCASE} on the same '
        'station counter. Keep the clasp and a portion of the white triangle in focus.',
        f'Rear close-up of {SUITCASE}. The back shell fills the frame while the attendant extends '
        'the black telescoping handle. The front face is out of view.',
        'Front-facing shot of a different teal hard-shell suitcase with a white circular decal '
        'and a yellow clasp, on an unrelated baggage cart. Only this suitcase appears.',
    ],
    'synth_rehearsal': [
        f'Medium shot of {SYNTH} on a rehearsal desk. A musician plays the keys. '
        'Show the whole control panel sharply, including the wheel and knob.',
        f'Close-up of the left end of {SYNTH}. The musician moves the red pitch wheel. '
        'Keep the right side of the instrument out of frame.',
        f'Close-up of the right end of {SYNTH}. The musician adjusts the silver master knob. '
        'Keep the far-left end of the instrument out of frame.',
        'Close-up of a different all-black synthesizer with a white checker decal and a red '
        'pitch wheel. Only this second instrument appears on a separate workbench.',
    ],
}
EDITS = {
    'station_luggage': [
        ('L1', 'front clasp', 'color',
         'Recolor the yellow front clasp on the teal suitcase with a white triangle decal to orange. '
         'Keep the clasp of the circular-decal suitcase unchanged.'),
        ('L2', 'telescoping handle', 'color',
         'Recolor only the telescoping handle on the triangle-decal suitcase from black to silver.'),
        ('L3', 'underside serial plate', 'color',
         'Recolor only the serial-number plate on the underside of the triangle-decal suitcase to blue.'),
    ],
    'synth_rehearsal': [
        ('M1', 'pitch wheel', 'color',
         'Recolor only the red pitch wheel of the cream synthesizer with the black stripe to blue. '
         'Keep the wheel of the black synthesizer unchanged.'),
        ('M2', 'master knob', 'color',
         'Recolor only the silver master-volume knob of the cream synthesizer to matte black.'),
        ('M3', 'underside battery cover', 'color',
         'Recolor only the battery compartment cover on the underside of the cream synthesizer to yellow.'),
    ],
}
MICROSCOPE = 'a cream microscope with two teal stripes on its upright arm'
SEWING_MACHINE = 'a sky-blue sewing machine with a white zigzag painted on its front'
HOLDOUT_STORIES = {
    'microscope_lab': [
        f'Steady three-quarter view of {MICROSCOPE} on a lab bench. Show the large red '
        'ridged focus wheel on the side of the arm and the slim red slide clip on the black stage.',
        f'Side macro shot of {MICROSCOPE}. A hand slowly turns the large red ridged focus wheel '
        'under the arm. Keep the two teal stripes and the wheel in the same frame.',
        f'Tight view of the black slide stage of {MICROSCOPE}. Show the narrow red slide clip '
        'holding the glass slide. The focus wheel is outside the frame.',
        'Side view of a different cream microscope with a large orange diamond sticker on its '
        'upright arm, no teal stripes, and a large red ridged focus wheel.',
    ],
    'sewing_workbench': [
        f'Steady front view of {SEWING_MACHINE} on a workbench. Show the white circular stitch '
        'selector with black tick marks and the small red rectangular lever directly below it.',
        f'Front macro shot of {SEWING_MACHINE}. A hand touches the small red rectangular '
        'lever below the white stitch selector. Keep the white zigzag visible.',
        f'Right-side view of {SEWING_MACHINE}. A hand turns the large white side handwheel. '
        'The front control panel is out of frame.',
        'Front view of a different sky-blue sewing machine with a white crescent decal '
        'and no zigzag. Show its white circular selector and small red rectangular lever.',
    ],
}
HOLDOUT_EDITS = {
    'microscope_lab': [
        ('H1', 'ridged focus wheel', 'color',
         'Recolor only the large red ridged focus wheel on the side of the cream microscope '
         'with two teal stripes to blue. Leave the orange-diamond microscope unchanged.'),
        ('H2', 'slide clip', 'color',
         'Recolor only the narrow red slide clip beside the glass slide on the black stage '
         'of the two-stripe microscope to yellow. Leave the red focus wheel unchanged.'),
        ('H3', 'underside serial plate', 'color',
         'Recolor only the serial-number plate on the underside of the two-stripe microscope '
         'base to purple. Leave all labels on the top and sides unchanged.'),
    ],
    'sewing_workbench': [
        ('H4', 'front stitch selector', 'color',
         'Recolor only the round white front stitch selector with black tick marks on the '
         'zigzag-marked sewing machine to pink. Leave the white side handwheel unchanged.'),
        ('H5', 'rectangular reverse lever', 'color',
         'Recolor only the small red rectangular lever directly below the front stitch '
         'selector on the zigzag-marked sewing machine to green. Leave the crescent-marked machine unchanged.'),
        ('H6', 'underside service hatch', 'color',
         'Recolor only the service hatch on the underside of the zigzag-marked sewing machine '
         'to gold. Leave its front panel and side unchanged.'),
    ],
}
SUITES = {
    'pilot': (STORIES, EDITS, 84001),
    'holdout': (HOLDOUT_STORIES, HOLDOUT_EDITS, 85001),
}


def protocol(server, suite='pilot'):
    stories, edits_by_story, seed_start = SUITES[suite]
    return dict(server=server, sample_fractions=FRACTIONS, stories={
        story: dict(assets=[
            asset(f'S{j}', prompt, {}, seed_start + i * 100 + j)
            for j, prompt in enumerate(prompts, 1)
        ]) for i, (story, prompts) in enumerate(stories.items())
    }, edits={
        story: [dict(id=case, entity=entity, attribute=attribute, edit=edit, declared=[])
                for case, entity, attribute, edit in edits]
        for story, edits in edits_by_story.items()
    })


def render_story(folder, backend, manifest, timeout, poll):
    folder.mkdir(exist_ok=True)
    runtime = AssetRuntime(str(folder / 'project.db'))
    try:
        if not runtime.db.execute('SELECT 1 FROM vf_project').fetchone():
            runtime.initialize(manifest)
            capture_environment(runtime, backend)
        snapshot = runtime.snapshot()
        if snapshot['revision'] != 1:
            raise ValueError(f'{folder.name}: expected the pre-edit revision')
        failures = [job['asset'] for job in snapshot['jobs'] if job['status'] == 'failed']
        if failures:
            raise RuntimeError(f'{folder.name}: failed shots {failures}; inspect worker.jsonl')
        if snapshot['timeline'] is None:
            wait_for(folder, runtime, backend, 'videoflow',
                     lambda: runtime.snapshot()['timeline'], time.monotonic() + timeout, poll)
        return archive_current(runtime, folder / 'media', backend.base)
    finally:
        runtime.close()


def prepare(output, backend, timeout, poll, *, suite='pilot'):
    # Preserve a generation plan before any H3 submission; retries reuse its job ledger.
    output.mkdir(parents=True, exist_ok=True)
    plan_path = output / 'generation-plan.json'
    frozen = json.loads(json.dumps(protocol(backend.base, suite)))
    if plan_path.exists():
        if json.loads(plan_path.read_text(encoding='utf-8')) != frozen:
            raise ValueError('existing output belongs to a different generation plan or server')
    else:
        if any(output.iterdir()):
            raise ValueError('output must be empty or belong to this generation plan')
        write_json(plan_path, frozen)
    review = {}
    for story, manifest in frozen['stories'].items():
        folder = output / story
        print(f'Preparing {story}', flush=True)
        archive = render_story(folder, backend, manifest, timeout, poll)
        review[story] = {}
        with TemporaryDirectory(dir=folder) as temp:
            for shot in archive['assets']:
                clip = folder / 'media' / shot['file']
                sheet = folder / (shot['asset'] + '.jpg')
                temporary = Path(temp) / sheet.name
                frame = make_sheet(clip, temporary)
                if frame['clip_sha256'] != shot['sha256']:
                    raise ValueError(f'archive hash mismatch: {clip}')
                temporary.replace(sheet)
                review[story][shot['asset']] = dict(
                    clip=str(clip.relative_to(output)), sheet=str(sheet.relative_to(output)), **frame,
                )
    index = output / 'review-index.json'
    write_json(index, dict(stories=review, sample_fractions=FRACTIONS))
    pack = output / 'vision-story-review.zip'
    with TemporaryDirectory(dir=output) as temp:
        temporary = Path(temp) / pack.name
        with ZipFile(temporary, 'w', compression=ZIP_DEFLATED) as bundle:
            bundle.write(index, index.name)
            for shots in review.values():
                for shot in shots.values():
                    bundle.write(output / shot['sheet'], shot['sheet'])
        temporary.replace(pack)
    print(f'Review pack: {pack}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--suite', choices=tuple(SUITES), default='pilot')
    parser.add_argument('--timeout', type=float, default=3600, help='seconds per story')
    parser.add_argument('--poll', type=float, default=3)
    args = parser.parse_args()
    if not all(0 < n < float('inf') for n in (args.timeout, args.poll)):
        parser.error('--timeout and --poll must be finite and positive')
    if not all(shutil.which(cmd) for cmd in ('ffmpeg', 'ffprobe')):
        parser.error('ffmpeg and ffprobe must be available')
    prepare(args.output, ComfyUI(args.server), args.timeout, args.poll, suite=args.suite)


if __name__ == '__main__':
    main()
