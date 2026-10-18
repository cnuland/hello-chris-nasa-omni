"""AC-API-41 crop privacy (UI review round 4, finding UI4-01): the API's own check that no stored crop overlaps a
person. The same rule as perception (``perception.privacy``, AC-PER-49), on normalised (x1, y1, x2, y2) boxes; every
measure is a ratio of areas, so it does not depend on the frame's aspect ratio.

A crop box C overlaps a person box P when IoU(C, P) > 0.05, or P covers more than 10 % of C, or C covers at least 25 % of
P's head zone (its top third, where the face is).
"""

from __future__ import annotations

from collections.abc import Sequence

IOU_MAX = 0.05
CROP_SHARE_MAX = 0.10
HEAD_COVER_MIN = 0.25
HEAD_FRACTION = 1.0 / 3.0

Box = Sequence[float]


def _area(b: Box) -> float:
    return max(float(b[2]) - float(b[0]), 0.0) * max(float(b[3]) - float(b[1]), 0.0)


def _inter(a: Box, b: Box) -> float:
    w = min(float(a[2]), float(b[2])) - max(float(a[0]), float(b[0]))
    h = min(float(a[3]), float(b[3])) - max(float(a[1]), float(b[1]))
    return max(w, 0.0) * max(h, 0.0)


def overlaps_person(crop: Box, people: Sequence[Box], kinds: Sequence[str] | None = None) -> bool:
    """True when ``crop`` overlaps any of ``people`` under the rule above. ``kinds[i] == "face"`` makes the whole box
    the head zone. A crop with no area cannot be checked and counts as overlapping (fail closed)."""
    crop_area = _area(crop)
    if crop_area <= 0:
        return bool(people)
    for i, p in enumerate(people):
        face = bool(kinds) and i < len(kinds) and kinds[i] == "face"
        inter = _inter(crop, p)
        p_area = _area(p)
        union = crop_area + p_area - inter
        if p_area > 0 and union > 0 and inter / union > IOU_MAX:
            return True
        if inter / crop_area > CROP_SHARE_MAX:
            return True
        x1, y1, x2, y2 = (float(v) for v in p)
        head = (x1, y1, x2, y2) if face else (x1, y1, x2, y1 + (y2 - y1) * HEAD_FRACTION)
        head_area = _area(head)
        if head_area > 0 and _inter(crop, head) / head_area >= HEAD_COVER_MIN:
            return True
    return False
