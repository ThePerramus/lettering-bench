"""The real shape of a speech balloon, as a profile text can be laid out in.

build_bubble_clip_mask() (the same balloon-interior detection Clean uses)
finds the balloon's inside by flood-filling its background from the text;
from that mask this takes, for each row, the free horizontal span - the run
of interior pixels the balloon's body is in - drops the tail (rows much
narrower than the body, at the top or bottom), keeps an inner margin, and
normalizes everything to the resulting box so it scales with the text box
like the oval does.

profile = {
    "aspect": box height / box width,
    "rows": [[left, right], ...]   # PROFILE_ROWS rows, fractions of the width
}
"""

from __future__ import annotations

import numpy as np

from modules.utils.image_utils import build_bubble_clip_mask

PROFILE_ROWS = 48
# Rows narrower than this fraction of the widest one are the tail or a sliver
# of the outline, not room for text.
_MIN_ROW_FRACTION = 0.3
# Inner margin, as a fraction of the balloon's smaller side.
_MARGIN_FRACTION = 0.07
# Window (fraction of the height) for ignoring spikes in the outline.
_EDGE_WINDOW_FRACTION = 0.06


def _row_span(row: np.ndarray, center: int) -> tuple[int, int] | None:
    """The run of True pixels containing `center`, or the longest run."""
    if not row.any():
        return None
    padded = np.concatenate(([False], row, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    runs = list(zip(edges[0::2], edges[1::2]))  # [start, end)
    for start, end in runs:
        if start <= center < end:
            return int(start), int(end)
    start, end = max(runs, key=lambda r: r[1] - r[0])
    return int(start), int(end)


def balloon_profile(image: np.ndarray, bubble_xyxy, seed_xyxy=None):
    """-> (profile, (x1, y1, x2, y2) of the free area in page pixels), or
    (None, None) when there's no usable shape."""
    if image is None or bubble_xyxy is None:
        return None, None
    bx1, by1, bx2, by2 = [int(round(float(v))) for v in bubble_xyxy[:4]]
    h_img, w_img = image.shape[:2]
    bx1, by1 = max(0, bx1), max(0, by1)
    bx2, by2 = min(w_img, bx2), min(h_img, by2)
    bw, bh = bx2 - bx1, by2 - by1
    if bw < 12 or bh < 12:
        return None, None

    seed = None
    if seed_xyxy is not None:
        sx1, sy1, sx2, sy2 = [int(round(float(v))) for v in seed_xyxy[:4]]
        # Only a seed inside the balloon makes sense for the flood fill.
        sx1, sy1 = max(sx1, bx1), max(sy1, by1)
        sx2, sy2 = min(sx2, bx2), min(sy2, by2)
        if sx2 > sx1 and sy2 > sy1:
            seed = (sx1, sy1, sx2, sy2)

    try:
        mask = build_bubble_clip_mask(
            (bh, bw), (bx1, by1, bx2, by2), (bx1, by1, bx2, by2),
            inset=2, image=image, seed_bbox=seed,
        )
    except Exception:
        return None, None
    if mask is None or not mask.any():
        return None, None

    cols = np.flatnonzero(mask.any(axis=0))
    center = int((cols[0] + cols[-1]) // 2)
    spans = [_row_span(mask[y], center) for y in range(bh)]
    widths = np.array([(s[1] - s[0]) if s else 0 for s in spans])
    if widths.max() <= 0:
        return None, None

    margin = max(2, int(round(min(bw, bh) * _MARGIN_FRACTION)))
    usable = widths >= max(widths.max() * _MIN_ROW_FRACTION, 2 * margin + 4)
    rows = np.flatnonzero(usable)
    if rows.size < 2 * margin + 4:
        return None, None
    # The body: the longest stretch of usable rows (a tail is cut off).
    breaks = np.flatnonzero(np.diff(rows) > 1)
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [rows.size - 1]))
    k = int(np.argmax(ends - starts))
    top, bottom = int(rows[starts[k]]) + margin, int(rows[ends[k]]) - margin
    if bottom - top < 8:
        return None, None

    lefts = np.array([spans[y][0] + margin for y in range(top, bottom + 1)], dtype=float)
    rights = np.array([spans[y][1] - margin for y in range(top, bottom + 1)], dtype=float)
    # A tail joining the body at the side makes the edge jut out for a few
    # rows: over a window of rows keep the innermost edge, so such a spike
    # doesn't count as room (a smooth outline is barely affected).
    k = max(1, int(round(len(lefts) * _EDGE_WINDOW_FRACTION)))
    pad_l = np.pad(lefts, k, mode="edge")
    pad_r = np.pad(rights, k, mode="edge")
    lefts = np.array([pad_l[i:i + 2 * k + 1].max() for i in range(len(lefts))])
    rights = np.array([pad_r[i:i + 2 * k + 1].min() for i in range(len(rights))])
    ok = rights - lefts > 2
    if not ok.any():
        return None, None
    x_min, x_max = float(lefts[ok].min()), float(rights[ok].max())
    width, height = x_max - x_min, float(bottom - top + 1)
    if width < 8:
        return None, None

    out_rows = []
    for i in range(PROFILE_ROWS):
        y0 = int(i * len(lefts) / PROFILE_ROWS)
        y1 = max(y0 + 1, int((i + 1) * len(lefts) / PROFILE_ROWS))
        seg_l, seg_r = lefts[y0:y1], rights[y0:y1]
        seg_ok = (seg_r - seg_l) > 2
        if not seg_ok.any():
            out_rows.append([0.5, 0.5])
            continue
        left = (float(seg_l[seg_ok].max()) - x_min) / width
        right = (float(seg_r[seg_ok].min()) - x_min) / width
        out_rows.append([round(max(0.0, left), 4), round(min(1.0, max(left, right)), 4)])

    profile = {"aspect": round(height / width, 4), "rows": out_rows}
    area = (bx1 + x_min, by1 + top, bx1 + x_max, by1 + bottom + 1)
    return profile, area
