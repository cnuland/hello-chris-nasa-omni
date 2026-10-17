"""NASA Omni perception service (spec: docs/spec/20-perception.md).

Pipeline: ingest (ffmpeg) -> window builder -> fast path (detector + embedder) + omni window (vLLM-Omni)
-> reconciler (label->catalog, candidates, gates, scoring, decision model) -> inventory-api /ingest/window.

Model-backed implementations (OWLv2, DINOv2) import torch/transformers lazily; importing this package must not.
"""

__version__ = "0.1.0"

UNKNOWN = "UNKNOWN"
NEW = "NEW"
