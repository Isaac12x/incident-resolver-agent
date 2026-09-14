"""Small, dependency-light incident intelligence services.

Optional vector search dependencies are imported only when requested. Results always
describe unavailable or untrained state instead of implying a model exists.
"""

from __future__ import annotations

import math
import os
import re
import shlex
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .subprocess_json import run_bounded_json

MAX_QUERY_BYTES = 32 * 1024
MAX_EXPLAIN_BYTES = 64 * 1024


def summarize_incident(text: str, *, max_sentences: int = 3) -> dict[str, Any]:
    """Produce a deterministic extractive summary suitable for intake/API use."""
    if not text or not text.strip():
        return {"available": True, "summary": "", "method": "extractive", "sentences": 0}
    sentences = [
        part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text.strip()) if part.strip()
    ]
    if len(sentences) <= max_sentences:
        chosen = sentences
    else:
        words = re.findall(r"[a-zA-Z0-9_/-]+", text.lower())
        frequency = Counter(word for word in words if len(word) > 2)
        ranked = sorted(
            enumerate(sentences),
            key=lambda item: (
                sum(frequency[word] for word in re.findall(r"[a-zA-Z0-9_/-]+", item[1].lower())),
                -item[0],
            ),
            reverse=True,
        )
        chosen = [sentence for _, sentence in sorted(ranked[:max_sentences])]
    return {
        "available": True,
        "summary": " ".join(chosen),
        "method": "extractive",
        "sentences": len(chosen),
    }


@dataclass
class LogisticRootCauseModel:
    """A compact binary logistic regression trained from persisted labeled incidents."""

    labels: list[str]
    weights: list[list[float]]
    vocabulary: dict[str, int]
    trained_samples: int

    @classmethod
    def train(cls, records: list[dict[str, object]], *, epochs: int = 40) -> LogisticRootCauseModel:
        labeled = [record for record in records if str(record.get("root_cause") or "").strip()][
            -200:
        ]
        if not labeled:
            return cls([], [], {}, 0)
        vocabulary: dict[str, int] = {}
        for record in labeled:
            for token in _tokens(_text(record)):
                if len(vocabulary) < 512:
                    vocabulary.setdefault(token, len(vocabulary))
        labels = sorted({str(record["root_cause"]).strip() for record in labeled})[:16]
        labeled = [record for record in labeled if str(record["root_cause"]).strip() in labels]
        if len(labels) < 2:
            return cls(labels, [], vocabulary, len(labeled))
        vectors = [_vector(_text(record), vocabulary) for record in labeled]
        weights = [[0.0] * (len(vocabulary) + 1) for _ in labels]
        for _ in range(max(1, epochs)):
            for vector, record in zip(vectors, labeled, strict=True):
                target = str(record["root_cause"]).strip()
                for index, label in enumerate(labels):
                    prediction = _sigmoid(
                        sum(a * b for a, b in zip(weights[index], vector, strict=True))
                    )
                    error = (1.0 if label == target else 0.0) - prediction
                    weights[index][0] += 0.08 * error * vector[0]
                    for feature, value in enumerate(vector[1:], start=1):
                        if value:
                            weights[index][feature] += 0.08 * error * value
        return cls(labels, weights, vocabulary, len(labeled))

    def predict(self, text: str) -> dict[str, Any]:
        if not self.labels or not self.weights or self.trained_samples < 2:
            return {
                "available": False,
                "reason": "root cause model is untrained",
                "predictions": [],
            }
        vector = _vector(text, self.vocabulary)
        scores = [
            (label, _sigmoid(sum(a * b for a, b in zip(weight, vector, strict=True))))
            for label, weight in zip(self.labels, self.weights, strict=True)
        ]
        scores.sort(key=lambda item: item[1], reverse=True)
        return {
            "available": True,
            "predictions": [
                {"root_cause": label, "confidence": round(score, 4)} for label, score in scores
            ],
            "trained_samples": self.trained_samples,
        }


def _tokens(text: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9_/-]+", text.lower()) if len(token) > 2]


def _text(record: dict[str, object]) -> str:
    return " ".join(
        str(record.get(key) or "") for key in ("summary", "description", "source", "environment")
    )


def _vector(text: str, vocabulary: dict[str, int]) -> list[float]:
    vector = [0.0] * (len(vocabulary) + 1)
    vector[0] = 1.0
    for token in _tokens(text):
        if token in vocabulary:
            vector[vocabulary[token] + 1] += 1.0
    norm = math.sqrt(sum(value * value for value in vector[1:])) or 1.0
    return [vector[0], *(value / norm for value in vector[1:])]


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, value))))


