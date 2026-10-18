# UI review, round 5 (2026-10-18, 06:33–06:48 UTC, fresh guarded Gemma 4 run)

**Verdict: pass with issues; no P1.**
- Earlier items: 13 FIXED, 6 PARTIAL, 0 regressed.
- New findings: 10 (4 P2, 6 P3).
- axe: 0 violations on 16 routes at 1440 and 1024 px.

**Demo-readiness: READY WITH CAVEATS.** Every walkthrough step works end to end. Chat: first text in ≤5 s for 5 of 9
questions (routed ones about 0.1 s), worst 15 s.

## Privacy: PASS
- 56 images checked; none of the 13 crops shows a face (two show only a leg or sleeve fragment at the edge).
- Whole frames with crew are allowed by design.
- Private windows: 9 items show the withheld reason, 19 search terms found no private window, and two chat probes
  leaked nothing.

## New findings
| Priority | ID | Issue |
|---|---|---|
| P2 | UI5-01 | Unknowns have no box (all 18 are whole frames); the OWLv2 unknown-label queries fix is under TDD |
| P2 | UI5-02 | The unknown queue floods with structure again (5 → 26 in 15 min: latch, foil insulation, wall-mounted cylinder, cloth cover, treadmill, "grey object" = hatch) |
| P2 | UI5-03 | Re-ID teleports an item: laptop 8535788a JPM1 → USOS → ZVEZDA with no move event; Kibo lost its laptop |
| P2 | UI5-04 | The Cupola answer names a Node 1 track. Three causes: filtering by last_location, the carry labelling Node 1 frames as CUP1, and a wrong person attach |
| P3 | UI5-05 | A segment-only sighting overwrites a module location |
| P3 | UI5-06 | Boxless history frames can't be enlarged |
| P3 | UI5-07 | "show more" late in a chat goes through the model, and page 2 repeats rows |
| P3 | UI5-08 | Empty answers are terse and cite unrelated rows |
| P3 | UI5-09 | A first sighting creates "Moved by an unknown person" |
| P3 | UI5-10 | Copy polish (person hero "no box for this object", "split" vs unmerge, label casing) |

## Walkthrough
1. Find and edit: OK.
2. Classify a real unknown: OK. The orange sphere became the new catalog item "Orange ball"; no existing class fit.
3. Not inventory: OK.
4. Merge and undo: OK.
5. Chat: 7 of 9 correct.

## Mutations (ui-review-5)
1. Laptop 8535788a description edited.
2. Review 857720bf classified as a new catalog item `orange-ball`.
3. Review 83eb5e1a marked Not inventory.
4. Merge then undo: 8c58d341 → 395bae46.
