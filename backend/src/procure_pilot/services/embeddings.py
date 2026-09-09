from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from typing import Protocol

from procure_pilot.domain.knowledge import KNOWLEDGE_EMBEDDING_DIMENSIONS

_LATIN_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]+", re.IGNORECASE)
_CJK_SEQUENCE_RE = re.compile(r"[\u3400-\u9fff]+")


class EmbeddingProvider(Protocol):
    """Small replaceable boundary for local or hosted embedding models."""

    model: str
    dimensions: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashingEmbeddingProvider:
    """Deterministic offline embedding for development and repeatable evaluations.

    It uses signed feature hashing over Latin tokens and CJK character n-grams.
    Production deployments can replace this provider without changing retrieval or storage.
    """

    model = "hash-ngram-v1"

    def __init__(self, dimensions: int = KNOWLEDGE_EMBEDDING_DIMENSIONS) -> None:
        if dimensions < 32:
            raise ValueError("Embedding dimensions must be at least 32")
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        features = self._features(text)
        if not features:
            return vector
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign
        norm = sum(value * value for value in vector) ** 0.5
        if norm:
            vector = [value / norm for value in vector]
        return vector

    @staticmethod
    def _features(text: str) -> list[str]:
        normalized = unicodedata.normalize("NFKC", text).lower()
        features = [f"word:{token}" for token in _LATIN_TOKEN_RE.findall(normalized)]
        for sequence in _CJK_SEQUENCE_RE.findall(normalized):
            features.extend(f"c1:{character}" for character in sequence)
            for size in (2, 3):
                features.extend(
                    f"c{size}:{sequence[index : index + size]}"
                    for index in range(max(0, len(sequence) - size + 1))
                )
        return features
