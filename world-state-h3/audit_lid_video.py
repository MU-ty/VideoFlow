"""Audit the 928 lid experiment's existing videos without running H3 again."""
import argparse
import json
from pathlib import Path
import subprocess

import numpy as np

WIDTH, HEIGHT = 640, 360
# Coordinates frozen on the screened before sheet, at 640 x 360.
REGIONS = {'tray': (122, 120, 235, 272), 'mouth': (373, 130, 492, 249),
           'cup': (270, 30, 348, 102)}
MOUTH_ANCHOR = (432, 195)
MIN_VISIBLE_PIXELS = 40


def lid_mask(frame):
    red, green, blue = np.moveaxis(frame.astype(np.int16), -1, 0)
    return (green - red >= 24) & (blue - red >= 24) & (np.abs(green - blue) < 35)


def counts(mask):
    return {name: int(mask[y0:y1, x0:x1].sum())
            for name, (x0, y0, x1, y1) in REGIONS.items()}


def forecast(mask):
    x0, y0, x1, y1 = REGIONS['tray']
    y, x = np.where(mask[y0:y1, x0:x1])
    if len(x) < MIN_VISIBLE_PIXELS:
        return None
    x, y = x + x0, y + y0
    x += round(MOUTH_ANCHOR[0] - np.median(x))
    y += round(MOUTH_ANCHOR[1] - np.median(y))
    valid = (x >= 0) & (x < WIDTH) & (y >= 0) & (y < HEIGHT)
    result = np.zeros_like(mask)
    result[y[valid], x[valid]] = True
    return sorted(region for region, count in counts(mask | result).items()
                  if count >= MIN_VISIBLE_PIXELS)


def read_video(path, *, estimate=False):
    probe = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
         'frame=best_effort_timestamp_time', '-of', 'json', str(path)],
        check=True, capture_output=True, text=True)
    timings = [float(row['best_effort_timestamp_time'])
               for row in json.loads(probe.stdout)['frames']]
    size = WIDTH * HEIGHT * 3
    rows = []
    with subprocess.Popen(
        ['ffmpeg', '-v', 'error', '-nostdin', '-i', str(path), '-map', '0:v:0',
         '-vf', f'scale={WIDTH}:{HEIGHT}:flags=bicubic', '-vsync', '0',
         '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-threads', '1', 'pipe:1'],
        stdout=subprocess.PIPE
    ) as decoded:
        while data := decoded.stdout.read(size):
            if len(data) != size or len(rows) >= len(timings):
                decoded.kill()
                raise ValueError(f'frame count or frame size mismatch: {path}')
            frame = np.frombuffer(data, dtype=np.uint8).reshape(HEIGHT, WIDTH, 3)
            mask = lid_mask(frame)
            rows.append(dict(frame=len(rows), timestamp_s=timings[len(rows)] - timings[0],
                             teal_pixels=counts(mask)))
            if estimate:
                rows[-1]['forecast'] = forecast(mask)
        if decoded.wait() or len(rows) != len(timings) or not rows:
            raise ValueError(f'could not decode every timestamped frame: {path}')
    return rows


def flagged_intervals(rows):
    intervals = []
    start = None
    for row in rows:
        if not row['color_consistent'] and start is None:
            start = row
        if row['color_consistent'] and start is not None:
            intervals.append(dict(first_frame=start['frame'], start_s=start['timestamp_s'],
                                  end_s_exclusive=row['timestamp_s']))
            start = None
    if start is not None:
        intervals.append(dict(first_frame=start['frame'], start_s=start['timestamp_s'],
                              last_frame=rows[-1]['frame'], last_frame_s=rows[-1]['timestamp_s']))
    return intervals


def audit(before, after):
    # Derive the occupancy threshold from the source clip only.
    tray_area = int(np.median([row['teal_pixels']['tray'] for row in before]))
    if tray_area < MIN_VISIBLE_PIXELS:
        raise ValueError('the lid is not sufficiently visible in the source tray crop')
    occupancy_floor = max(MIN_VISIBLE_PIXELS, round(.25 * tray_area))
    for rows, occupied in ((before, 'tray'), (after, 'mouth')):
        for row in rows:
            row['color_consistent'] = all(
                n >= occupancy_floor if name == occupied else n < MIN_VISIBLE_PIXELS
                for name, n in row['teal_pixels'].items())
    return dict(
        interpretation=('Fixed-region teal occupancy is a review proxy. Inspect flagged intervals '
                        'and the video for physical closure, continuity, and object drift.'),
        frame_size=[WIDTH, HEIGHT], regions=REGIONS, source_mouth_anchor=MOUTH_ANCHOR,
        min_visible_pixels=MIN_VISIBLE_PIXELS, occupancy_floor=occupancy_floor,
        old_frame_selected=sorted({name for row in before for name, n in row['teal_pixels'].items()
                                   if n >= MIN_VISIBLE_PIXELS}),
        source_state_forecast=sorted({name for row in before for name in row['forecast'] or []}),
        source_forecast_unavailable_frames=sum(row['forecast'] is None for row in before),
        before=dict(total_frames=len(before), consistent_frames=sum(x['color_consistent'] for x in before),
                    flags=flagged_intervals(before), frames=before),
        after=dict(total_frames=len(after), consistent_frames=sum(x['color_consistent'] for x in after),
                   flags=flagged_intervals(after), frames=after))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', type=Path, required=True)
    parser.add_argument('--after', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    before = read_video(args.before, estimate=True)
    after = read_video(args.after)
    result = audit(before, after)
    with args.output.open('x') as output:
        json.dump(result, output, indent=2)
        output.write('\n')
    for phase in ('before', 'after'):
        summary = result[phase]
        print(f"{phase}: {summary['consistent_frames']}/{summary['total_frames']} frames "
              f"consistent with expected teal region, {len(summary['flags'])} flagged intervals")


if __name__ == '__main__':
    main()
