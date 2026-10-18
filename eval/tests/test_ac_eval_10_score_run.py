"""AC-EVAL-10 scoring a live (or exported) run against the caption module timeline (docs/spec/50-eval.md).

Hand-computed fixture (first_offset_s = 0, timeline: COL1 10-40, JPM1 40-70, Quest (unmapped) 70-100, NOD2 100-130):

  window  video   sightings                 predicted    truth@mid   scored
  w0      0-0     USOS                      unresolved   (title)     no
  w1      15-20   COL1A1, COL1, USOS        COL1         COL1        yes, correct
  w2      45-50   NOD2, NOD2, JPM1          NOD2         JPM1        yes, wrong
  w3      55-60   USOS                      unresolved   JPM1        yes, wrong
  w4      75-80   COL1                      COL1         unmapped    no
  w5      105-110 NOD2                      NOD2         NOD2        yes, correct

window_location_acc = 2/4; modules reached = COL1, JPM1, NOD2, correctly seen = COL1, NOD2 -> module_coverage 2/3.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import yaml

from nasa_eval import THRESHOLDS_PATH
from nasa_eval.gate import load_report_only, load_thresholds
from nasa_eval.real import score_run
from nasa_eval.real.score_run import Alignment, RunData, Window
from nasa_eval.real.srt_truth import module_timeline, parse_srt

T = datetime(2026, 10, 18, 3, 0, 0, tzinfo=UTC)

SRT = (
    "1\n00:00:10,000 --> 00:00:12,000\nColumbus Laboratory Module\n\n"
    "2\n00:00:20,000 --> 00:00:25,000\nEurope's lab, next to Harmony\n\n"
    "3\n00:00:40,000 --> 00:00:42,000\nJapanese Experiment Module “Kibo”\n\n"
    "4\n00:01:10,000 --> 00:01:12,000\nQuest Airlock\n\n"
    "5\n00:01:40,000 --> 00:01:42,000\nHarmony Module\n"
)

LOCATIONS = [
    {"location_id": "USOS", "parent_id": None, "kind": "segment", "display_name": "US Orbital Segment"},
    {"location_id": "COL1", "parent_id": "USOS", "kind": "module", "display_name": "Columbus"},
    {"location_id": "COL1A1", "parent_id": "COL1", "kind": "zone", "display_name": "Columbus aft rack 1"},
    {"location_id": "JPM1", "parent_id": "USOS", "kind": "module", "display_name": "Kibo"},
    {"location_id": "NOD2", "parent_id": "USOS", "kind": "module", "display_name": "Node 2 Harmony"},
    {"location_id": "LAB1", "parent_id": "USOS", "kind": "module", "display_name": "Destiny"},
]

# (window start s, window end s, [(location, seen_at s)])
WINDOWS = [
    (0, 0, [("USOS", 0)]),
    (15, 20, [("COL1A1", 15), ("COL1", 17), ("USOS", 20)]),
    (45, 50, [("NOD2", 45), ("NOD2", 46), ("JPM1", 50)]),
    (55, 60, [("USOS", 58)]),
    (75, 80, [("COL1", 76)]),
    (105, 110, [("NOD2", 107)]),
]


def at(s: float) -> datetime:
    return T + timedelta(seconds=s)


def run_data(windows=WINDOWS) -> RunData:
    return RunData(
        windows=[Window(at(a), at(b), [(loc, at(t)) for loc, t in sights]) for a, b, sights in windows],
        locations=LOCATIONS,
    )


@pytest.fixture
def timeline():
    return module_timeline(parse_srt(SRT), video_end_s=130.0)


def test_ac_eval_10_score_hand_computed(timeline):
    result = score_run.score(run_data(), timeline, Alignment(first_offset_s=0.0, video_duration_s=None))
    m = result["metrics"]
    assert m["window_location_acc"] == pytest.approx(0.5)
    assert m["module_coverage"] == pytest.approx(2 / 3)
    assert m["windows_total"] == 6
    assert m["windows_scored"] == 4
    assert m["windows_unresolved"] == 1  # scored windows without any module-level sighting
    assert result["modules_reached"] == ["COL1", "JPM1", "NOD2"]
    assert result["modules_in_captions"] == ["COL1", "JPM1", "NOD2"]
    assert result["confusion"] == {
        "COL1": {"COL1": 1},
        "JPM1": {"NOD2": 1, "unresolved": 1},
        "NOD2": {"NOD2": 1},
    }
    rows = result["windows"]
    assert [r["video_t_mid"] for r in rows] == [0.0, 17.5, 47.5, 57.5, 77.5, 107.5]
    assert [r["predicted"] for r in rows] == ["unresolved", "COL1", "NOD2", "unresolved", "COL1", "NOD2"]
    assert [r["truth"] for r in rows] == [None, "COL1", "JPM1", "JPM1", None, "NOD2"]
    assert [r["scored"] for r in rows] == [False, True, True, True, False, True]
    assert rows[4]["truth_name"].lower().startswith("quest")


def test_ac_eval_10_alignment_offset_is_configurable(timeline):
    # shift the whole run 30 s later in the video: w1 (17.5 -> 47.5) now falls in Kibo
    result = score_run.score(run_data(), timeline, Alignment(first_offset_s=30.0, video_duration_s=None))
    assert result["windows"][1]["video_t_mid"] == pytest.approx(47.5)
    assert result["windows"][1]["truth"] == "JPM1"
    assert result["alignment"] == {"first_offset_s": 30.0, "video_duration_s": None}


def test_ac_eval_10_looping_replay_wraps_at_video_duration(timeline):
    windows = [*WINDOWS, (130 + 15, 130 + 20, [("COL1", 130 + 16)])]
    result = score_run.score(run_data(windows), timeline, Alignment(first_offset_s=0.0, video_duration_s=130.0))
    last = result["windows"][-1]
    assert last["pass"] == 1 and last["video_t_mid"] == pytest.approx(17.5) and last["truth"] == "COL1"
    assert result["metrics"]["window_location_acc"] == pytest.approx(3 / 5)
    assert result["metrics"]["passes"] == 2


def test_ac_eval_10_modal_location_tie_breaks_on_earliest_sighting(timeline):
    windows = [(15, 20, [("NOD2", 18), ("COL1", 16), ("COL1A1", 19), ("NOD2", 17)])]
    result = score_run.score(run_data(windows), timeline, Alignment())
    assert result["windows"][0]["predicted"] == "COL1"


def test_ac_eval_10_empty_run_has_no_accuracy(timeline):
    result = score_run.score(RunData(windows=[], locations=LOCATIONS), timeline, Alignment())
    assert result["metrics"]["window_location_acc"] is None
    assert result["metrics"]["module_coverage"] is None
    assert result["metrics"]["windows_total"] == 0


# ------------------------------------------------------------------------------------------------ inputs
def _fake_api(requests: list[httpx.Request]) -> httpx.MockTransport:
    items, histories = [], {}
    for i, (_, _, sights) in enumerate(WINDOWS):
        for j, (loc, t) in enumerate(sights):
            eid = f"00000000-0000-0000-0000-{i:06d}{j:06d}"
            items.append({"entity_id": eid, "status": "provisional", "label": "x"})
            histories[eid] = [
                {"at": at(t + 1).isoformat().replace("+00:00", "Z"), "kind": "audit", "location_id": None},
                {"at": at(t).isoformat().replace("+00:00", "Z"), "kind": "sighting", "location_id": loc},
                {"at": at(t).isoformat(), "kind": "event", "location_id": "LAB1"},
            ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"  # read-only
        path = request.url.path
        if path == "/api/locations":
            return httpx.Response(200, json=LOCATIONS)
        if path == "/api/items":
            limit, offset = int(request.url.params["limit"]), int(request.url.params["offset"])
            return httpx.Response(200, json={"items": items[offset : offset + limit], "total": len(items)})
        if path.startswith("/api/items/") and path.endswith("/history"):
            return httpx.Response(200, json=histories[path.split("/")[3]])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_ac_eval_10_fetch_api_rebuilds_windows_from_sightings(timeline):
    requests: list[httpx.Request] = []
    data = score_run.fetch_api("https://inv.example/api", transport=_fake_api(requests), page_size=4)
    assert [(w.t_start, w.t_end) for w in data.windows] == [
        (at(0), at(0)), (at(15), at(20)), (at(45), at(50)), (at(58), at(58)), (at(76), at(76)), (at(107), at(107))
    ]
    assert sorted(loc for loc, _ in data.windows[1].sightings) == ["COL1", "COL1A1", "USOS"]
    item_reqs = [r for r in requests if r.url.path == "/api/items"]
    assert len(item_reqs) == 3  # 10 items, pages of 4
    statuses = set(item_reqs[0].url.params.get_list("status"))
    assert statuses == {"confirmed", "provisional", "unknown", "merged", "dismissed"}
    result = score_run.score(data, timeline, Alignment())
    assert result["metrics"]["window_location_acc"] == pytest.approx(0.5)


def _write_export(path: Path) -> Path:
    export = {
        "windows": [
            {
                "window_id": f"w{i}",
                "t_start": at(a).isoformat(),
                "t_end": at(b).isoformat(),
                "sightings": [{"location_id": loc, "seen_at": at(t).isoformat()} for loc, t in sights] or None,
            }
            for i, (a, b, sights) in enumerate([*WINDOWS, (120, 125, [])])  # a window without sightings
        ],
        "locations": LOCATIONS,
    }
    path.write_text(json.dumps(export))
    return path


def test_ac_eval_10_load_export_keeps_windows_without_sightings(tmp_path, timeline):
    data = score_run.load_export(_write_export(tmp_path / "export.json"))
    assert len(data.windows) == 7 and data.windows[-1].sightings == []
    result = score_run.score(data, timeline, Alignment())
    assert result["metrics"]["windows_scored"] == 5  # the empty NOD2 window is scored as unresolved
    assert result["metrics"]["window_location_acc"] == pytest.approx(2 / 5)


def test_ac_eval_10_load_export_rejects_malformed(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"windows": "nope"}))
    with pytest.raises(ValueError):
        score_run.load_export(bad)


def test_ac_eval_10_export_sql_is_read_only():
    sql = score_run.EXPORT_SQL.lower()
    assert "observation_window" in sql and "sighting" in sql and "location" in sql
    for verb in ("insert", "update", "delete", "drop", "alter", "truncate"):
        assert verb not in sql.split()


# ------------------------------------------------------------------------------------------------ reports + CLI
def test_ac_eval_10_reports_json_and_markdown(tmp_path, timeline):
    result = score_run.score(run_data(), timeline, Alignment())
    result = score_run.with_targets(result, {"window_location_acc": load_thresholds_like(min_=0.7)})
    js, md = score_run.write_reports(result, tmp_path, "tour", now=datetime(2026, 10, 18, 4, 0, tzinfo=UTC))
    assert js.name == "20261018T040000Z-real-tour.json" and md.suffix == ".md"
    data = json.loads(js.read_text())
    assert data["kind"] == "real_video"
    assert data["targets"]["window_location_acc"] == {"min": 0.7, "max": None, "value": 0.5, "met": False}
    text = md.read_text()
    assert "window_location_acc" in text and "0.500" in text and "report-only" in text.lower()
    assert "| JPM1 |" in text  # confusion matrix row
    assert "first_offset_s" in text


def load_thresholds_like(min_=None, max_=None):
    from nasa_eval.gate import Threshold

    return Threshold("window_location_acc", min_, max_)


def test_ac_eval_10_cli_scores_an_export(tmp_path, capsys):
    srt = tmp_path / "tour.srt"
    srt.write_text(SRT)
    export = _write_export(tmp_path / "export.json")
    code = score_run.main([
        "--export", str(export), "--srt", str(srt), "--video-duration-s", "130", "--reports-dir", str(tmp_path / "r"),
        "--name", "fixture",
    ])
    assert code == 0  # report-only: a missed target does not fail
    out = capsys.readouterr().out
    assert "window_location_acc" in out
    assert len(list((tmp_path / "r").glob("*-real-fixture.json"))) == 1


def test_ac_eval_10_cli_print_export_sql(capsys):
    assert score_run.main(["--print-export-sql"]) == 0
    assert "observation_window" in capsys.readouterr().out


def test_ac_eval_10_cli_missing_srt_is_a_usage_error(tmp_path):
    export = _write_export(tmp_path / "export.json")
    assert score_run.main(["--export", str(export), "--srt", str(tmp_path / "missing.srt")]) == 2


# ------------------------------------------------------------------------------------------------ thresholds
def test_ac_eval_10_thresholds_real_video_section_is_report_only():
    raw = yaml.safe_load(THRESHOLDS_PATH.read_text())
    assert raw["report_only"]["real_video"]["window_location_acc"] == {"min": 0.7}
    assert "report_only" not in load_thresholds(THRESHOLDS_PATH)  # never gated
    assert "window_location_acc" not in load_thresholds(THRESHOLDS_PATH)
    real = load_report_only(THRESHOLDS_PATH, "real_video")
    assert real["window_location_acc"].min == 0.7


def test_ac_eval_10_load_report_only_validates(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("duplicate_rate: {max: 0.1}\nreport_only:\n  real_video:\n    window_location_acc: {min: nope}\n")
    with pytest.raises(ValueError):
        load_report_only(p, "real_video")
    assert set(load_thresholds(p)) == {"duplicate_rate"}
    p.write_text("duplicate_rate: {max: 0.1}\n")
    assert load_report_only(p, "real_video") == {}
