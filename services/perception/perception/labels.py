"""AC-PER-7 label -> catalog mapping.

Order: exact class_name / catalog_id match (case/whitespace-insensitive) -> synonym match -> fuzzy string similarity
to class names, display names and synonyms -> embedding similarity (optional TextEmbedder) to the same names.
Best score below ``floor`` (default 0.55) -> ``UNKNOWN``. Only ids from the given catalog are ever returned.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any, Protocol

import numpy as np

from . import UNKNOWN
from .types import LabelMatch

DEFAULT_FLOOR = 0.55


def normalise_label(text: str) -> str:
    """Lower-case, punctuation to spaces, whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", str(text).lower())).strip()


def _bigrams(text: str) -> Counter[str]:
    padded = f" {text} "
    return Counter(padded[i : i + 2] for i in range(len(padded) - 1))


def fuzzy_similarity(a: str, b: str) -> float:
    """Conservative string similarity in [0, 1]: the minimum of character-bigram Dice and difflib's ratio.

    Both must agree, so short accidental overlaps ("the" vs "tape", "strange orb" vs "stowage bag") stay low while
    spelling variants ("multimetre", "flash light", "laptops") score high."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ba, bb = _bigrams(a), _bigrams(b)
    dice = 2.0 * sum((ba & bb).values()) / float(sum(ba.values()) + sum(bb.values()))
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return min(dice, ratio)


class TextEmbedder(Protocol):
    def embed_text(self, text: str) -> np.ndarray: ...


class CatalogMapper:
    def __init__(
        self,
        catalog: Sequence[dict[str, Any]],
        *,
        text_embedder: TextEmbedder | None = None,
        floor: float = DEFAULT_FLOOR,
    ):
        self.catalog = list(catalog)
        self.text_embedder = text_embedder
        self.floor = floor
        self._by_id = {c["catalog_id"]: c for c in self.catalog}
        self._exact: dict[str, str] = {}
        self._synonyms: dict[str, str] = {}
        self._names: list[tuple[str, str]] = []  # (normalised name, catalog_id), for fuzzy / embedding
        for item in self.catalog:
            cid = item["catalog_id"]
            for key in (item.get("class_name"), cid):
                if key:
                    self._exact.setdefault(normalise_label(key), cid)
            for syn in item.get("synonyms") or []:
                self._synonyms.setdefault(normalise_label(syn), cid)
            for name in (item.get("class_name"), item.get("display_name"), *(item.get("synonyms") or [])):
                if name and normalise_label(name):
                    self._names.append((normalise_label(name), cid))
        self._name_vectors: list[tuple[np.ndarray, str]] | None = None
        self._cache: dict[str, LabelMatch] = {}

    def item(self, catalog_id: str | None) -> dict[str, Any] | None:
        return self._by_id.get(catalog_id) if catalog_id else None

    def map(self, label: str) -> LabelMatch:
        key = normalise_label(label or "")
        if key not in self._cache:
            if len(self._cache) > 4096:
                self._cache.clear()
            self._cache[key] = self._map(key, str(label or "").strip())
        return self._cache[key]

    def _match(self, catalog_id: str, score: float, method: str) -> LabelMatch:
        return LabelMatch(
            catalog_id=catalog_id,
            score=float(score),
            method=method,
            class_name=self._by_id[catalog_id].get("class_name"),
        )

    def _map(self, key: str, raw: str) -> LabelMatch:
        if not key:
            return LabelMatch(catalog_id=UNKNOWN, score=0.0, method="none")
        if key in self._exact:
            return self._match(self._exact[key], 1.0, "exact")
        if key in self._synonyms:
            return self._match(self._synonyms[key], 1.0, "synonym")
        best_score, best_id = 0.0, None
        for name, cid in self._names:
            score = fuzzy_similarity(key, name)
            if score > best_score:
                best_score, best_id = score, cid
        if best_id is not None and best_score >= self.floor:
            # A fuzzy hit is never "exact": cap just below 1.0 (e.g. "flash light" vs "flashlight").
            return self._match(best_id, min(best_score, 0.999), "fuzzy")
        emb_score, emb_id = self._embedding_match(raw)
        if emb_id is not None and emb_score >= self.floor:
            return self._match(emb_id, emb_score, "embedding")
        return LabelMatch(catalog_id=UNKNOWN, score=float(max(best_score, emb_score, 0.0)), method="none")

    def _embedding_match(self, text: str) -> tuple[float, str | None]:
        if self.text_embedder is None:
            return 0.0, None
        if self._name_vectors is None:
            self._name_vectors = [(_unit(self.text_embedder.embed_text(name)), cid) for name, cid in self._names]
        query = _unit(self.text_embedder.embed_text(text))
        best_score, best_id = -1.0, None
        for vec, cid in self._name_vectors:
            score = float(np.dot(query, vec))
            if score > best_score:
                best_score, best_id = score, cid
        return max(best_score, 0.0), best_id


def _unit(vector: Any) -> np.ndarray:
    v = np.asarray(vector, dtype=np.float64).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v
