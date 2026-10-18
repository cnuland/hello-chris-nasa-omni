"""AC-PER-49 crop privacy (UI review round 4, finding UI4-01): no stored or embedded crop may overlap a person.

Geometry is on normalised (x1, y1, x2, y2) boxes. IoU, the share of the crop that a region covers and the share of a
region's head zone that the crop covers are all ratios of areas, so they do not depend on the frame's aspect ratio.
inventory-api applies the same rule on its own (``inventory_api.privacy``, AC-API-41).
"""

from __future__ import annotations

from collections.abc import Sequence

from .labels import is_structure_label, label_heads, label_key

IOU_MAX = 0.05  # IoU above this is an overlap
CROP_SHARE_MAX = 0.10  # a region covering more than this share of the crop is an overlap
HEAD_COVER_MIN = 0.25  # a crop covering at least this share of a region's head zone is an overlap
HEAD_FRACTION = 1.0 / 3.0  # the head zone of a person box is its top third (where the face is)
FACE = "face"
PERSON = "person"

Box = Sequence[float]

# Body-part labels: never an object instance, always a person region. Head nouns (last word of the label's noun
# phrase) and whole-phrase keys; "head" and "body" only as the whole label ("drill head" is not a body part).
_BODY_HEADS = frozenset(
    ["face", "faces", "hand", "hands", "arm", "arms", "finger", "fingers", "leg", "legs", "foot", "feet", "torso", "figure",
     "silhouette"]
)
_BODY_WHOLE = frozenset(label_key(k) for k in ("head", "body", "human", "humans", "man", "woman", "people",
                                               "human face", "human head"))
# Region kind "face": the whole box is the head zone
_FACE_HEADS = frozenset({"face", "faces", "head"})


def is_body_label(text: str) -> bool:
    """A face / head / hand / arm ... label (also "astronaut's hand", "crew member's face")."""
    heads = label_heads(text)
    if not heads or is_structure_label(text):  # "robotic arm" is station hardware, not a body part
        return False
    return heads[-1] in _BODY_HEADS or heads[0] in _BODY_WHOLE


def region_kind(text: str) -> str:
    """"face" for a face or head label (the whole box is the head zone), else "person"."""
    heads = label_heads(text)
    return FACE if heads and (heads[-1] in _FACE_HEADS or heads[0] in _FACE_HEADS) else PERSON


def ordered(b: Box) -> tuple[float, float, float, float]:
    """(x1, y1, x2, y2) with x1 <= x2 and y1 <= y2 (review round: an inverted box must not fail open)."""
    x1, y1, x2, y2 = (float(v) for v in b)
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def _area(b: Box) -> float:
    b = ordered(b)
    return max(b[2] - b[0], 0.0) * max(b[3] - b[1], 0.0)


def _inter(a: Box, b: Box) -> float:
    a, b = ordered(a), ordered(b)
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(w, 0.0) * max(h, 0.0)


def iou(a: Box, b: Box) -> float:
    inter = _inter(a, b)
    union = _area(a) + _area(b) - inter
    return inter / union if union > 0 and _area(a) > 0 and _area(b) > 0 else 0.0


def intersection_over_crop(crop: Box, region: Box) -> float:
    """Share of the crop's area that the region covers."""
    area = _area(crop)
    return _inter(crop, region) / area if area > 0 else 0.0


def head_zone(region: Box, *, face: bool = False) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = ordered(region)
    if face:
        return (x1, y1, x2, y2)
    return (x1, y1, x2, y1 + (y2 - y1) * HEAD_FRACTION)


def head_zone_cover(crop: Box, region: Box, *, face: bool = False) -> float:
    """Share of the region's head zone (top third; the whole box for a face) that the crop covers."""
    zone = head_zone(region, face=face)
    area = _area(zone)
    return _inter(crop, zone) / area if area > 0 else 0.0


def overlaps_person(crop: Box, regions: Sequence[Box], kinds: Sequence[str] | None = None) -> bool:
    """True when the crop box overlaps any person region: IoU > 0.05, or the region covers > 10 % of the crop, or the
    crop covers >= 25 % of the region's head zone. ``kinds[i]`` is "face" or "person" (default) for ``regions[i]``."""
    if regions and _area(crop) <= 0:
        return True  # a crop without area cannot be checked: fail closed
    for i, region in enumerate(regions):
        face = bool(kinds) and i < len(kinds) and kinds[i] == FACE
        if (
            iou(crop, region) > IOU_MAX
            or intersection_over_crop(crop, region) > CROP_SHARE_MAX
            or head_zone_cover(crop, region, face=face) >= HEAD_COVER_MIN
        ):
            return True
    return False
