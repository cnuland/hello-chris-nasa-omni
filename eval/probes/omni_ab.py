"""A/B probe: run the SAME video windows through several omni endpoints with perception's real client.

Runs inside the perception image (it has ffmpeg, the package and the sample video volume). Usage:
  python omni_ab.py --source /media/sample.mp4 --windows 8 --skip 10 \
      --target qwen=http://omni.nasa-inference.svc:8000,omni --target gemma=http://gemma4.nasa-inference.svc:8000,gemma4
Writes one JSON line per (target, window) to stdout: status, latency, tokens, objects (label, bbox), location hint.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import UTC, datetime

from perception.config import PerceptionConfig
from perception.ingest import ingest
from perception.omni import create_omni_client
from perception.window import WindowBuilder


def windows(source: str, n: int, skip: int, cfg: PerceptionConfig):
    builder = WindowBuilder(n_frames=cfg.window_frames, stride_s=cfg.stride_s, long_side=cfg.long_side,
                            detect_long_side=cfg.detect_long_side)
    seen = 0
    for item in ingest(source, start_time=datetime(2026, 10, 1, tzinfo=UTC), fps=cfg.fps, chunk_s=cfg.audio_chunk_s,
                       sample_rate=cfg.audio_sample_rate):
        for w in builder.push(item):
            seen += 1
            if seen > skip:
                yield w
                if seen - skip >= n:
                    return


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--windows", type=int, default=6)
    ap.add_argument("--skip", type=int, default=0)
    ap.add_argument("--target", action="append", required=True, help="name=url,model")
    a = ap.parse_args()
    base = PerceptionConfig.from_env()
    targets = []
    for t in a.target:
        name, rest = t.split("=", 1)
        url, model = rest.split(",", 1)
        cfg = dataclasses.replace(base, omni_url=url, omni_model=model)
        targets.append((name, create_omni_client(cfg)))
    for i, w in enumerate(windows(a.source, a.windows, a.skip, base)):
        for name, client in targets:
            r = client.analyze(w)
            obs = r.observation or {}
            print(json.dumps({
                "target": name, "window": i + a.skip, "t": w.t_start.isoformat(), "status": r.status,
                "latency_ms": r.latency_ms, "attempts": r.attempts, "truncated": r.truncated,
                "usage": getattr(r, "usage", None), "finish": getattr(r, "finish_reason", None),
                "location_hint": obs.get("location_hint"), "summary": obs.get("scene_summary"),
                "objects": [{"label": o.get("label"), "bbox": o.get("bbox"), "f": o.get("frame_index"),
                             "conf": o.get("confidence")} for o in obs.get("objects", [])],
                "people": len(obs.get("people", [])), "events": len(obs.get("events", [])),
                "error": r.error, "raw_head": (r.raw or "")[:300] if r.status != "ok" else None,
            }), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
