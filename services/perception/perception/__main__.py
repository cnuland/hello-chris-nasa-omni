"""CLI: ``python -m perception replay --source PATH --camera ID [--start-time ISO] [--loop]``.

Collaborators are built from the environment (PerceptionConfig.from_env): HttpInventoryClient(INVENTORY_API_URL),
omni (OMNI_URL; realtime when OMNI_REALTIME), OwlV2Detector, Dinov2Embedder, DecisionClient(DECISION_URL),
AsrClient(ASR_URL) or None, S3Storage. Then ``perception.replay.run`` is called (looked up on the module at call
time) and the ReplayStats are printed as JSON. Exit code 0 on success, 1 on runtime errors, 2 on usage errors.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("perception.cli")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="perception", description="NASA Omni perception service")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True
    replay = sub.add_parser(
        "replay",
        help="run the pipeline over a video file or stream URL",
        description="Run ingest -> windows -> detector/embedder + omni -> reconciler -> "
        "inventory-api over a video source.",
    )
    replay.add_argument("--source", required=True, help="video file path or stream URL")
    replay.add_argument("--camera", required=True, help="camera id (must exist in inventory-api)")
    replay.add_argument(
        "--start-time", default=None, help="wall-clock time of media pts 0 (ISO 8601; default: now, UTC)"
    )
    replay.add_argument("--loop", action="store_true", help="loop a file source forever (or --max-loops times)")
    replay.add_argument("--max-loops", type=int, default=None, help="number of passes when looping")
    return parser


def _start_time(raw: str | None, parser: argparse.ArgumentParser) -> datetime:
    if not raw:
        return datetime.now(UTC)
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        parser.error(f"--start-time must be ISO 8601, got {raw!r}")
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def build_collaborators(config: Any) -> dict[str, Any]:
    """Real collaborators from the configuration (no network calls are made here)."""
    from .asr import AsrClient
    from .decision import DecisionClient
    from .detector import OwlV2Detector
    from .embedder import Dinov2Embedder
    from .inventory_client import HttpInventoryClient
    from .omni import create_omni_client
    from .storage import S3Storage

    missing = [
        name
        for name, value in (
            ("INVENTORY_API_URL", config.inventory_api_url),
            ("OMNI_URL", config.omni_url),
            ("DECISION_URL", config.decision_url),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"missing required environment variables: {', '.join(missing)}")
    from .config import resolve_device

    device = resolve_device(config.device)
    return {
        "api": HttpInventoryClient(config.inventory_api_url),
        "omni": create_omni_client(config),  # catalog names are filled in by replay.run
        "detector": OwlV2Detector(min_score=config.min_score, device=device),
        "embedder": Dinov2Embedder(device=device),
        "decision": DecisionClient(
            config.decision_url, model=config.decision_model, timeout_s=config.decision_timeout_s
        ),
        "asr": AsrClient(config.asr_url, model=config.asr_model) if config.asr_url else None,
        "storage": S3Storage.from_config(config),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse: 0 for --help, 2 for usage errors
        return int(exc.code or 0)

    from . import replay
    from .config import ConfigError, PerceptionConfig
    from .logging_config import configure_logging

    configure_logging()
    start_time = _start_time(args.start_time, parser)
    try:
        config = PerceptionConfig.from_env()
        collaborators = build_collaborators(config)
    except (ConfigError, ValueError) as exc:
        print(f"perception: configuration error: {exc}", file=sys.stderr)
        return 2
    if args.camera not in config.camera_locations:
        # Privacy fails closed: without a location the camera's zone (crew quarters? WHC?) is unknown.
        print(
            f"perception: configuration error: camera {args.camera!r} has no location; add it to CAMERA_LOCATIONS",
            file=sys.stderr,
        )
        return 2
    try:
        stats = replay.run(
            args.source,
            camera_id=args.camera,
            start_time=start_time,
            config=config,
            loop=args.loop,
            max_loops=args.max_loops,
            **collaborators,
        )
    except Exception as exc:
        logger.exception("replay failed")
        print(f"perception: replay failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(dataclasses.asdict(stats)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
