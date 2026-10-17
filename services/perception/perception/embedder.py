"""AC-PER-6 embedder interface ``embed(crop) -> 768-d L2-normalised float32 vector``.

Dinov2Embedder (facebook/dinov2-base) imports torch/transformers lazily.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import numpy as np

EMBEDDING_DIM = 768

logger = logging.getLogger(__name__)


def l2_normalise(vector: np.ndarray) -> np.ndarray:
    """float32 unit vector (a zero vector is returned unchanged)."""
    v = np.asarray(vector, dtype=np.float64).reshape(-1)
    norm = float(np.linalg.norm(v))
    if norm == 0.0:
        return v.astype(np.float32)
    return (v / norm).astype(np.float32)


class Embedder(Protocol):
    def embed(self, crop: np.ndarray) -> np.ndarray: ...


class Dinov2Embedder:
    def __init__(self, model_name: str = "facebook/dinov2-base", *, device: str = "cpu"):
        self.model_name = model_name
        self.device = device
        self._processor: Any = None
        self._model: Any = None

    def _load(self) -> None:
        if self._model is not None:
            return
        # Lazy imports (AC-PER-5/6): torch/transformers are only needed when a real embedding is requested.
        from transformers import AutoImageProcessor, AutoModel

        logger.info("loading DINOv2", extra={"model": self.model_name, "device": self.device})
        self._processor = AutoImageProcessor.from_pretrained(self.model_name)
        self._model = AutoModel.from_pretrained(self.model_name).to(self.device).eval()

    def embed(self, crop: np.ndarray) -> np.ndarray:
        """CLS embedding of the crop, L2-normalised, float32, 768-d."""
        self._load()
        import torch
        from PIL import Image

        image = Image.fromarray(np.ascontiguousarray(crop, dtype=np.uint8)).convert("RGB")
        inputs = self._processor(images=image, return_tensors="pt").to(self.device)
        with torch.no_grad():
            outputs = self._model(**inputs)
        cls = outputs.last_hidden_state[0, 0].float().cpu().numpy()
        return l2_normalise(cls)