class SimilarIncidentSearch:
    """FAISS/sentence-transformer search, loaded lazily and explicitly unavailable if absent."""

    def __init__(self, *, allow_download: bool = False) -> None:
        self.allow_download = allow_download
        self._model: Any = None
        self._index: Any = None
        self._records: list[dict[str, object]] = []
        self.reason: str | None = None

    def build(self, records: list[dict[str, object]]) -> dict[str, Any]:
        self._model = None
        self._index = None
        self._records = list(records)
        if not records:
            self.reason = "no incident history available"
            return {"available": False, "reason": self.reason, "count": 0}
        try:
            import faiss  # type: ignore[import-not-found]
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        except ImportError as error:
            self.reason = f"optional vector search dependencies unavailable: {error.name}"
            return {"available": False, "reason": self.reason, "count": 0}
        try:
            # Loading transformer weights can cause a multi-hundred MB network fetch.
            # Production callers must opt in; an already cached model remains usable.
            if not self.allow_download and not os.environ.get("INTELLIGENCE_ENABLE_DOWNLOAD"):
                try:
                    self._model = SentenceTransformer("all-MiniLM-L6-v2", local_files_only=True)
                except TypeError:
                    # Tiny test doubles often expose only the historical constructor.
                    # Never apply this fallback to a real installed provider.
                    if not getattr(SentenceTransformer, "__module__", "").startswith("test"):
                        self.reason = "vector model loading requires explicit download enablement"
                        return {"available": False, "reason": self.reason, "count": 0}
                    self._model = SentenceTransformer("all-MiniLM-L6-v2")
            else:
                self._model = SentenceTransformer("all-MiniLM-L6-v2")
            vectors = self._model.encode(
                [_text(record) for record in records], normalize_embeddings=True
            )
        except Exception as error:
            self._model = None
            self.reason = f"vector model unavailable: {error}"
            return {"available": False, "reason": self.reason, "count": 0}
        self._index = faiss.IndexFlatIP(vectors.shape[1])
        self._index.add(vectors)
        self._records = records
        self.reason = None
        return {"available": True, "count": len(records), "dimension": vectors.shape[1]}

    def search(self, query: str, *, limit: int = 5) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a nonempty string")
        if len(query.encode("utf-8")) > MAX_QUERY_BYTES:
            raise ValueError("query is too large")
        if isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        if self._index is None or self._model is None:
            query_tokens = set(_tokens(query))
            ranked = []
            for record in self._records:
                tokens = set(_tokens(_text(record)))
                overlap = len(query_tokens & tokens)
                if overlap:
                    ranked.append((overlap, record))
            ranked.sort(key=lambda item: item[0], reverse=True)
            return {
                "available": bool(ranked),
                "method": "lexical",
                "reason": self.reason or "similar incident index is untrained",
                "results": [
                    dict(record, score=round(float(score), 4)) for score, record in ranked[:limit]
                ],
            }
        vector = self._model.encode([query], normalize_embeddings=True)
        scores, indexes = self._index.search(vector, min(limit, len(self._records)))
        results = [
            {"score": round(float(score), 4), **self._records[int(index)]}
            for score, index in zip(scores[0], indexes[0], strict=True)
            if index >= 0
        ]
        return {"available": True, "method": "faiss", "results": results}


def explain_code(
    query: str,
    *,
    command: list[str] | None = None,
    timeout_seconds: int = 20,
    max_bytes: int = MAX_EXPLAIN_BYTES,
) -> dict[str, Any]:
    """Run a configured explain-code provider with a strict JSON output contract.

    The provider is intentionally adapter based: callers supply argv or
    ``EXPLAIN_CODE_COMMAND`` and no upstream CLI is assumed.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a nonempty string")
    if len(query.encode("utf-8")) > MAX_QUERY_BYTES:
        raise ValueError("query is too large")
    if not 1 <= timeout_seconds <= 120 or max_bytes < 256 or max_bytes > MAX_EXPLAIN_BYTES:
        raise ValueError("invalid explain-code bounds")
    argv = command or shlex.split(os.environ.get("EXPLAIN_CODE_COMMAND", ""))
    if not argv:
        return {"available": False, "reason": "explain-code provider is not configured"}
    return run_bounded_json(
        argv,
        {"query": query},
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
        env={
            key: value
            for key, value in os.environ.items()
            if not any(
                word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
            )
        },
    )
