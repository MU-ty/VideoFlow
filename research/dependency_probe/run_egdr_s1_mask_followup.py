"""Render one S1 follow-up with the inner rim and spokes corrected in its reference."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from run_egdr_reference_pair import (
    HEIGHT, WIDTH, ComfyUI, check_backend, checksum, condition, ffmpeg,
    polygon_spans, render_pair_member, upload_frame, write_json,
)


PREVIOUS_RGB = 'efb867b6e8d9bdbc0a572ada510946a116b4e0da51453d04e75058f6442bbeb2'
# Coordinates are calibrated to the archived 864 x 480 reference, not the rendered clip.
RESIDUAL_REGIONS = (
    ((483, 104), (521, 111), (552, 139), (577, 167), (591, 195),
     (602, 225), (600, 243), (587, 242), (576, 220), (566, 195),
     (548, 170), (525, 150), (502, 131), (487, 123)),
    ((535, 300), (551, 288), (578, 285), (610, 281), (621, 297),
     (616, 317), (591, 323), (560, 326), (542, 330)),
    ((476, 186), (494, 198), (504, 211), (518, 225), (537, 236),
     (542, 247), (530, 253), (512, 243), (496, 230), (483, 213)),
)


def repair_residue(pixels):
    result = bytearray(pixels)
    changed = 0
    for y in range(HEIGHT):
        for region in RESIDUAL_REGIONS:
            for start, end in polygon_spans(region, y + 0.5):
                for x in range(int(start + 0.5), int(end + 0.5)):
                    i = 3 * (y * WIDTH + x)
                    r, g, b = pixels[i:i + 3]
                    # Only the residual amber pigment, not silver, skin, or neutral highlights.
                    if r - g >= 20 and g - b >= 12 and b <= 38:
                        result[i:i + 3] = (r, min(g, max(b, r // 9)), max(b, r // 16))
                        changed += 1
    return bytes(result), changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pair-run', required=True, type=Path)
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
    archived = json.loads((args.pair_run / 'report.json').read_text(encoding='utf-8'))
    rows = [r for r in archived['rows'] if (r['shot'], r['style']) == ('S1', 'red_wheel_frame')]
    if len(rows) != 1 or rows[0]['state'] != 'succeeded':
        raise ValueError('expected one successful archived S1 recolored-reference run')
    old = rows[0]
    reference = old['conditioning_image']
    old_png = args.pair_run / reference['file']
    if checksum(old_png) != reference['sha256']:
        raise ValueError('archived reference PNG does not match the report')
    pixels = ffmpeg('-i', old_png, '-frames:v', 1, '-pix_fmt', 'rgb24',
                    '-threads', 1, '-f', 'rawvideo', 'pipe:1')
    if len(pixels) != WIDTH * HEIGHT * 3 or hashlib.sha256(pixels).hexdigest() != PREVIOUS_RGB:
        raise ValueError('the correction is calibrated to a different S1 reference')
    workflow = old['workflow']
    if (workflow['9']['inputs']['noise_seed'] != 88001
            or workflow['5']['inputs']['first_frame'] != ['15', 0]
            or not workflow['15']['inputs']['image'].endswith('/' + reference['sha256'] + '.png')
            or old['inspection']['history_entry']['prompt'][2] != workflow):
        raise ValueError('archived S1 workflow does not match the provider record')
    edited, changed = repair_residue(pixels)
    if not changed:
        raise ValueError('the correction changed zero pixels')
    args.output.mkdir(parents=True)
    corrected = args.output / 'S1-inner-rim-corrected.png'
    ffmpeg('-f', 'rawvideo', '-pixel_format', 'rgb24', '-video_size', f'{WIDTH}x{HEIGHT}',
           '-i', 'pipe:0', '-frames:v', 1, '-threads', 1, corrected, data=edited)
    report = dict(
        evidence='single_shot_followup_to_paired_H3_first_frame_conditioning',
        previous_run=args.pair_run.name, previous_prompt_id=old['prompt_id'],
        previous_reference=reference, previous_output_sha256=old['sha256'],
        corrected_reference=dict(file=corrected.name, sha256=checksum(corrected),
                                 rgb_sha256=hashlib.sha256(edited).hexdigest()),
        changed_pixels=changed, residual_regions=RESIDUAL_REGIONS,
        seed=88001, prompt=workflow['5']['inputs']['prompt'],
    )
    write_json(args.output / 'protocol.json', report)
    print(f'Prepared {corrected}: {changed} pixels changed', flush=True)
    if args.prepare_only:
        return
    backend = ComfyUI(args.server)
    check_backend(backend)
    image_name = upload_frame(backend, corrected, report['corrected_reference']['sha256'])
    row = render_pair_member(backend, condition(workflow, image_name),
                             args.output, 'S1', 'inner_rim_corrected', args.timeout, args.poll)
    write_json(args.output / 'report.json', dict(protocol=report, rows=[row], completed=1, planned=1))
    print(f'DONE S1 inner_rim_corrected: {row["file"]}', flush=True)


if __name__ == '__main__':
    main()
