"""AC-PER-8 (Integration phase regression): identical twins keep their identities.

Found by the eval integration run (synth seed 2, repeated runs): two identical duct-tape rolls in two zones get
identical (or nearly identical) appearance scores against both twin entities. ``/ingest/candidates`` breaks exact
similarity ties by ``entity_id`` (a random UUID), so which twin an observation was assigned to changed from run to
run, and the twins sometimes swapped identities (false merge, pairwise F1 0.94 instead of 1.0).

Rule: among candidates whose score is within ``NEAR_TIE_P`` of the best one, prefer the candidate last seen in the
observation's own location (camera zone); otherwise the order is score, then entity id. A clearly better appearance
match still wins over location.
"""
from __future__ import annotations

import numpy as np
import pytest
from support import T0, assignment_for_bbox, blend, obj, random_unit, ts
from test_ac_per_08_reconciler import BOX_A, BOX_B, process

from perception.reconciler import NEAR_TIE_P

LAB_ZONES = {"LAB1S2": (0.0, 0.0, 0.5, 1.0), "LAB1P3": (0.5, 0.0, 1.0, 1.0)}
LOW_ID = "00000000-0000-4000-8000-000000000001"
HIGH_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"
TWIN = random_unit(7)


class ConstEmbedder:
    """Every crop looks the same: identical twins."""

    def __init__(self, vector):
        self.vector = np.asarray(vector, dtype=np.float32)

    def embed(self, crop):
        return self.vector


def _rec(make_reconciler):
    rec = make_reconciler()
    rec.config.camera_zones = {"cam-lab1-fwd": dict(LAB_ZONES)}
    rec.embedder = ConstEmbedder(TWIN)
    return rec


def _twins(inv, left_vec, right_vec, *, left_first: bool, left_id: str, right_id: str):
    last = ts(-5)
    rows = [("left", left_id, left_vec, "LAB1S2"), ("right", right_id, right_vec, "LAB1P3")]
    if not left_first:
        rows.reverse()
    ids = {}
    for name, eid, vec, loc in rows:
        ids[name] = inv.add_entity(catalog_id="duct-tape", embedding=vec, last_location=loc, last_seen_at=last,
                                   entity_id=eid)
    return ids


def _assigned(inv):
    payload = inv.last
    return (assignment_for_bbox(payload, BOX_A)["entity_id"], assignment_for_bbox(payload, BOX_B)["entity_id"])


@pytest.mark.parametrize("left_first", [True, False])
@pytest.mark.parametrize("left_id,right_id", [(LOW_ID, HIGH_ID), (HIGH_ID, LOW_ID)])
def test_ac_per_08_identical_twins_keep_their_zones(make_reconciler, inv, left_first, left_id, right_id):
    ids = _twins(inv, TWIN, TWIN, left_first=left_first, left_id=left_id, right_id=right_id)
    process(_rec(make_reconciler), [[("tape-L", BOX_A), ("tape-R", BOX_B)]],
            [obj("duct tape", BOX_A), obj("duct tape", BOX_B)], {"tape-L": "duct tape", "tape-R": "duct tape"},
            t0=T0)
    assert _assigned(inv) == (ids["left"], ids["right"])


@pytest.mark.parametrize("left_first", [True, False])
def test_ac_per_08_near_tie_prefers_same_zone(make_reconciler, inv, left_first):
    """The left twin's stored view is slightly different (similarity 0.995): still a near tie, location decides."""
    ids = _twins(inv, blend(TWIN, random_unit(8), 0.995), TWIN, left_first=left_first, left_id=LOW_ID,
                 right_id=HIGH_ID)
    process(_rec(make_reconciler), [[("tape-L", BOX_A), ("tape-R", BOX_B)]],
            [obj("duct tape", BOX_A), obj("duct tape", BOX_B)], {"tape-L": "duct tape", "tape-R": "duct tape"},
            t0=T0)
    assert _assigned(inv) == (ids["left"], ids["right"])


def test_ac_per_08_clearly_better_appearance_beats_location(make_reconciler, inv):
    """Not a near tie: the object in the left zone looks like the right-zone entity (it was moved there)."""
    left = inv.add_entity(catalog_id="duct-tape", embedding=blend(TWIN, random_unit(9), 0.80), last_location="LAB1S2",
                          last_seen_at=T0)
    right = inv.add_entity(catalog_id="duct-tape", embedding=TWIN, last_location="LAB1P3", last_seen_at=T0)
    process(_rec(make_reconciler), [[("tape-L", BOX_A)]], [obj("duct tape", BOX_A)], {"tape-L": "duct tape"}, t0=T0)
    assert assignment_for_bbox(inv.last, BOX_A)["entity_id"] == right
    assert left != right
    assert NEAR_TIE_P < 0.7 * (1 - 0.80)
