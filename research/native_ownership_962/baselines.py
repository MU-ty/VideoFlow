"""Frozen order-invariant geometric baseline from Round 955."""
from collections import Counter


def full_frame(shot):
    if not shot["valid_tracks"]:
        return "unknown"
    # A disconnected foreground alone cannot reject. Apply the same gap evidence.
    rows = shot["frame_rows"]
    c = Counter(label for _, label, _ in rows)
    directional = c["same"] + c["distinct"]
    negative = sum(gap for _, _, gap in rows)
    if directional >= 12:
        if negative >= 0.75 * directional:
            return "distinct"
        if c["same"] >= 0.75 * directional:
            return "same"
    return "unknown"
