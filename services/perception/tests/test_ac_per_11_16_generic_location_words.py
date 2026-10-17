"""AC-PER-11 / AC-PER-16 (Integration phase regression): generic words must not resolve to a location.

Found by the eval integration run (synth seed 1): the crew line "I'm moving the multimeter over to the port rack."
produced a speech ``moved`` event to NOD2P5 ("Node 2 crew quarters (port)", a no_capture zone), because the text
inside a zone's parentheses ("port", "deck", "stbd", "ovhd") was registered as an alias of that zone. Parenthetical
aliases are meaningful for module names ("Destiny (US Lab)") but are bare qualifiers for zones. The resolver's own
rule applies: a wrong location is worse than none.
"""
from __future__ import annotations

import pytest
from support import CATALOG, LOCATIONS

from perception.locations import LocationResolver
from perception.speech import parse_speech_claim


@pytest.fixture()
def resolver():
    return LocationResolver(LOCATIONS)


@pytest.mark.parametrize("word", ["port", "deck", "stbd", "ovhd", "the port", "toilet", "whc"])
def test_ac_per_11_zone_qualifier_in_parentheses_is_not_an_alias(resolver, word):
    assert resolver.resolve(word) is None


@pytest.mark.parametrize("hint,expected", [
    ("US Lab", "LAB1"), ("Destiny", "LAB1"), ("Node 2 crew quarters (port)", "NOD2P5"), ("NOD2P5", "NOD2P5"),
    ("Lab port rack 3", "LAB1P3"),
])
def test_ac_per_11_module_parenthetical_and_full_names_still_resolve(resolver, hint, expected):
    assert resolver.resolve(hint) == expected


def test_ac_per_16_port_rack_is_not_crew_quarters():
    claim = parse_speech_claim("I'm moving the multimeter over to the port rack.", CATALOG, LocationResolver(LOCATIONS))
    assert claim is None or claim.location_id != "NOD2P5"


def test_ac_per_16_explicit_zone_still_claims():
    claim = parse_speech_claim("I'm moving the multimeter over to lab port rack 3.", CATALOG,
                               LocationResolver(LOCATIONS))
    assert claim is not None and claim.location_id == "LAB1P3" and claim.catalog_id == "multimeter"
