"""Compare original versus locally recolored H3 conditioning images on two archived shots."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from artifacts import checksum, write_json
from comfyui import ComfyUI
from recovery_reference_comfy import check_backend, upload_frame
from run_egdr_prompt_pair import load_targets, render_pair_member


WIDTH, HEIGHT = 864, 480
SEEDS = {'S1': 88001, 'S2': 88002}
# Zero-based decoded frame indices. Polygons trace the colored wheel material;
# gray hubs, openings, housing, tools and the operator are excluded.
SPECS = {
    'S1': (24, (
        ((198, 99), (232, 59), (280, 24), (332, 6), (366, 0), (535, 0),
         (586, 15), (635, 44), (671, 90), (706, 151), (724, 212),
         (741, 286), (736, 344), (725, 412), (701, 459), (686, 480),
         (574, 480), (587, 442), (598, 404), (607, 360), (611, 305),
         (602, 254), (590, 207), (568, 168), (545, 136), (512, 114),
         (472, 95), (430, 85), (390, 81), (346, 88), (319, 93),
         (293, 89), (255, 88)),
        ((420, 83), (456, 88), (500, 104), (492, 147), (489, 180),
         (493, 211), (513, 230), (522, 245), (502, 258), (469, 211),
         (440, 184), (416, 163)),
        ((502, 238), (536, 244), (575, 242), (594, 233), (608, 256),
         (612, 293), (575, 296), (542, 308), (531, 323), (519, 288)),
    )),
    'S2': (60, (
        ((208, 385), (226, 361), (260, 343), (300, 333), (353, 326),
         (421, 324), (489, 328), (544, 339), (588, 359), (606, 381),
         (609, 404), (603, 427), (579, 446), (546, 459), (502, 471),
         (459, 477), (351, 477), (298, 468), (254, 455), (220, 436),
         (207, 414), (204, 397)),
    )),
}


def polygon_spans(polygon, y):
    edges = zip(polygon, polygon[1:] + polygon[:1])
    crossings = sorted(x + (y - ya) * (xb - x) / (yb - ya)
                       for (x, ya), (xb, yb) in edges
                       if min(ya, yb) <= y < max(ya, yb))
    return zip(crossings[::2], crossings[1::2])


def recolor(pixels, polygons):
    """Shift amber wheel pixels toward red while retaining shading and highlights."""
    edited = bytearray(pixels)
    for y in range(HEIGHT):
        for polygon in polygons:
            for start, end in polygon_spans(polygon, y + 0.5):
                for x in range(max(0, int(start + 0.5)), min(WIDTH, int(end + 0.5))):
                    i = 3 * (y * WIDTH + x)
                    r, g, b = pixels[i:i + 3]
                    # Reject neutral highlights, gray hardware and non-amber surfaces.
                    strength = max(0, min(28, r - g - 8, r - b - 22, 128 - b))
                    if strength:
                        edited[i] = min(255, r + strength // 3)
                        edited[i + 1] = g - max(0, g - (b * 3 // 4 + 10)) * strength // 28
                        edited[i + 2] = min(255, b + strength // 4)
    return bytes(edited)


def ffmpeg(*args, data=None):
    return subprocess.run(
        ['ffmpeg', '-nostdin', '-v', 'error', *map(str, args)], input=data,
        stdout=subprocess.PIPE, check=True, timeout=90,
    ).stdout


def prepare_images(source, output, shot, asset):
    video = source / asset['file']
    if checksum(video) != asset['sha256']:
        raise ValueError(f'{shot}: source video does not match the revision-1 manifest')
    index, polygons = SPECS[shot]
    pixels = ffmpeg('-i', video, '-map', '0:v:0', '-vf', f'select=eq(n\\,{index})',
                    '-frames:v', 1, '-pix_fmt', 'rgb24', '-threads', 1, '-f', 'rawvideo', 'pipe:1')
    if len(pixels) != WIDTH * HEIGHT * 3:
        raise ValueError(f'{shot}: expected one {WIDTH}x{HEIGHT} RGB frame')
    red = recolor(pixels, polygons)
    changed = sum(pixels[i:i + 3] != red[i:i + 3] for i in range(0, len(pixels), 3))
    if not changed:
        raise ValueError(f'{shot}: the recolor mask changed zero pixels')
    images = {}
    for arm, data in (('original_frame', pixels), ('red_wheel_frame', red)):
        path = output / f'{shot}-{arm}.png'
        ffmpeg('-f', 'rawvideo', '-pixel_format', 'rgb24', '-video_size', f'{WIDTH}x{HEIGHT}',
               '-i', 'pipe:0', '-frames:v', 1, '-threads', 1, path, data=data)
        images[arm] = dict(file=path.name, sha256=checksum(path),
                           rgb_sha256=hashlib.sha256(data).hexdigest())
    return dict(source_video_sha256=asset['sha256'], source_revision=1,
                decoded_frame_index=index, wheel_polygons=polygons, changed_pixels=changed,
                images=images)


def condition(workflow, image_name):
    result = copy.deepcopy(workflow)
    result['15'] = {'class_type': 'LoadImage', 'inputs': {'image': image_name}}
    result['5']['inputs']['first_frame'] = ['15', 0]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--timeout', default=1800, type=float)
    parser.add_argument('--poll', default=3, type=float)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if not all(0 < n < float('inf') for n in (args.timeout, args.poll)):
        parser.error('timeout and poll must be finite and positive')
    if not shutil.which('ffmpeg'):
        parser.error('ffmpeg must be available')
    source = args.source_run / 'source'
    manifest = json.loads((source / 'revision-1.json').read_text(encoding='utf-8'))
    assets = {item['asset']: item for item in manifest['assets']}
    if any(assets[shot]['revision'] != 1 for shot in SPECS):
        raise ValueError('expected revision-1 source videos for both shots')
    workflows = {
        shot: variants['final_state']
        for shot, _, _, variants in load_targets(args.source_run, compare_final_state=True)
    }
    for shot, workflow in workflows.items():
        inputs = workflow['5']['inputs']
        if (inputs['width'], inputs['height']) != (WIDTH, HEIGHT) or (
                workflow['9']['inputs']['noise_seed'] != SEEDS[shot]):
            raise ValueError(f'{shot}: unexpected archived workflow dimensions or seed')
        if '15' in workflow or 'first_frame' in inputs:
            raise ValueError(f'{shot}: archived workflow already binds a first frame')
    args.output.mkdir(parents=True)
    frames = {shot: prepare_images(source, args.output, shot, assets[shot]) for shot in SPECS}
    order = ('original_frame', 'red_wheel_frame')
    protocol = dict(
        evidence='paired_H3_first_frame_conditioning_diagnostic',
        frames=frames, seeds=SEEDS, order={'S1': order, 'S2': order[::-1]},
        prompts={shot: workflow['5']['inputs']['prompt'] for shot, workflow in workflows.items()},
        quality='requires_full_clip_review_against_source',
    )
    write_json(args.output / 'protocol.json', protocol)
    if args.prepare_only:
        print(f'Reference images and protocol written to {args.output}')
        return

    backend = ComfyUI(args.server)
    check_backend(backend)
    rows = []
    for shot in SPECS:
        for arm in protocol['order'][shot]:
            reference = frames[shot]['images'][arm]
            provider_image = upload_frame(backend, args.output / reference['file'], reference['sha256'])
            print(f'START {shot} {arm}', flush=True)
            row = render_pair_member(backend, condition(workflows[shot], provider_image),
                                     args.output, shot, arm, args.timeout, args.poll)
            row['conditioning_image'] = reference
            rows.append(row)
            write_json(args.output / 'report.json', dict(protocol=protocol, rows=rows,
                       completed=len(rows), planned=4))
            print(f'DONE {shot} {arm}', flush=True)


if __name__ == '__main__':
    main()
