"""Score a live or exported pipeline run against the caption module timeline (AC-EVAL-10, docs/spec/50-eval.md).

``python -m nasa_eval.real.score_run [--api URL | --export FILE] [--srt tour.srt] ...`` writes
``eval/reports/real/<ts>-real-<name>.{json,md}`` (``kind: "real_video"``; report-only, never gated).

Data paths (both read-only):

* **API** (default, the live dashboard route): ``GET /locations``, ``GET /items?status=<all five>`` paged and
  ``GET /items/{id}/history`` per item. The API has no window list, so windows are rebuilt from the sighting times:
  a sighting more than ``window_gap_s`` after the first sighting of the current window starts a new one (the live
  windows are 5 s long every 30 s). Windows without any sighting are invisible on this path.
* **Export**: ``{"windows": [{t_start, t_end, sightings: [{location_id, seen_at}]}], "locations": [...]}`` from
  ``EXPORT_SQL`` run with psql inside the database pod (``--print-export-sql`` prints the command), which also sees
  the windows without sightings.

Alignment assumption (``Alignment``): video offset of a window = (t_start - first window t_start) + first_offset_s,
taken modulo ``video_duration_s`` when the replay loops; a window is judged at its mid time. The replay starts at
video t = 0 and its first window's frames start there at 1 fps, so the default first_offset_s is 0.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from nasa_eval import REPORTS_ROOT, THRESHOLDS_PATH
from nasa_eval.real.srt_truth import ModuleTimeline, module_timeline, parse_srt

DEFAULT_API = "https://dashboard-nasa-app.apps.bgrr25lx.eastus.aroapp.io/api"
DEFAULT_SRT = Path(os.environ.get("NASA_TOUR_SRT", Path.home() / "nasa-media" / "iss-tour" / "tour.srt"))
DEFAULT_VIDEO_DURATION_S = 897.0  # "Step Inside the ISS" tour, 0:14:57
DEFAULT_REPORTS_DIR = REPORTS_ROOT / "real"
ENTITY_STATUSES = ("confirmed", "provisional", "unknown", "merged", "dismissed")
UNRESOLVED = "unresolved"
REPORT_KIND = "real_video"

EXPORT_SQL = """SELECT json_build_object(
  'windows', coalesce((SELECT json_agg(json_build_object(
      'window_id', w.window_id, 'camera_id', w.camera_id, 't_start', w.t_start, 't_end', w.t_end,
      'sightings', (SELECT json_agg(json_build_object('location_id', s.location_id, 'seen_at', s.seen_at)
                                    ORDER BY s.seen_at)
                      FROM sighting s WHERE s.window_id = w.window_id))
    ORDER BY w.t_start) FROM observation_window w), '[]'::json),
  'locations', (SELECT json_agg(json_build_object('location_id', l.location_id, 'parent_id', l.parent_id,
                                                  'kind', l.kind, 'display_name', l.display_name))
                  FROM location l))"""

EXPORT_HOWTO = (
    "oc exec -n nasa-data <primary db pod, e.g. inventory-db-1> -c postgres -- "
    "psql -U postgres -d inventory -At -c \"$(python -m nasa_eval.real.score_run --print-export-sql)\" > export.json"
)


@dataclass(frozen=True)
class Window:
    t_start: datetime
    t_end: datetime
    sightings: list[tuple[str | None, datetime]]  # (location_id, seen_at)


@dataclass(frozen=True)
class RunData:
    windows: list[Window]
    locations: list[Mapping[str, Any]]


@dataclass(frozen=True)
class Alignment:
    first_offset_s: float = 0.0  # video time of the first window's t_start
    video_duration_s: float | None = DEFAULT_VIDEO_DURATION_S  # wrap for a looping replay; None = no wrap


# ------------------------------------------------------------------------------------------------ inputs
def _require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def _ts(value: Any) -> datetime:
    _require(isinstance(value, str), f"expected an ISO timestamp, got {value!r}")
    dt = datetime.fromisoformat(value)  # Python 3.11 accepts a trailing Z
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _windows_from_sightings(sightings: list[tuple[str | None, datetime]], gap_s: float) -> list[Window]:
    windows: list[Window] = []
    current: list[tuple[str | None, datetime]] = []
    for s in sorted(sightings, key=lambda x: x[1]):
        if current and (s[1] - current[0][1]).total_seconds() > gap_s:
            windows.append(Window(current[0][1], current[-1][1], current))
            current = []
        current.append(s)
    if current:
        windows.append(Window(current[0][1], current[-1][1], current))
    return windows


def fetch_api(
    base_url: str = DEFAULT_API,
    *,
    transport: httpx.BaseTransport | None = None,
    page_size: int = 500,
    window_gap_s: float = 10.0,
    verify: bool = False,
    timeout: float = 30.0,
) -> RunData:
    """Read-only GETs against the inventory API; windows are rebuilt from the sighting times (see module doc)."""
    with httpx.Client(base_url=base_url.rstrip("/"), transport=transport, verify=verify, timeout=timeout) as client:

        def get(path: str, params: Any = None) -> Any:
            r = client.get(path, params=params)
            r.raise_for_status()
            return r.json()

        locations = get("/locations")
        entity_ids: list[str] = []
        offset = 0
        while True:
            params = [("status", s) for s in ENTITY_STATUSES] + [("limit", page_size), ("offset", offset)]
            page = get("/items", params)
            items = page.get("items") or []
            entity_ids += [str(uuid.UUID(str(it["entity_id"]))) for it in items]  # ids go into a path: validate
            offset += len(items)
            if not items or offset >= int(page.get("total", 0)):
                break
        sightings: list[tuple[str | None, datetime]] = []
        for eid in dict.fromkeys(entity_ids):
            for entry in get(f"/items/{eid}/history", {"limit": 2000, "collapse": "true"}):
                if entry.get("kind") == "sighting":
                    sightings.append((entry.get("location_id"), _ts(entry["at"])))
    _require(isinstance(locations, list), "GET /locations did not return a list")
    return RunData(_windows_from_sightings(sightings, window_gap_s), locations)


def load_export(path: str | Path) -> RunData:
    """Parse a JSON export (``EXPORT_SQL``). Raises ValueError when malformed."""
    try:
        data = json.loads(Path(path).read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON ({exc})") from exc
    _require(isinstance(data, dict) and isinstance(data.get("windows"), list), f"{path}: expected a 'windows' list")
    locations = data.get("locations") or []
    _require(isinstance(locations, list), f"{path}: 'locations' must be a list")
    windows = []
    for w in data["windows"]:
        _require(isinstance(w, dict), f"{path}: window entries must be objects")
        sights = w.get("sightings") or []
        _require(isinstance(sights, list) and all(isinstance(x, dict) for x in sights), f"{path}: bad sightings")
        sightings = [(x.get("location_id"), _ts(x.get("seen_at"))) for x in sights]
        windows.append(Window(_ts(w.get("t_start")), _ts(w.get("t_end")), sightings))
    return RunData(sorted(windows, key=lambda w: w.t_start), locations)


# ------------------------------------------------------------------------------------------------ scoring
def _module_resolver(locations: Sequence[Mapping[str, Any]]):
    by_id = {str(loc.get("location_id")): loc for loc in locations}

    def module_of(location_id: str | None) -> str | None:
        loc_id, depth = location_id, 0
        while loc_id is not None and loc_id in by_id and depth < 32:
            loc = by_id[loc_id]
            if loc.get("kind") == "module":
                return loc_id
            loc_id, depth = loc.get("parent_id"), depth + 1
        return None  # segment level (USOS/ROS), unknown or no location

    return module_of


def _modal_module(window: Window, module_of) -> str:
    counts: Counter[str] = Counter()
    first_seen: dict[str, datetime] = {}
    for loc, seen_at in window.sightings:
        mod = module_of(loc)
        if mod is None:
            continue
        counts[mod] += 1
        first_seen[mod] = min(seen_at, first_seen.get(mod, seen_at))
    if not counts:
        return UNRESOLVED
    return min(counts, key=lambda m: (-counts[m], first_seen[m], m))


def score(run: RunData, timeline: ModuleTimeline, alignment: Alignment | None = None) -> dict[str, Any]:
    """Per-window predicted vs caption module, ``window_location_acc``, ``module_coverage`` and the confusion
    matrix (truth -> predicted -> count, scored windows only)."""
    alignment = alignment or Alignment()
    module_of = _module_resolver(run.locations)
    windows = sorted(run.windows, key=lambda w: w.t_start)
    t0 = windows[0].t_start if windows else None
    dur = alignment.video_duration_s or None
    rows: list[dict[str, Any]] = []
    confusion: dict[str, dict[str, int]] = {}
    for w in windows:
        offset = (w.t_start - t0).total_seconds() + alignment.first_offset_s  # type: ignore[operator]
        n_pass = int(offset // dur) if dur else 0
        offset -= n_pass * (dur or 0.0)
        mid = round(offset + (w.t_end - w.t_start).total_seconds() / 2, 3)
        seg = timeline.module_at(mid)
        predicted = _modal_module(w, module_of)
        scored = seg is not None and seg.code is not None
        rows.append(
            {
                "t_start": w.t_start.isoformat(),
                "pass": n_pass,
                "video_t_mid": mid,
                "predicted": predicted,
                "truth": seg.code if seg else None,
                "truth_name": seg.name if seg else None,
                "scored": scored,
                "correct": scored and predicted == seg.code,  # type: ignore[union-attr]
                "sightings": len(w.sightings),
            }
        )
        if scored:
            confusion.setdefault(seg.code, {}).setdefault(predicted, 0)  # type: ignore[union-attr, arg-type]
            confusion[seg.code][predicted] += 1  # type: ignore[index, union-attr]
    scored_rows = [r for r in rows if r["scored"]]
    reached = [c for c in timeline.codes() if c in confusion]
    covered = [c for c in reached if confusion[c].get(c)]
    metrics = {
        "window_location_acc": (sum(r["correct"] for r in scored_rows) / len(scored_rows)) if scored_rows else None,
        "module_coverage": (len(covered) / len(reached)) if reached else None,
        "windows_total": len(rows),
        "windows_scored": len(scored_rows),
        "windows_correct": sum(r["correct"] for r in scored_rows),
        "windows_unresolved": sum(r["predicted"] == UNRESOLVED for r in scored_rows),
        "windows_unscored": len(rows) - len(scored_rows),
        "passes": (max(r["pass"] for r in rows) + 1) if rows else 0,
    }
    return {
        "metrics": metrics,
        "confusion": confusion,
        "modules_in_captions": timeline.codes(),
        "modules_reached": reached,
        "modules_covered": covered,
        "alignment": {"first_offset_s": alignment.first_offset_s, "video_duration_s": alignment.video_duration_s},
        "timeline": [
            {"t_start": s.t_start, "t_end": s.t_end, "code": s.code, "name": s.name} for s in timeline.segments
        ],
        "windows": rows,
    }


def with_targets(result: dict[str, Any], thresholds: Mapping[str, Any]) -> dict[str, Any]:
    """Attach report-only targets: metric -> {min, max, value, met}."""
    targets = {}
    for name, th in thresholds.items():
        value = result["metrics"].get(name)
        ok = isinstance(value, int | float) and not isinstance(value, bool) and not math.isnan(value)
        met = ok and (th.min is None or value >= th.min) and (th.max is None or value <= th.max)
        targets[name] = {"min": th.min, "max": th.max, "value": value, "met": bool(met)}
    return {**result, "targets": targets}


# ------------------------------------------------------------------------------------------------ reports
def _fmt(v: Any) -> str:
    return f"{v:.3f}" if isinstance(v, float) else ("n/a" if v is None else str(v))


def _markdown(report: Mapping[str, Any]) -> str:
    m, a = report["metrics"], report["alignment"]
    lines = [
        f"# Real-video location eval: {report['name']}",
        "",
        (
            f"Generated {report['generated_at']} from {report['source']}. Ground truth: caption title cues "
            "(docs/spec/50-eval.md AC-EVAL-10). **Report-only**: never gated."
        ),
        "",
        (
            f"Alignment: video offset = (window t_start - first window t_start) + first_offset_s "
            f"(first_offset_s = {a['first_offset_s']}), modulo video_duration_s = {a['video_duration_s']}; "
            "judged at the window's mid time."
        ),
        "",
        "| metric | value |",
        "|---|---|",
        *[f"| {k} | {_fmt(v)} |" for k, v in m.items()],
        "",
    ]
    if report.get("targets"):
        lines += ["## Targets (report-only)", "", "| metric | target | value | met |", "|---|---|---|---|"]
        for k, t in report["targets"].items():
            bound = " and ".join(p for p in (
                f">= {t['min']:g}" if t["min"] is not None else "", f"<= {t['max']:g}" if t["max"] is not None else ""
            ) if p)
            lines.append(f"| {k} | {bound} | {_fmt(t['value'])} | {'yes' if t['met'] else 'NO'} |")
        lines.append("")
    confusion = report["confusion"]
    cols = sorted({p for row in confusion.values() for p in row} | set(confusion), key=lambda c: (c == UNRESOLVED, c))
    lines += [
        "## Confusion matrix (rows = caption module, columns = predicted)",
        "",
        "| truth | " + " | ".join(cols) + " | n | acc |",
        "|---|" + "---|" * (len(cols) + 2),
    ]
    for truth in report["modules_reached"]:
        row = confusion[truth]
        n = sum(row.values())
        lines.append(
            f"| {truth} | " + " | ".join(str(row.get(c, 0)) for c in cols) + f" | {n} | {row.get(truth, 0) / n:.2f} |"
        )
    lines += [
        "",
        (
            f"Caption modules: {', '.join(report['modules_in_captions'])}; "
            f"reached: {', '.join(report['modules_reached']) or 'none'}; "
            f"correctly seen: {', '.join(report['modules_covered']) or 'none'}."
        ),
        "",
        "## Caption timeline",
        "",
        "| start s | end s | module | title cue |",
        "|---|---|---|---|",
        *[f"| {s['t_start']:.1f} | {s['t_end']:.1f} | {s['code'] or '(unmapped)'} | {s['name']} |"
          for s in report["timeline"]],
        "",
        "## Windows",
        "",
        "| pass | video mid s | predicted | truth | scored | correct | sightings |",
        "|---|---|---|---|---|---|---|",
        *[f"| {r['pass']} | {r['video_t_mid']:.1f} | {r['predicted']} | {r['truth'] or '-'} | "
          f"{'yes' if r['scored'] else 'no'} | {'yes' if r['correct'] else ''} | {r['sightings']} |"
          for r in report["windows"]],
        "",
    ]
    return "\n".join(lines)


def write_reports(
    result: Mapping[str, Any], out_dir: str | Path, name: str, *, source: str = "", now: datetime | None = None
) -> tuple[Path, Path]:
    now = now or datetime.now(UTC)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{now.strftime('%Y%m%dT%H%M%SZ')}-real-{name}"
    report = {"kind": REPORT_KIND, "name": name, "generated_at": now.isoformat(), "source": source, **result}
    js, md = out / f"{stem}.json", out / f"{stem}.md"
    js.write_text(json.dumps(report, indent=2, default=str))
    md.write_text(_markdown(report))
    return js, md


# ------------------------------------------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m nasa_eval.real.score_run",
        description="Real-video location eval of a run against the tour captions (AC-EVAL-10, report-only).",
        epilog=f"Export path: {EXPORT_HOWTO}",
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--api", default=DEFAULT_API, help=f"inventory API base URL, read-only (default {DEFAULT_API})")
    src.add_argument("--export", type=Path, help="JSON export from EXPORT_SQL instead of the API")
    p.add_argument("--srt", type=Path, default=DEFAULT_SRT, help=f"tour captions (default {DEFAULT_SRT})")
    p.add_argument("--video-duration-s", type=float, default=DEFAULT_VIDEO_DURATION_S,
                   help="video length: timeline end and loop wrap (0 = no wrap, end at the last cue)")
    p.add_argument("--first-offset-s", type=float, default=0.0, help="video time of the first window's t_start")
    p.add_argument("--window-gap-s", type=float, default=10.0, help="API mode: window rebuild gap")
    p.add_argument("--verify-tls", action="store_true", help="verify the API certificate (default off: self-signed)")
    p.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    p.add_argument("--name", default="tour")
    p.add_argument("--thresholds", type=Path, default=THRESHOLDS_PATH)
    p.add_argument("--print-export-sql", action="store_true", help="print EXPORT_SQL and exit")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    from nasa_eval.gate import load_report_only

    args = build_parser().parse_args(argv)
    if args.print_export_sql:
        print(EXPORT_SQL)
        return 0
    try:
        srt_text = args.srt.read_text(encoding="utf-8-sig")
    except OSError as exc:
        print(f"eval-real: cannot read captions {args.srt}: {exc}", file=sys.stderr)
        return 2
    duration = args.video_duration_s or None
    timeline = module_timeline(parse_srt(srt_text), video_end_s=duration)
    try:
        if args.export:
            run, source = load_export(args.export), f"export {args.export}"
        else:
            run = fetch_api(args.api, window_gap_s=args.window_gap_s, verify=args.verify_tls)
            source = f"API {args.api}"
        targets = load_report_only(args.thresholds, "real_video")
    except (OSError, ValueError, httpx.HTTPError) as exc:
        print(f"eval-real: {exc}", file=sys.stderr)
        return 2
    result = with_targets(score(run, timeline, Alignment(args.first_offset_s, duration)), targets)
    js, md = write_reports(result, args.reports_dir, args.name, source=source)
    m = result["metrics"]
    print(f"eval-real ({source}): " + ", ".join(f"{k}={_fmt(v)}" for k, v in m.items()))
    for truth in result["modules_reached"]:
        print(f"  {truth}: {result['confusion'][truth]}")
    for k, t in result["targets"].items():
        print(f"  target {k}: {_fmt(t['value'])} ({'met' if t['met'] else 'MISSED'}; report-only)")
    print(f"reports: {js} {md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
