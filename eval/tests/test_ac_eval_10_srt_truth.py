"""AC-EVAL-10 real-video ground truth from the NASA tour captions (docs/spec/50-eval.md).

The tour's SRT names each module in a title cue as the camera enters it; descriptions mention modules in sentences
and must not start segments. The real ``tour.srt`` is downloaded data (untrusted, parsed only); its test is skipped
when the file is absent.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nasa_eval.real import srt_truth
from nasa_eval.real.srt_truth import Cue, module_timeline, parse_srt

TOUR_SRT = Path("/Users/cnuland/nasa-media/iss-tour/tour.srt")

FIXTURE = (
    "﻿1\r\n00:00:01,101 --> 00:00:10,627\r\nStep Inside\r\nThe International Space Station\r\n\r\n"
    "2\r\n00:00:13,730 --> 00:00:19,052\r\nColumbus Laboratory Module\r\n\r\n"
    "3\r\n00:00:19,052 --> 00:00:24,307\r\nEurope's contribution, next door to Harmony,\r\nfor the crew\r\n\r\n"
    "4\r\n00:00:41,324 --> 00:00:46,646\r\nJapanese Experiment Module “Kibo”\r\n\r\n"
    "5\r\n00:01:21,181 --> 00:01:26,503\r\nScience on all sides\r\n\r\n"
    "6\r\nnot a timestamp\r\ngarbage block\r\n\r\n"
    "7\r\n00:01:44,454 --> 00:01:49,776\r\nKnown as Node 2, Harmony connects labs\r\n\r\n"
    "8\r\n00:02:00,000 --> 00:02:05,000\r\nQuest Airlock\r\n\r\n"
    "9\r\n00:02:30,000 --> 00:02:35,000\r\nHarmony Module\r\n"
)


def test_ac_eval_10_parse_srt_cues_bom_crlf_and_multiline():
    cues = parse_srt(FIXTURE)
    assert cues[0] == Cue(1.101, 10.627, "Step Inside The International Space Station")
    assert cues[1] == Cue(13.73, 19.052, "Columbus Laboratory Module")
    assert cues[2].text == "Europe's contribution, next door to Harmony, for the crew"
    # the malformed block is skipped, the others are kept in order
    assert len(cues) == 8
    assert [c.t_start for c in cues] == sorted(c.t_start for c in cues)
    assert cues[-1] == Cue(150.0, 155.0, "Harmony Module")


def test_ac_eval_10_parse_srt_hours_and_dot_millis():
    cues = parse_srt("1\n01:02:03.500 --> 01:02:04,000\nCupola\n")
    assert cues == [Cue(3723.5, 3724.0, "Cupola")]


def test_ac_eval_10_seed_modules_and_aliases():
    modules = srt_truth.load_seed_modules()
    for code in ("COL1", "LAB1", "NOD1", "NOD2", "NOD3", "CUP1", "JPM1", "ZARYA", "ZVEZDA", "NAUKA"):
        assert code in modules
    assert "USOS" not in modules and "NOD2D4" not in modules  # segments and zones are not modules
    aliases = srt_truth.module_aliases(modules)
    assert aliases["columbus"] == "COL1"
    assert aliases["kibo"] == "JPM1"  # pinned (JPM1 and JLP1 both say Kibo)
    assert aliases["harmony"] == "NOD2" and aliases["unity"] == "NOD1" and aliases["tranquility"] == "NOD3"
    assert aliases["destiny"] == "LAB1" and aliases["cupola"] == "CUP1"
    assert aliases["zarya"] == "ZARYA" and aliases["zvezda"] == "ZVEZDA" and aliases["nauka"] == "NAUKA"
    assert set(aliases.values()) <= set(modules)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Columbus Laboratory Module", "COL1"),
        ("Japanese Experiment Module “Kibo”", "JPM1"),
        ("Harmony Module", "NOD2"),
        ("Cupola", "CUP1"),
        ("Zvezda Service Module", "ZVEZDA"),
        ("Nauka Multipurpose Laboratory Module", "NAUKA"),
        ("Quest Airlock", None),  # a module the seed lacks: unmapped segment
        ("Pressurized Mating Adapters", None),
        ("Poisk Research Module", None),
    ],
)
def test_ac_eval_10_title_cues_name_a_module(text, expected):
    hit = srt_truth.match_module_title(text, srt_truth.module_aliases(srt_truth.load_seed_modules()))
    assert hit is not None
    assert hit[0] == expected


@pytest.mark.parametrize(
    "text",
    [
        "Step Inside The International Space Station",
        "Science on all sides",
        "Known as Node 2, Harmony connects international science labs",
        "before space shuttle Endeavour arrived to install the Unity modul",
        "Station's main U.S. laboratory, Destiny enables a wide variety of experiments",
        "",
    ],
)
def test_ac_eval_10_non_title_cues_are_ignored(text):
    assert srt_truth.match_module_title(text, srt_truth.module_aliases(srt_truth.load_seed_modules())) is None


def test_ac_eval_10_timeline_segments_until_next_module_cue_or_video_end():
    tl = module_timeline(parse_srt(FIXTURE), video_end_s=200.0)
    assert [(s.t_start, s.t_end, s.code) for s in tl.segments] == [
        (13.73, 41.324, "COL1"),
        (41.324, 120.0, "JPM1"),  # "Science on all sides" and the Harmony description do not cut it
        (120.0, 150.0, None),  # Quest airlock: unmapped
        (150.0, 200.0, "NOD2"),
    ]
    assert tl.module_at(5.0) is None  # title card: before the first module cue
    assert tl.module_at(13.73).code == "COL1"
    assert tl.module_at(100.0).code == "JPM1"
    assert tl.module_at(130.0).code is None and tl.module_at(130.0).name.lower().startswith("quest")
    assert tl.module_at(199.9).code == "NOD2"
    assert tl.module_at(200.0) is None
    assert tl.codes() == ["COL1", "JPM1", "NOD2"]


def test_ac_eval_10_timeline_defaults_end_to_last_cue_and_merges_repeated_module():
    srt = "1\n00:00:10,000 --> 00:00:12,000\nCupola\n\n2\n00:00:20,000 --> 00:00:22,000\nCupola\n\n" \
          "3\n00:00:30,000 --> 00:00:40,000\nCredits\n"
    tl = module_timeline(parse_srt(srt))
    assert [(s.t_start, s.t_end, s.code) for s in tl.segments] == [(10.0, 40.0, "CUP1")]


@pytest.mark.skipif(not TOUR_SRT.exists(), reason="NASA tour captions not downloaded")
def test_ac_eval_10_real_tour_srt_module_sequence():
    tl = module_timeline(parse_srt(TOUR_SRT.read_text(encoding="utf-8-sig")), video_end_s=897.0)
    seq = [s.code for s in tl.segments]
    assert seq[0] == "COL1"
    assert tl.segments[0].t_start == pytest.approx(13.73)
    assert seq == [
        "COL1", "JPM1", "NOD2", "LAB1", "NOD1", "CUP1",
        None,  # Quest Airlock 04:07
        None,  # Pressurized Mating Adapters 04:57 (a different unmapped module: its own segment)
        "ZARYA", "ZVEZDA", "NAUKA",
        None,  # Poisk 09:00 to the end
    ]
    assert tl.module_at(13 * 60).code is None  # credits are still the Poisk segment (no later title cue)
    assert tl.module_at(2 * 60 + 20).code == "LAB1"
    assert tl.module_at(3 * 60 + 30).code == "CUP1"
    assert tl.segments[-1].t_end == 897.0
