"""Frozen Round 943 sequential correlation tracker."""
import numpy as np
from scipy.signal import correlate2d

PATCH_RADIUS = 12
SEARCH_RADIUS = 24
MIN_CORRELATION = 0.65

def move(template_image, image, center):
    r, s = PATCH_RADIUS, SEARCH_RADIUS
    x, y = center
    height, width = image.shape
    if not (r <= x < width-r and r <= y < height-r):
        return None
    template = template_image[y-r:y+r+1, x-r:x+r+1]
    template = template - template.mean()
    norm = np.linalg.norm(template)
    if norm < 1e-6:
        return None
    x0, y0 = max(0, x-r-s), max(0, y-r-s)
    crop = image[y0:min(height,y+r+s+1), x0:min(width,x+r+s+1)]
    ones = np.ones(template.shape)
    total = correlate2d(crop, ones, mode="valid")
    energy = correlate2d(crop**2, ones, mode="valid") - total**2/template.size
    denominator = norm * np.sqrt(np.maximum(0, energy))
    matches = correlate2d(crop, template, mode="valid") / np.maximum(denominator, 1e-6)
    iy, ix = np.unravel_index(np.argmax(matches), matches.shape)
    if matches[iy, ix] < MIN_CORRELATION:
        return None
    return (int(x0+ix+r), int(y0+iy+r))


def track(frames, initial):
    positions = [initial]
    for previous, frame in zip(frames, frames[1:]):
        chosen = move(previous, frame, positions[-1])
        if chosen is None:
            return positions
        positions.append(chosen)
    return positions


