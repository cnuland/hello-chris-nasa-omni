"""AC-PER-53 (final Gemma run, 2026-10-18): after the structure rule (AC-PER-50) was added to the prompt, Gemma listed
99 objects with only 2 non-catalog labels (both structure): loose items it used to report ("orange sphere", "pen",
"wristwatch", "microphone") were forced into catalog names or skipped, so the review queue had 0 unknowns, which
breaks the core requirement that NASA classifies unknown objects. The prompt must ask for non-catalog loose items
explicitly, with a plain label, and forbid forcing an item into a catalog class it does not clearly match."""
from perception.omni import build_system_prompt

CATALOG = ["laptop", "camera-dslr", "drill-pistol", "ctb-half", "tablet"]


def test_prompt_asks_for_non_catalog_loose_items_with_plain_labels():
    p = build_system_prompt(CATALOG).lower()
    assert "not in the catalog" in p or "not one of the catalog" in p
    assert "plain" in p and "label" in p
    assert "never force" in p


def test_prompt_says_unknown_items_matter():
    p = build_system_prompt(CATALOG).lower()
    assert "unknown" in p and ("classif" in p or "review" in p)


def test_catalog_names_still_preferred_when_they_clearly_match():
    p = build_system_prompt(CATALOG).lower()
    assert "clearly" in p
    assert "laptop" in p  # catalog list still present


def test_structure_rule_kept_but_scoped_to_fixed_structure():
    p = build_system_prompt(CATALOG).lower()
    assert "skip station structure" in p
    assert "loose" in p  # the contrast: fixed structure is skipped, loose items are reported
