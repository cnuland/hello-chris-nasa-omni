# UI review, round 4 (2026-10-18, live US-origin stack, final tour run)

**Verdict: pass with issues; one P1 (privacy).**
- Earlier items checked: 16, of which 8 FIXED, 8 PARTIAL, 0 NOT FIXED. One fix caused a layout regression (UI4-02).
- New findings: 11 (1 P1, 5 P2, 5 P3).
- axe: 0 violations on 14 routes at 1024 and 1440 px.

**Demo-readiness: not yet; it could be after a short fix list.** Top 5:
1. Face crops: UI4-01.
2. Overflowing wide crops: UI4-02.
3. Structure-only unknowns: UI4-05.
4. Contradictory person events: UI4-06.
5. Chat argument hygiene: UI4-03 and UI4-04.

## P1 UI4-01: purged by the orchestrator at about 05:00 UTC
A misplaced "window" object box produced a 915×258 crop of a crew member's face and upper body
(`crops/cam-handheld/2026-10-18/043649.880320-30.jpg`, item bb545ad4, review 1cc1dff1). It was removed:
- the S3 object was deleted and its URL now returns 404;
- exemplar 34 (the face embedding) was deleted;
- the item's snapshot was cleared;
- the review image was replaced with a "removed for crew privacy" placeholder and the review dismissed by
  `privacy-purge`;
- an audit_log row was written.

A prevention fix (no object crop that overlaps a person) is under TDD. The other 22 crops show no faces.

## Status
| Status | Items |
|---|---|
| FIXED | UI3-01 people count (chat 25 = API 25), UI3-02 private text, UI3-06, UI3-07 (with the UI4-02 regression), UI3-08, UI3-09, UI3-12, UI2-16 streaming |
| PARTIAL | UI3-03 (Overview still "Person 737f632b"), UI3-04, UI3-05 (whole frames, no person box), UI3-10 (list starts at row 2), UI3-11 casing, UI2-07 (0 of 41 objects have boxes), UI2-09, UI-22 (25 tracks for about 7 crew; the continuity fix is committed but not deployed) |

## New findings
| Priority | ID | Issue |
|---|---|---|
| P1 | UI4-01 | Face crop shown as the unknown "window" (purged; prevention under way) |
| P2 | UI4-02 | Wide crops overflow the review card; the page is 1890 px wide at 1440 |
| P2 | UI4-03 | The Cupola question lost its location as Q6 of a long chat |
| P2 | UI4-04 | Change list starts at row 2 (the model passed offset=1); duplicate citations |
| P2 | UI4-05 | All 6 unknowns are station structure (window, hatch, handle…), so there is nothing to classify |
| P2 | UI4-06 | Overview: "Person <id>", "seen" with no place, "left USOS" while still in USOS |
| P3 | UI4-07 | 30 items only at USOS; by_module leaves out segments |
| P3 | UI4-08 | Laptop answer lists 5 with no total (9 exist) |
| P3 | UI4-09 | No text for 8–23 s, then a fast replay; the status timer briefly shows the previous question's time |
| P3 | UI4-10 | Items first seen in private windows show "no image" with no reason |
| P3 | UI4-11 | A 76×62 crop is used as the detail-page hero |

## Chat (one conversation)
| Question | Time | Result |
|---|---|---|
| Laptop | 24.8 s | 5 listed, no total |
| Multimeter | 8.1 s | OK |
| Drill | 9.9 s | OK |
| Last hour | 7.3 s | starts at row 2 |
| show more changes | 1.6 s | pages correctly |
| Unknowns in Node 1 | 17.1 s | OK |
| Cupola | 11.4 s | wrong scope |
| People count | 21.6 s | **correct**, 23 = API |

## Privacy
No text or image from private windows was found in any API, search or chat probe.

## Mutations (ui-review-4)
1. Edit: drill 48f6f551 moved USOS → LAB1.
2. Dismiss: review c247b413 (a handle).
3. Merge then undo: b4e0a0f1 → 15302bc1. The review reopened as 20c3c040.
