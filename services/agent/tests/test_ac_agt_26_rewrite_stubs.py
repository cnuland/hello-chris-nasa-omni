"""AC-AGT-26 (live 2026-10-18, Gemma 4 chat): the usual-stowage rewrite (AC-AGT-18) kept a stub of a list:
"... catalog expected count: 1. The Fluke multimeter is usually kept in the following location: 1." The sentence
splitter cut "location: 1. LAB1 (Destiny ...)" at "1. "; the LAB1 part was dropped (it names the home) and the
lead-in plus a bare list marker survived. Kept fragments that are only a lead-in ending in ':' (optionally followed
by a bare list marker) are dropped."""
from nasa_agent.grounding import kept_sentences

HOME = {"catalog_id": "multimeter", "display_name": "Fluke multimeter", "home_location": "LAB1",
        "home_location_name": "Destiny (US Lab)", "expected_count": 1, "is_fungible": False}


def test_lead_in_with_bare_list_marker_is_dropped():
    reply = "The Fluke multimeter is usually kept in the following location: 1. LAB1 (Destiny (US Lab))."
    assert kept_sentences(reply, [HOME], [HOME]) == ""


def test_lead_in_colon_without_content_is_dropped():
    reply = "Here is where it is usually kept: LAB1 (Destiny (US Lab)). It is a Fluke model."
    out = kept_sentences(reply, [HOME], [HOME])
    assert "kept:" not in out and out.endswith("It is a Fluke model.")


def test_unrelated_numbered_content_is_kept():
    reply = "Expected count is 1. Calibrate it before use."
    assert kept_sentences(reply, [HOME], [HOME]) == "Expected count is 1. Calibrate it before use."
