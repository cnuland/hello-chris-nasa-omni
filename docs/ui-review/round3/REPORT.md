# UI review, round 3 (2026-10-18, live US-origin stack: Gemma 4 perception and chat, Whisper)

**Verdict: pass with issues.**
- Round-2 and earlier items: **19 FIXED, 6 PARTIAL, 0 NOT FIXED, 0 REGRESSED.**
- New findings: **12** (1 P1, 4 P2, 7 P3); details in `findings.json`, screenshots in `shots/`.
- **Privacy:** no image of a sleeping person or a crew-quarters interior anywhere (P1 check passed). The scene-summary
  *text* of private windows is still stored and searchable (UI3-02).

## What works now
- **Merge:** confirm, undo toast, unmerge from the item page, banners. Tested end to end twice.
- **Duplicate candidates:** ranked, with score and reason chips.
- **Chat:** 6 of 7 standard questions answered correctly in 8–24 s, with a live status line, tool names, Stop and
  Retry.
- **Layout:** filters aligned at 1024 and 1440 px.
- **axe:** 0 violations on all main pages at both widths.

## New findings
| Priority | ID | Issue |
|---|---|---|
| P1 | UI3-01 | The people-count answer is wrong ("4 people" vs 17 tracks). The agent counted a capped recent_changes list. |
| P2 | UI3-02 | No-capture windows' scene summaries are searchable (e.g. "person is sleeping in a sleeping bag inside the Harmony Module"). |
| P2 | UI3-03 | Person rows all read "person / Crew member"; the appearance should be the display label. |
| P2 | UI3-04 | Person events read as one person moving another ("MOVED person … by person (spacesuit…)"). |
| P2 | UI3-05 | Person duplicate reviews can't be decided: whole frames with no box, an internal "expected count" reason, mixed-outfit tracks. |
| P3 | UI3-06 | The "People seen" tile counts tracks, not sightings. |
| P3 | UI3-07 | Crops are captioned "Whole frame" and stretched until blurry. |
| P3 | UI3-08 | Catalog slugs shown instead of names. |
| P3 | UI3-09 | The 171 px sticky footer hides candidates at 1024×768. |
| P3 | UI3-10 | Chat change lists use machine phrasing and inconsistent formats. |
| P3 | UI3-11 | Wording: split vs unmerge, mixed update labels. |
| P3 | UI3-12 | axe page-has-heading-one on the not-found pages. |

## Remaining PARTIAL items
- **Boxes:** only 1 item has a box, and it is misplaced. Perception data.
- **Event wording:** "to an unknown location" in 6 of 10 overview events.
- **Streaming:** the status line is live, but the answer text still lands at the end.
- **Person tracks:** 17 tracks for a crew of 7; some tracks mix outfits.

## Chat timings
| Question | Time | Result |
|---|---|---|
| Laptop now | 17.3 s | OK |
| Multimeter usual | 11.0 s | OK |
| Drill user | 8.1 s | OK |
| Last hour | 24.1 s | OK, but with machine phrasing |
| Unknowns in Node 1 | 7.8 s | OK |
| Cupola | 10.5 s | OK |
| People count | 19.8 s | **wrong** |

## Mutations (operator ui-review-3)
- Merge + unmerge of 2348f197… into a26efebe… (review f6f302cf…), done twice, once from the item page and once from
  the toast undo. Both restored the items.
- Edit: 2348f197… location USOS → JPM1.
