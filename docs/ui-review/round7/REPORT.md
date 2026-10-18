# UI review, round 7 (2026-10-18, 09:52–10:18 UTC, live Gemma 4 run, passes 1 to 3)

**Verdict: READY WITH CAVEATS; no P1; privacy PASS; no UI regressions** (axe: 0 violations in 34 runs).

## Round-6 fixes verified live
| Fix | Result |
|---|---|
| History boxes | FIXED |
| Spacesuits as person tracks | FIXED |
| Worn items as unknowns | FIXED |
| Module → own-segment "moves" | FIXED |
| Private-window multiplication | FIXED |
| Colour-aware object re-ID | FIXED; people still over-merge |
| Structure memory | PASS: the dismissed duct was not raised again on the next pass (spacesuit inconclusive) |

## Remaining items (all data, mostly from the looping replay)
| Priority | ID | Issue |
|---|---|---|
| P2 | UI7-01 | `cam-handheld` defaults to USOS, so Russian-segment frames are filed under the wrong segment |
| P2 | UI7-02 | Person tracks: same-frame splits, phantom tracks, look-alike merges (20 tracks for about 7 crew) |
| P2 | UI7-03 | The queue doubles each loop pass (repeat duplicates from one frame) |
| P2 | UI7-04 | A catalog-label conflict was missed (duct → "astrobee robot"); chat ignores low_confidence reviews |
| P3 | UI7-05..08 | Memory copy/undo UI, private segment attaches, omni boxes on bodies, chat copy |

## Measured
- Boxes roughly right: detector 79%, omni 76%.
- Crops: 21 checked, 0 with faces.
- Chat: 9 questions in 47 s; 8 correct, 1 (drill) correct for the data but resting on a mislabelled item.

## What to say when presenting
- **People:** tracks are clothing and appearance, not identity. Expect about 2–3× more tracks than crew.
- **Places:** trust places that name a module. "US Orbital Segment" often means "place unknown".
- **Replays:** each replay of the tour adds review cards, so show the queue early (or after a fresh reset).
- **Boxes:** about 80% of solid and 75% of dashed boxes are roughly on target.
- **Labels:** labels can be wrong. Single-sighting answers are leads.
- **"Not inventory" memory:** it worked live, but is scoped to the whole segment and can't be undone in the UI yet.
- **Private areas:** only a label and a place are stored.

## Mutations (ui-review-7)
Not inventory on reviews ccf7f409 (spacesuit) and 4f8b75be (duct), which created memory rows 1 and 2.
