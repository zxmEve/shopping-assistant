from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel

from procure_pilot.domain.knowledge import (
    KNOWLEDGE_EMBEDDING_DIMENSIONS,
    KnowledgeChunk,
    KnowledgeCitation,
    KnowledgeDocument,
    KnowledgeDocumentStatus,
    KnowledgeDocumentVersion,
    KnowledgeSearchResult,
    KnowledgeVersionStatus,
    PolicyKnowledgeEvidence,
    PolicyKnowledgeReviewRequired,
)
from procure_pilot.domain.models import utc_now
from procure_pilot.persistence.knowledge_repository import SqlAlchemyKnowledgeRepository
from procure_pilot.services.embeddings import EmbeddingProvider, HashingEmbeddingProvider

KNOWLEDGE_WRITE_ROLES = frozenset({"ADMIN", "KNOWLEDGE_STEWARD", "PROCUREMENT_SPECIALIST"})
RRF_K = 60
VECTOR_RELEVANCE_THRESHOLD = 0.72
logger = logging.getLogger(__name__)
_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s+|第[一二三四五六七八九十百千0-9]+[编章节条]|"
    r"[0-9]+(?:\.[0-9]+)*[、.．]\s*|[（(][一二三四五六七八九十0-9]+[）)])"
)
_CLAUSE_RE = re.compile(
    r"^(第[一二三四五六七八九十百千0-9]+条|[0-9]+(?:\.[0-9]+)+|"
    r"[（(][一二三四五六七八九十0-9]+[）)])"
)
_PAGE_NUMBER_RE = re.compile(
    r"^(?:第\s*\d+\s*页(?:\s*/\s*共\s*\d+\s*页)?|page\s+\d+(?:\s+of\s+\d+)?)$",
    re.IGNORECASE,
)
_LATIN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]+", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3400-\u9fff]+")
_CJK_STOP = frozenset("的了和与及或为是在对将由按其本")


class KnowledgeIngestResult(BaseModel):
    document: KnowledgeDocument
    version: KnowledgeDocumentVersion
    chunk_count: int


@dataclass
class _HybridCandidate:
    chunk: KnowledgeChunk
    version: KnowledgeDocumentVersion
    document: KnowledgeDocument
    fts_rank: int | None = None
    fts_score: float = 0.0
    vector_rank: int | None = None
    vector_similarity: float | None = None

    @property
    def rrf_score(self) -> float:
        score = 0.0
        if self.fts_rank is not None:
            score += 1.0 / (RRF_K + self.fts_rank)
        if self.vector_rank is not None:
            score += 1.0 / (RRF_K + self.vector_rank)
        return score


def clean_policy_text(raw_content: str) -> str:
    """Normalize layout noise without rewriting policy facts or legal language."""
    value = unicodedata.normalize("NFKC", raw_content).replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\ufeff", "").replace("\u200b", "")
    lines: list[str] = []
    for raw_line in value.splitlines():
        line = re.sub(r"[\t \u3000]+", " ", raw_line).strip()
        if _PAGE_NUMBER_RE.fullmatch(line):
            continue
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def lexical_terms(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).lower()
    terms = [item for item in _LATIN_RE.findall(normalized) if len(item) > 1]
    for sequence in _CJK_RE.findall(normalized):
        terms.extend(sequence[index : index + 2] for index in range(max(0, len(sequence) - 1)))
        terms.extend(character for character in sequence if character not in _CJK_STOP)
    return list(dict.fromkeys(term for term in terms if term))


def build_search_text(*values: str) -> str:
    return " ".join(lexical_terms(" ".join(values)))


def split_policy_document(
    cleaned_content: str, *, max_chars: int = 1_200
) -> list[tuple[str, str | None, str]]:
    """Split on headings/clauses first, then sentence boundaries for oversized clauses."""
    if max_chars < 200:
        raise ValueError("Knowledge chunk size must be at least 200 characters")
    sections: list[tuple[str, str | None, list[str]]] = []
    heading = "正文"
    clause_ref: str | None = None
    body: list[str] = []

    def flush() -> None:
        nonlocal body
        content = "\n".join(body).strip()
        if content:
            sections.append((heading, clause_ref, [content]))
        body = []

    for line in cleaned_content.splitlines():
        if not line:
            if body and body[-1] != "":
                body.append("")
            continue
        if _HEADING_RE.match(line):
            flush()
            heading = line.lstrip("# ").strip()
            matched = _CLAUSE_RE.match(heading)
            clause_ref = matched.group(1) if matched else None
            body.append(line)
        else:
            body.append(line)
    flush()

    chunks: list[tuple[str, str | None, str]] = []
    for section_heading, section_clause, bodies in sections:
        content = bodies[0]
        if len(content) <= max_chars:
            chunks.append((section_heading, section_clause, content))
            continue
        sentences = [
            item.strip()
            for item in re.split(r"(?<=[。！？；.!?;])\s*|\n+", content)
            if item.strip()
        ]
        current = ""
        for sentence in sentences:
            pieces = [
                sentence[index : index + max_chars] for index in range(0, len(sentence), max_chars)
            ]
            for piece in pieces:
                candidate = f"{current}\n{piece}".strip() if current else piece
                if current and len(candidate) > max_chars:
                    chunks.append((section_heading, section_clause, current))
                    current = piece
                else:
                    current = candidate
        if current:
            chunks.append((section_heading, section_clause, current))
    return chunks


