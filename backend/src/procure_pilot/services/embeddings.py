from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from typing import Any, Protocol

from openai import OpenAI

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


class OpenAIEmbeddingProvider:
    """Hosted semantic embeddings with a stable, storage-safe model identity."""

    def __init__(
        self,
        *,
        api_key: str,
        api_model: str = "text-embedding-3-small",
        dimensions: int = KNOWLEDGE_EMBEDDING_DIMENSIONS,
        base_url: str | None = None,
        batch_size: int = 64,
        timeout_seconds: float = 30.0,
        client: Any | None = None,
    ) -> None:
        if not api_key and client is None:
            raise ValueError("An embedding API key is required for the OpenAI provider")
        if dimensions != KNOWLEDGE_EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"OpenAI embedding dimensions must be {KNOWLEDGE_EMBEDDING_DIMENSIONS}"
            )
        if batch_size < 1 or batch_size > 2048:
            raise ValueError("Embedding batch size must be between 1 and 2048")
        self.api_model = api_model
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.model = f"openai:{api_model}:d{dimensions}"
        self.client = client or OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=2,
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        values = list(texts)
        if not values:
            return []
        embeddings: list[list[float]] = []
        for start in range(0, len(values), self.batch_size):
            batch = values[start : start + self.batch_size]
            response = self.client.embeddings.create(
                input=batch,
                model=self.api_model,
                dimensions=self.dimensions,
                encoding_format="float",
            )
            ordered = sorted(response.data, key=lambda item: item.index)
            if [item.index for item in ordered] != list(range(len(batch))):
                raise ValueError("Embedding provider returned invalid item indexes")
            embeddings.extend([list(item.embedding) for item in ordered])
        if len(embeddings) != len(values) or any(
            len(value) != self.dimensions for value in embeddings
        ):
            raise ValueError("Embedding provider returned an invalid batch shape")
        return embeddings
