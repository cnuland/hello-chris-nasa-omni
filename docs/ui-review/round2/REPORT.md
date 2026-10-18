# UI review, round 2 (2026-10-18)

**Verdict: pass with issues.** The dashboard is now usable by operators. The chatbot is much better but still answers
2 of 5 standard questions wrongly.

Round-1 findings: **14 FIXED, 6 PARTIAL, 2 NOT FIXED, 0 REGRESSED.** New findings: **17** (2 P1, 6 P2, 9 P3); details
are in `findings.json`, screenshots in `shots/`.

## Round-1 status
| ID | Status | Notes |
|---|---|---|
| UI-01 follow-ups | PARTIAL | Follow-ups call the right tool, and the multimeter answer is correct. They inherit the previous question's location (UI2-02). |
| UI-02 who / when | PARTIAL | "Last hour" works after an edit. Drill and Node 1 questions still fail (UI2-01, UI2-08). |
| UI-03 duplicate cards | FIXED | 37 of 37 reviews show candidates side by side; merge buttons have unique names; "keep both" exists. |
| UI-04 last handled | PARTIAL | 29 of 123 rows filled. A "used" event reads "to an unknown location"; history still shows "used USOS -> ?". |
| UI-05 status / stop | FIXED | |
| UI-06 markdown / citations | FIXED | |
| UI-07 UTC times | FIXED | |
| UI-08 bbox / lightbox | PARTIAL | The box can miss the object; candidate tiles and item hero images have no box. |
| UI-09 / UI-10 / UI-11 / UI-12 | FIXED | |
| UI-13 overview wording | PARTIAL | Says "from X" instead of "at X"; operators shown as people. |
| UI-14, UI-15, UI-17..UI-20 | FIXED | |
| UI-16 case-insensitive sort | NOT FIXED | Deferred to the API. |
| UI-21 filter alignment | PARTIAL | Sort box clipped at 1024 px. |
| UI-22 person-track split | NOT FIXED | Deferred. |

## New issues
| Priority | ID | Issue |
|---|---|---|
| P1 | UI2-01 | "Who used the drill?" still says "no record", although the event exists (drill a454e74e, used 22:53). |
| P1 | UI2-02 | Follow-ups silently reuse the previous question's location (Q4 scoped to USOS). |
| P2 | UI2-03 | Answers are cut mid-word at the 400-token cap, with no notice. |
| P2 | UI2-04 | The LLM works out "last hour" itself; one call used a 2 h window, one returned non-JSON. |
| P2 | UI2-05 | Merge has no confirm and no undo; the merged item's page doesn't say where it went. |
| P2 | UI2-06 | Duplicate suggestions aren't ranked (same 8 items everywhere; similarity is null). |
| P2 | UI2-07 | Review bbox frames an empty patch (omni box). |
| P2 | UI2-08 | Unknowns in Node 1 answers "no record" instead of "none in NOD1"; "Node 1" isn't resolved. |
| P3 | UI2-09..17 | Wording, operator vs person, "9 × 9" label, list numbering, filtered counts, the one axe issue (repeated region names), toast jargon, no live streaming of answer text, filter alignment. |

## Chat timings (one conversation)
| Question | Time | Result |
|---|---|---|
| Laptop now | 29.7 s | Cut off |
| Multimeter usual | 10.8 s | OK |
| Drill user | 15.3 s | Wrong |
| Last hour | 13.2 s | Wrongly scoped |
| Unknowns in Node 1 | 10.2 s | Wrong |
| Last hour, after an edit | 15.5 s | OK |

## Mutations (operator ui-review-2)
1. Edit: 3e01062f… moved NOD3 → NOD1.
2. Merge: review 85c4f568…, merging d34865e0… into 99eba10e… (identical bags 2 s apart). No unmerge endpoint exists yet.
3. Keep both: review 8ac61810….

## Regression checks
- Filters: 12 combinations, all exact.
- 1024 px: no overflow.
- axe: one moderate violation on /review.
- Lightbox: keyboard OK.
- Console and network: 0 errors.
