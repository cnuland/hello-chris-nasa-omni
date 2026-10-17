"""AC-PER-5 detector interface ``detect(frame, labels) -> [Detection(label, bbox, score)]``.

OwlV2Detector uses transformers lazily (no torch/transformers import at module import time). Detections with
``score < min_score`` (default 0.15) are dropped.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Protocol

import numpy as np

from .types import Detection

DEFAULT_MIN_SCORE = 0.15

logger = logging.getLogger(__name__)


class Detector(Protocol):
    def detect(self, frame: np.ndarray, labels: Sequence[str]) -> list[Detection]: ...


def filter_detections(detections: Sequence[Detection], min_score: float = DEFAULT_MIN_SCORE) -> list[Detection]:
    """Keep detections with ``score >= min_score`` (order preserved)."""
    return [d for d in detections if d.score >= min_score]


class OwlV2Detector:
    def __init__(
        self,
        model_name: str = "google/owlv2-base-patch16-ensemble",
        *,
        min_score: float = DEFAULT_MIN_SCORE,
        device: str = "cpu",
    ):
        self.model_name = model_name
        self.min_score = min_score
        self.device = device
        self._processor: Any = None
        self._model: Any = None

    def _load(self) -> None:
        if self._model is not None:
            return
        # Lazy imports: importing perception must never pull in torch/transformers (AC-PER-5).
        from transformers import Owlv2ForObjectDetection, Owlv2Processor

        logger.info("loading OWLv2", extra={"model": self.model_name, "device": self.device})
        self._processor = Owlv2Processor.from_pretrained(self.model_name)
        self._model = Owlv2ForObjectDetection.from_pretrained(self.model_name).to(self.device).eval()

    def _post_process(self, outputs: Any, target_sizes: Any) -> dict[str, Any]:
        """transformers 5.x moved ``post_process_object_detection`` off ``Owlv2Processor`` (it remains on the image
        processor; the processor only has ``post_process_grounded_object_detection``). Support both layouts."""
        image_processor = getattr(self._processor, "image_processor", None)
        if hasattr(image_processor, "post_process_object_detection"):
            return image_processor.post_process_object_detection(
                outputs, threshold=self.min_score, target_sizes=target_sizes
            )[0]
        if hasattr(self._processor, "post_process_object_detection"):  # transformers 4.x
            return self._processor.post_process_object_detection(
                outputs, threshold=self.min_score, target_sizes=target_sizes
            )[0]
        return self._processor.post_process_grounded_object_detection(
            outputs, threshold=self.min_score, target_sizes=target_sizes
        )[0]

    def detect(self, frame: np.ndarray, labels: Sequence[str]) -> list[Detection]:
        """Open-vocabulary detection of ``labels``; bboxes normalised to the frame; filtered at ``min_score``."""
        labels = [str(lbl) for lbl in labels if str(lbl).strip()]
        if not labels:
            return []
        self._load()
        import torch
        from PIL import Image

        h, w = frame.shape[:2]
        image = Image.fromarray(np.ascontiguousarray(frame, dtype=np.uint8))
        inputs = self._processor(text=[labels], images=image, return_tensors="pt").to(self.device)
        with torch.no_grad():
            outputs = self._model(**inputs)
        # OWLv2 pads the image to a square before resizing: post-process against the padded size.
        side = max(h, w)
        results = self._post_process(outputs, torch.tensor([[side, side]], device=self.device))
        detections = []
        for score, label_idx, box in zip(
            results["scores"].tolist(), results["labels"].tolist(), results["boxes"].tolist(), strict=True
        ):
            x1, y1, x2, y2 = box
            bbox = (
                min(max(x1 / w, 0.0), 1.0),
                min(max(y1 / h, 0.0), 1.0),
                min(max(x2 / w, 0.0), 1.0),
                min(max(y2 / h, 0.0), 1.0),
            )
            if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                continue
            detections.append(Detection(labels[int(label_idx)], bbox, float(score)))
        return filter_detections(detections, self.min_score)
