# UI review, round 6: final pre-demo check (2026-10-18, 08:08–08:20 UTC, live Gemma 4 run)

**Verdict: READY WITH CAVEATS; no P1; privacy PASS.**
- Every walkthrough step works end to end. Chat answered all 7 standard questions plus "show more" and the
  crew-quarters question correctly; the 7 questions take about 48 s in total.
- axe found 0 violations in 32 runs (16 routes × 2 widths).
- Round-5 items: 9 FIXED, 6 PARTIAL, 1 NOT FIXED (person tracks).

## Box accuracy (66 checked)
| Source | Correct | Roughly right | Wrong |
|---|---|---|---|
| Detector (solid) | 21 of 34 | 8 | 5 |
| Omni (dashed) | 14 of 32 | 10 | 8 |

That is about 86% / 75% at least roughly on target.

## Unknown queue (18 open)
- 10 real loose items, 2 equipment not in the catalog (EMU spacesuits), 4 structure, 2 worn by crew.
- All 18 have boxes: 15 correct, 2 roughly right, 1 wrong.

## Privacy
- 22 crops checked: none shows a face or upper body.
- Private windows: no text or images stored; only a label and a place are visible. Probe questions leaked nothing.

## What to say when presenting (honest caveats)
- **Person tracks are clothing and appearance, not identity.** Today's footage also counts empty spacesuits and
  merges look-alike blue shirts.
- **Places:** many second-pass sightings are only "US Orbital Segment". Trust places that name a module.
- **Boxes:** about 86% of solid boxes and 75% of dashed boxes are roughly on target.
- **Labels:** a catalog label can disagree with the description. Answers built on one sighting are leads.
- **Private areas** store no images or descriptions, only a label and a place.

## New findings
| Priority | ID | Issue |
|---|---|---|
| P2 | UI6-01 | History frames lost their boxes: integer vs string `sighting_id` (**fixed by the orchestrator, tested**) |
| P2 | UI6-02 | Same-class re-ID ignores colour and skips the strong-match rule when a sighting is segment-only (bag chains, one blue-shirt track covering several people) |
| P2 | UI6-03 | The second pass degrades locations (72% segment-only); Kibo frames filed under USOS |
| P2 | UI6-04 | Empty spacesuits become person tracks and unknowns |
| P2 | UI6-05 | "Not inventory" decisions aren't remembered (the dismissed duct came back as "white padded tube") |
| P2 | UI6-06 | Catalog labels contradict their descriptions (the "drill" is described as a vacuum cleaner) |
| P3 | UI6-07 | Wristwatch and helmet worn by crew raised as unknowns |
| P3 | UI6-08 | A module → own-segment change is reported as "moved" |
| P3 | UI6-09 | The Cupola answer shows the time the location was held, not the sighting time |
| P3 | UI6-10 | Private-window items multiply on each pass; duplicate CTB change rows |
| P3 | UI6-11 | Stray "·" in the person "Last seen" row |

## Mutations (ui-review-6)
1. Edit: laptop 19c96a34 description.
2. Classify: review ff2d6a28 → Orange ball.
3. Bulk Not inventory: reviews dc687b97 and 6388e99f.
4. Merge then undo: 00bc2c87 → 1e8251b6.