class PolicyKnowledgeService:
    def __init__(
        self,
        repository: SqlAlchemyKnowledgeRepository,
        *,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.repository = repository
        self.embedding_provider = embedding_provider or HashingEmbeddingProvider()
        if self.embedding_provider.dimensions != KNOWLEDGE_EMBEDDING_DIMENSIONS:
            raise ValueError(
                "Embedding provider dimensions must match the knowledge vector schema "
                f"({KNOWLEDGE_EMBEDDING_DIMENSIONS})"
            )

    def ingest(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        roles: tuple[str, ...],
        document_key: str,
        title: str,
        category_id: str,
        source_uri: str,
        policy_version: str,
        effective_from: datetime,
        raw_content: str,
    ) -> KnowledgeIngestResult:
        self._authorize_write(roles)
        if effective_from.tzinfo is None or effective_from.utcoffset() is None:
            raise ValueError("Knowledge effective_from must include a timezone")
        cleaned = clean_policy_text(raw_content)
        if len(cleaned) < 20:
            raise ValueError("Policy document content is too short after cleaning")
        existing = self.repository.find_document_by_key(document_key, tenant_id=tenant_id)
        now = utc_now()
        document = existing or KnowledgeDocument(
            document_id=f"kdoc_{uuid4().hex[:20]}",
            tenant_id=tenant_id,
            document_key=document_key,
            title=title,
            category_id=category_id,
            source_uri=source_uri,
            status=KnowledgeDocumentStatus.DRAFT,
            created_by=actor_user_id,
            created_at=now,
            updated_at=now,
        )
        if existing and (existing.category_id != category_id or existing.source_uri != source_uri):
            raise ValueError("A document key cannot change category or source URI")
        version_number = self.repository.next_version(document.document_id, tenant_id=tenant_id)
        version = KnowledgeDocumentVersion(
            version_id=f"kver_{uuid4().hex[:20]}",
            document_id=document.document_id,
            tenant_id=tenant_id,
            version=version_number,
            policy_version=policy_version,
            effective_from=effective_from,
            raw_content=raw_content,
            content_hash=hashlib.sha256(raw_content.encode("utf-8")).hexdigest(),
            status=KnowledgeVersionStatus.DRAFT,
            created_by=actor_user_id,
            created_at=now,
        )
        split = split_policy_document(cleaned)
        chunk_values = [
            (heading, clause_ref, content)
            for heading, clause_ref, content in split
        ]
        embedding_inputs = [f"{title}\n{heading}\n{content}" for heading, _, content in chunk_values]
        embeddings = self.embedding_provider.embed(embedding_inputs)
        if len(embeddings) != len(chunk_values) or any(
            len(value) != self.embedding_provider.dimensions for value in embeddings
        ):
            raise ValueError("Embedding provider returned an invalid batch shape")
        chunks = [
            KnowledgeChunk(
                chunk_id=f"kchunk_{uuid4().hex[:20]}",
                version_id=version.version_id,
                document_id=document.document_id,
                tenant_id=tenant_id,
                ordinal=ordinal,
                heading=heading,
                clause_ref=clause_ref,
                content=content,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                search_text=build_search_text(title, heading, content),
                embedding=embedding,
                embedding_model=self.embedding_provider.model,
                created_at=now,
            )
            for ordinal, ((heading, clause_ref, content), embedding) in enumerate(
                zip(chunk_values, embeddings, strict=True)
            )
        ]
        saved_document, saved_version = self.repository.create_draft(document, version, chunks)
        return KnowledgeIngestResult(
            document=saved_document,
            version=saved_version,
            chunk_count=len(chunks),
        )

    def publish(
        self,
        document_id: str,
        *,
        tenant_id: str,
        roles: tuple[str, ...],
        version: int | None = None,
    ) -> KnowledgeDocumentVersion:
        self._authorize_write(roles)
        return self.repository.publish(
            document_id,
            tenant_id=tenant_id,
            version=version,
            published_at=utc_now(),
        )

    def search(
        self,
        *,
        tenant_id: str,
        category_id: str,
        query: str,
        as_of: datetime | None = None,
        limit: int = 5,
    ) -> KnowledgeSearchResult:
        if not query.strip():
            raise ValueError("Knowledge search query cannot be empty")
        if limit < 1 or limit > 20:
            raise ValueError("Knowledge search limit must be between 1 and 20")
        resolved_at = as_of or utc_now()
        query_terms = lexical_terms(query)
        if not query_terms:
            return KnowledgeSearchResult(
                query=query,
                as_of=resolved_at,
                category_id=category_id,
            )
        candidate_limit = max(20, limit * 4)
        fts_hits = self.repository.search_fts(
            tenant_id=tenant_id,
            category_id=category_id,
            as_of=resolved_at,
            query_tokens=query_terms,
            limit=candidate_limit,
        )
        degraded_channels: list[str] = []
        try:
            embedded = self.embedding_provider.embed([query])
            if len(embedded) != 1 or len(embedded[0]) != self.embedding_provider.dimensions:
                raise ValueError("Embedding provider returned an invalid query shape")
            vector_hits = self.repository.search_vector(
                tenant_id=tenant_id,
                category_id=category_id,
                as_of=resolved_at,
                query_embedding=embedded[0],
                embedding_model=self.embedding_provider.model,
                limit=candidate_limit,
            )
        except Exception as exc:  # FTS remains available if vector retrieval is degraded.
            logger.warning("Vector knowledge retrieval degraded: %s", type(exc).__name__)
            vector_hits = []
            degraded_channels.append("vector")

        candidates: dict[str, _HybridCandidate] = {}
        for rank, hit in enumerate(fts_hits, start=1):
            candidates[hit.chunk.chunk_id] = _HybridCandidate(
                chunk=hit.chunk,
                version=hit.version,
                document=hit.document,
                fts_rank=rank,
                fts_score=hit.score,
            )
        for rank, hit in enumerate(vector_hits, start=1):
            candidate = candidates.setdefault(
                hit.chunk.chunk_id,
                _HybridCandidate(
                    chunk=hit.chunk,
                    version=hit.version,
                    document=hit.document,
                ),
            )
            candidate.vector_rank = rank
            candidate.vector_similarity = hit.score

        query_set = set(query_terms)
        scored: list[tuple[float, _HybridCandidate]] = []
        for candidate in candidates.values():
            chunk = candidate.chunk
            # The title is indexed for recall, but a citation must also match the
            # actual heading/body so a generic document title cannot ground every clause.
            chunk_terms = set(lexical_terms(f"{chunk.heading} {chunk.content}"))
            overlap = query_set.intersection(chunk_terms)
            strong_overlap = {term for term in overlap if len(term) >= 2}
            lexical_relevant = bool(strong_overlap) or len(overlap) >= 2
            semantic_relevant = (
                candidate.vector_similarity or 0.0
            ) >= VECTOR_RELEVANCE_THRESHOLD
            if not lexical_relevant and not semantic_relevant:
                continue
            coverage = len(overlap) / len(query_set)
            specificity = len(overlap) / max(1, len(chunk_terms))
            heading_bonus = (
                0.1 if strong_overlap.intersection(lexical_terms(chunk.heading)) else 0.0
            )
            lexical_score = min(1.0, coverage * 0.8 + specificity * 0.2 + heading_bonus)
            max_rrf = 2.0 / (RRF_K + 1)
            normalized_rrf = candidate.rrf_score / max_rrf
            semantic_score = max(0.0, candidate.vector_similarity or 0.0)
            rerank_score = round(
                normalized_rrf * 0.55 + lexical_score * 0.30 + semantic_score * 0.15,
                6,
            )
            scored.append((rerank_score, candidate))
        scored.sort(
            key=lambda item: (
                -item[0],
                -item[1].rrf_score,
                item[1].version.version,
                item[1].chunk.ordinal,
            )
        )
        citations = [
            KnowledgeCitation(
                chunk_id=candidate.chunk.chunk_id,
                document_id=candidate.document.document_id,
                document_key=candidate.document.document_key,
                document_title=candidate.document.title,
                document_version=candidate.version.version,
                policy_version=candidate.version.policy_version,
                heading=candidate.chunk.heading,
                clause_ref=candidate.chunk.clause_ref,
                excerpt=candidate.chunk.content[:320],
                source_uri=candidate.document.source_uri,
                source_ref=(
                    f"knowledge://{candidate.document.document_key}/v"
                    f"{candidate.version.version}/chunks/{candidate.chunk.chunk_id}"
                ),
                score=rerank_score,
                fts_rank=candidate.fts_rank,
                fts_score=(round(candidate.fts_score, 6) if candidate.fts_rank else None),
                vector_rank=candidate.vector_rank,
                rrf_score=round(candidate.rrf_score, 8),
                rerank_score=rerank_score,
                vector_similarity=(
                    round(candidate.vector_similarity, 6)
                    if candidate.vector_similarity is not None
                    else None
                ),
                retrieval_channels=[
                    channel
                    for channel, rank in (
                        ("fts", candidate.fts_rank),
                        ("vector", candidate.vector_rank),
                    )
                    if rank is not None
                ],
            )
            for rerank_score, candidate in scored[:limit]
        ]
        return KnowledgeSearchResult(
            query=query,
            as_of=resolved_at,
            category_id=category_id,
            retrieval_mode="hybrid_rrf" if vector_hits else "fts_only",
            embedding_model=self.embedding_provider.model,
            fts_candidate_count=len(fts_hits),
            vector_candidate_count=len(vector_hits),
            degraded_channels=degraded_channels,
            citations=citations,
        )

    def verify_policy_evidence(
        self,
        *,
        tenant_id: str,
        category_id: str,
        query: str,
        expected_policy_version: str,
        as_of: datetime | None = None,
    ) -> PolicyKnowledgeEvidence | None:
        """Return None before a tenant opts into governed knowledge; fail closed afterwards."""
        resolved_at = as_of or utc_now()
        if not self.repository.has_published_documents(
            tenant_id=tenant_id, category_id=category_id
        ):
            return None
        governed = self.repository.active_chunks(
            tenant_id=tenant_id,
            category_id=category_id,
            as_of=resolved_at,
            limit=1,
        )
        if not governed:
            raise PolicyKnowledgeReviewRequired(
                "KNOWLEDGE_NO_EFFECTIVE_VERSION",
                "No published policy version is effective at the decision time",
            )
        active_versions = self.repository.active_policy_versions(
            tenant_id=tenant_id,
            category_id=category_id,
            as_of=resolved_at,
        )
        if active_versions != {expected_policy_version}:
            raise PolicyKnowledgeReviewRequired(
                "POLICY_VERSION_CONFLICT",
                "Published policy knowledge and executable policy versions disagree",
            )
        result = self.search(
            tenant_id=tenant_id,
            category_id=category_id,
            query=query,
            as_of=resolved_at,
            limit=5,
        )
        if not result.citations:
            raise PolicyKnowledgeReviewRequired(
                "KNOWLEDGE_NO_ANSWER",
                "No effective policy clause supports the executable policy decision",
            )
        observed_versions = {citation.policy_version for citation in result.citations}
        if observed_versions != {expected_policy_version}:
            raise PolicyKnowledgeReviewRequired(
                "POLICY_VERSION_CONFLICT",
                "Published policy knowledge and executable policy versions disagree",
            )
        return PolicyKnowledgeEvidence(
            policy_version=expected_policy_version,
            citations=result.citations,
        )

    def reindex(
        self,
        *,
        tenant_id: str,
        roles: tuple[str, ...],
        category_id: str,
        as_of: datetime | None = None,
    ) -> int:
        self._authorize_write(roles)
        rows = self.repository.active_chunks(
            tenant_id=tenant_id,
            category_id=category_id,
            as_of=as_of or utc_now(),
            limit=10_000,
        )
        embedding_inputs = [
            f"{document.title}\n{chunk.heading}\n{chunk.content}"
            for chunk, _, document in rows
        ]
        embeddings = self.embedding_provider.embed(embedding_inputs)
        if len(embeddings) != len(rows) or any(
            len(value) != self.embedding_provider.dimensions for value in embeddings
        ):
            raise ValueError("Embedding provider returned an invalid reindex batch shape")
        for (chunk, _, document), embedding in zip(rows, embeddings, strict=True):
            self.repository.replace_chunk_index(
                chunk.chunk_id,
                tenant_id=tenant_id,
                search_text=build_search_text(document.title, chunk.heading, chunk.content),
                embedding=embedding,
                embedding_model=self.embedding_provider.model,
            )
        return len(rows)

    @staticmethod
    def _authorize_write(roles: tuple[str, ...]) -> None:
        if not KNOWLEDGE_WRITE_ROLES.intersection(roles):
            raise PermissionError("Knowledge ingestion requires a governed administrator role")
