from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from procure_pilot.domain.knowledge import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentStatus,
    KnowledgeDocumentVersion,
    KnowledgeVersionStatus,
    cosine_similarity,
)
from procure_pilot.persistence.models import (
    KnowledgeChunkRecord,
    KnowledgeDocumentRecord,
    KnowledgeDocumentVersionRecord,
)


@dataclass(frozen=True)
class RankedKnowledgeChunk:
    chunk: KnowledgeChunk
    version: KnowledgeDocumentVersion
    document: KnowledgeDocument
    score: float


@dataclass(frozen=True)
class KnowledgeIndexSource:
    chunk: KnowledgeChunk
    document: KnowledgeDocument


class SqlAlchemyKnowledgeRepository:
    """Tenant-scoped storage for governed, versioned policy knowledge."""

    def __init__(self, database_url: str, *, engine: Engine | None = None) -> None:
        from sqlalchemy import create_engine

        self.engine = engine or create_engine(database_url, pool_pre_ping=True)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)

    def create_draft(
        self,
        document: KnowledgeDocument,
        version: KnowledgeDocumentVersion,
        chunks: list[KnowledgeChunk],
        *,
        source_blob: bytes | None = None,
    ) -> tuple[KnowledgeDocument, KnowledgeDocumentVersion]:
        with self.session_factory.begin() as session:
            record = session.execute(
                select(KnowledgeDocumentRecord).where(
                    KnowledgeDocumentRecord.tenant_id == document.tenant_id,
                    KnowledgeDocumentRecord.document_key == document.document_key,
                )
            ).scalar_one_or_none()
            if record is None:
                session.add(self._document_record(document))
                session.flush()
            elif (
                record.document_id != document.document_id
                or record.category_id != document.category_id
            ):
                raise ValueError("Knowledge document key already exists with different metadata")

            duplicate = session.execute(
                select(KnowledgeDocumentVersionRecord).where(
                    KnowledgeDocumentVersionRecord.tenant_id == version.tenant_id,
                    KnowledgeDocumentVersionRecord.document_id == version.document_id,
                    KnowledgeDocumentVersionRecord.content_hash == version.content_hash,
                    KnowledgeDocumentVersionRecord.policy_version == version.policy_version,
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                return self._document_domain(
                    record or self._document_record(document)
                ), self._version_domain(duplicate)

            session.add(self._version_record(version, source_blob=source_blob))
            # Records intentionally have no ORM relationships. Flush the parents
            # explicitly so PostgreSQL foreign keys do not depend on mapper ordering.
            session.flush()
            session.add_all(self._chunk_record(chunk) for chunk in chunks)
        return document, version

    def get_document(self, document_id: str, *, tenant_id: str) -> KnowledgeDocument:
        with self.session_factory() as session:
            record = session.execute(
                select(KnowledgeDocumentRecord).where(
                    KnowledgeDocumentRecord.document_id == document_id,
                    KnowledgeDocumentRecord.tenant_id == tenant_id,
                )
            ).scalar_one_or_none()
        if record is None:
            raise PermissionError("Knowledge document is not accessible in this tenant")
        return self._document_domain(record)

    def find_document_by_key(
        self, document_key: str, *, tenant_id: str
    ) -> KnowledgeDocument | None:
        with self.session_factory() as session:
            record = session.execute(
                select(KnowledgeDocumentRecord).where(
                    KnowledgeDocumentRecord.tenant_id == tenant_id,
                    KnowledgeDocumentRecord.document_key == document_key,
                )
            ).scalar_one_or_none()
        return self._document_domain(record) if record is not None else None

    def next_version(self, document_id: str, *, tenant_id: str) -> int:
        with self.session_factory() as session:
            current = session.execute(
                select(func.max(KnowledgeDocumentVersionRecord.version)).where(
                    KnowledgeDocumentVersionRecord.tenant_id == tenant_id,
                    KnowledgeDocumentVersionRecord.document_id == document_id,
                )
            ).scalar_one()
        return int(current or 0) + 1

    def publish(
        self,
        document_id: str,
        *,
        tenant_id: str,
        version: int | None,
        published_at: datetime,
    ) -> KnowledgeDocumentVersion:
        with self.session_factory.begin() as session:
            document = session.execute(
                select(KnowledgeDocumentRecord).where(
                    KnowledgeDocumentRecord.document_id == document_id,
                    KnowledgeDocumentRecord.tenant_id == tenant_id,
                )
            ).scalar_one_or_none()
            if document is None:
                raise PermissionError("Knowledge document is not accessible in this tenant")
            statement = select(KnowledgeDocumentVersionRecord).where(
                KnowledgeDocumentVersionRecord.document_id == document_id,
                KnowledgeDocumentVersionRecord.tenant_id == tenant_id,
            )
            if version is not None:
                statement = statement.where(KnowledgeDocumentVersionRecord.version == version)
            else:
                statement = statement.order_by(KnowledgeDocumentVersionRecord.version.desc())
            target = session.execute(statement).scalars().first()
            if target is None:
                raise ValueError("Knowledge document version does not exist")
            if target.status == KnowledgeVersionStatus.PUBLISHED.value:
                return self._version_domain(target)

            later = session.execute(
                select(KnowledgeDocumentVersionRecord).where(
                    KnowledgeDocumentVersionRecord.tenant_id == tenant_id,
                    KnowledgeDocumentVersionRecord.document_id == document_id,
                    KnowledgeDocumentVersionRecord.status == KnowledgeVersionStatus.PUBLISHED.value,
                    KnowledgeDocumentVersionRecord.effective_from >= target.effective_from,
                ).limit(1)
            ).scalar_one_or_none()
            if later is not None:
                raise ValueError(
                    "A published version already starts at or after this effective date"
                )

            session.execute(
                update(KnowledgeDocumentVersionRecord)
                .where(
                    KnowledgeDocumentVersionRecord.tenant_id == tenant_id,
                    KnowledgeDocumentVersionRecord.document_id == document_id,
                    KnowledgeDocumentVersionRecord.status == KnowledgeVersionStatus.PUBLISHED.value,
                    KnowledgeDocumentVersionRecord.effective_from < target.effective_from,
                    or_(
                        KnowledgeDocumentVersionRecord.effective_to.is_(None),
                        KnowledgeDocumentVersionRecord.effective_to > target.effective_from,
                    ),
                )
                .values(effective_to=target.effective_from)
            )
            target.status = KnowledgeVersionStatus.PUBLISHED.value
            target.published_at = published_at
            document.status = KnowledgeDocumentStatus.PUBLISHED.value
            document.updated_at = published_at
            session.flush()
            result = self._version_domain(target)
        return result

    def active_chunks(
        self,
        *,
        tenant_id: str,
        category_id: str,
        as_of: datetime,
        query_tokens: list[str] | None = None,
        limit: int = 50,
    ) -> list[tuple[KnowledgeChunk, KnowledgeDocumentVersion, KnowledgeDocument]]:
        statement = (
            select(
                KnowledgeChunkRecord,
                KnowledgeDocumentVersionRecord,
                KnowledgeDocumentRecord,
            )
            .join(
                KnowledgeDocumentVersionRecord,
                KnowledgeDocumentVersionRecord.version_id == KnowledgeChunkRecord.version_id,
            )
            .join(
                KnowledgeDocumentRecord,
                KnowledgeDocumentRecord.document_id == KnowledgeChunkRecord.document_id,
            )
            .where(
                KnowledgeChunkRecord.tenant_id == tenant_id,
                KnowledgeDocumentRecord.tenant_id == tenant_id,
                KnowledgeDocumentVersionRecord.tenant_id == tenant_id,
                KnowledgeDocumentRecord.category_id == category_id,
                KnowledgeDocumentRecord.status == KnowledgeDocumentStatus.PUBLISHED.value,
                KnowledgeDocumentVersionRecord.status == KnowledgeVersionStatus.PUBLISHED.value,
                KnowledgeDocumentVersionRecord.effective_from <= as_of,
                or_(
                    KnowledgeDocumentVersionRecord.effective_to.is_(None),
                    KnowledgeDocumentVersionRecord.effective_to > as_of,
                ),
            )
        )
        tokens = list(dict.fromkeys(query_tokens or []))
        if tokens:
            if self.engine.dialect.name == "postgresql":
                web_query = " OR ".join(f'"{token}"' for token in tokens)
                vector = func.to_tsvector("simple", KnowledgeChunkRecord.search_text)
                parsed_query = func.websearch_to_tsquery("simple", web_query)
                statement = statement.where(vector.op("@@")(parsed_query)).order_by(
                    func.ts_rank_cd(vector, parsed_query).desc()
                )
            else:
                statement = statement.where(
                    or_(*(KnowledgeChunkRecord.search_text.contains(token) for token in tokens))
                )
        statement = statement.order_by(
            KnowledgeDocumentRecord.document_key,
            KnowledgeDocumentVersionRecord.version.desc(),
            KnowledgeChunkRecord.ordinal,
        ).limit(limit)
        with self.session_factory() as session:
            rows = session.execute(statement).all()
        return [
            (
                self._chunk_domain(chunk),
                self._version_domain(version),
                self._document_domain(document),
            )
            for chunk, version, document in rows
        ]

    def search_fts(
        self,
        *,
        tenant_id: str,
        category_id: str,
        as_of: datetime,
        query_tokens: list[str],
        limit: int,
    ) -> list[RankedKnowledgeChunk]:
        tokens = list(dict.fromkeys(query_tokens))
        if not tokens:
            return []
        if self.engine.dialect.name != "postgresql":
            rows = self.active_chunks(
                tenant_id=tenant_id,
                category_id=category_id,
                as_of=as_of,
                query_tokens=tokens,
                limit=max(limit * 4, 50),
            )
            ranked = [
                RankedKnowledgeChunk(
                    chunk=chunk,
                    version=version,
                    document=document,
                    score=len(set(tokens).intersection(chunk.search_text.split())) / len(tokens),
                )
                for chunk, version, document in rows
            ]
            return sorted(
                ranked,
                key=lambda item: (-item.score, item.document.document_key, item.chunk.ordinal),
            )[:limit]

        web_query = " OR ".join(f'"{token}"' for token in tokens)
        search_vector = func.to_tsvector("simple", KnowledgeChunkRecord.search_text)
        parsed_query = func.websearch_to_tsquery("simple", web_query)
        score = func.ts_rank_cd(search_vector, parsed_query).label("fts_score")
        statement = (
            self._active_records_statement(
                tenant_id=tenant_id,
                category_id=category_id,
                as_of=as_of,
            )
            .add_columns(score)
            .where(search_vector.op("@@")(parsed_query))
            .order_by(score.desc(), KnowledgeChunkRecord.ordinal)
            .limit(limit)
        )
        with self.session_factory() as session:
            rows = session.execute(statement).all()
        return [
            RankedKnowledgeChunk(
                chunk=self._chunk_domain(chunk),
                version=self._version_domain(version),
                document=self._document_domain(document),
                score=float(raw_score),
            )
            for chunk, version, document, raw_score in rows
        ]

    def search_vector(
        self,
        *,
        tenant_id: str,
        category_id: str,
        as_of: datetime,
        query_embedding: list[float],
        embedding_model: str,
        limit: int,
    ) -> list[RankedKnowledgeChunk]:
        if self.engine.dialect.name != "postgresql":
            rows = self.active_chunks(
                tenant_id=tenant_id,
                category_id=category_id,
                as_of=as_of,
                limit=10_000,
            )
            ranked = [
                RankedKnowledgeChunk(
                    chunk=chunk,
                    version=version,
                    document=document,
                    score=cosine_similarity(query_embedding, chunk.embedding),
                )
                for chunk, version, document in rows
                if chunk.embedding is not None and chunk.embedding_model == embedding_model
            ]
            return sorted(
                ranked,
                key=lambda item: (-item.score, item.document.document_key, item.chunk.ordinal),
            )[:limit]

        distance = KnowledgeChunkRecord.embedding.cosine_distance(query_embedding).label(
            "vector_distance"
        )
        statement = (
            self._active_records_statement(
                tenant_id=tenant_id,
                category_id=category_id,
                as_of=as_of,
            )
            .add_columns(distance)
            .where(
                KnowledgeChunkRecord.embedding.is_not(None),
                KnowledgeChunkRecord.embedding_model == embedding_model,
            )
            .order_by(distance, KnowledgeChunkRecord.ordinal)
            .limit(limit)
        )
        with self.session_factory() as session:
            rows = session.execute(statement).all()
        return [
            RankedKnowledgeChunk(
                chunk=self._chunk_domain(chunk),
                version=self._version_domain(version),
                document=self._document_domain(document),
                score=max(-1.0, min(1.0, 1.0 - float(raw_distance))),
            )
            for chunk, version, document, raw_distance in rows
        ]

    def has_published_documents(self, *, tenant_id: str, category_id: str) -> bool:
        with self.session_factory() as session:
            found = session.execute(
                select(KnowledgeDocumentVersionRecord.version_id)
                .join(
                    KnowledgeDocumentRecord,
                    KnowledgeDocumentRecord.document_id
                    == KnowledgeDocumentVersionRecord.document_id,
                )
                .where(
                    KnowledgeDocumentVersionRecord.tenant_id == tenant_id,
                    KnowledgeDocumentRecord.tenant_id == tenant_id,
                    KnowledgeDocumentRecord.category_id == category_id,
                    KnowledgeDocumentVersionRecord.status == KnowledgeVersionStatus.PUBLISHED.value,
                )
                .limit(1)
            ).scalar_one_or_none()
        return found is not None

    def active_policy_versions(
        self, *, tenant_id: str, category_id: str, as_of: datetime
    ) -> set[str]:
        with self.session_factory() as session:
            rows = session.execute(
                select(KnowledgeDocumentVersionRecord.policy_version)
                .join(
                    KnowledgeDocumentRecord,
                    KnowledgeDocumentRecord.document_id
                    == KnowledgeDocumentVersionRecord.document_id,
                )
                .where(
                    KnowledgeDocumentVersionRecord.tenant_id == tenant_id,
                    KnowledgeDocumentRecord.tenant_id == tenant_id,
                    KnowledgeDocumentRecord.category_id == category_id,
                    KnowledgeDocumentRecord.status == KnowledgeDocumentStatus.PUBLISHED.value,
                    KnowledgeDocumentVersionRecord.status
                    == KnowledgeVersionStatus.PUBLISHED.value,
                    KnowledgeDocumentVersionRecord.effective_from <= as_of,
                    or_(
                        KnowledgeDocumentVersionRecord.effective_to.is_(None),
                        KnowledgeDocumentVersionRecord.effective_to > as_of,
                    ),
                )
                .distinct()
            ).scalars()
        return set(rows)

    def replace_chunk_search_text(self, chunk_id: str, *, tenant_id: str, value: str) -> None:
        with self.session_factory.begin() as session:
            result = session.execute(
                update(KnowledgeChunkRecord)
                .where(
                    KnowledgeChunkRecord.chunk_id == chunk_id,
                    KnowledgeChunkRecord.tenant_id == tenant_id,
                )
                .values(search_text=value)
            )
            if result.rowcount != 1:
                raise PermissionError("Knowledge chunk is not accessible in this tenant")

    def replace_chunk_index(
        self,
        chunk_id: str,
        *,
        tenant_id: str,
        search_text: str,
        embedding: list[float],
        embedding_model: str,
    ) -> None:
        with self.session_factory.begin() as session:
            result = session.execute(
                update(KnowledgeChunkRecord)
                .where(
                    KnowledgeChunkRecord.chunk_id == chunk_id,
                    KnowledgeChunkRecord.tenant_id == tenant_id,
                )
                .values(
                    search_text=search_text,
                    embedding=embedding,
                    embedding_model=embedding_model,
                )
            )
            if result.rowcount != 1:
                raise PermissionError("Knowledge chunk is not accessible in this tenant")

    def replace_chunk_indexes_batch(
        self,
        values: list[tuple[str, str, list[float], str]],
        *,
        tenant_id: str,
    ) -> None:
        with self.session_factory.begin() as session:
            for chunk_id, search_text, embedding, embedding_model in values:
                result = session.execute(
                    update(KnowledgeChunkRecord)
                    .where(
                        KnowledgeChunkRecord.chunk_id == chunk_id,
                        KnowledgeChunkRecord.tenant_id == tenant_id,
                    )
                    .values(
                        search_text=search_text,
                        embedding=embedding,
                        embedding_model=embedding_model,
                    )
                )
                if result.rowcount != 1:
                    raise PermissionError("Knowledge chunk is not accessible in this tenant")

    def chunks_for_embedding_backfill(
        self,
        *,
        tenant_id: str,
        embedding_model: str,
        category_id: str | None,
        limit: int,
    ) -> list[KnowledgeIndexSource]:
        statement = (
            select(KnowledgeChunkRecord, KnowledgeDocumentRecord)
            .join(
                KnowledgeDocumentVersionRecord,
                KnowledgeDocumentVersionRecord.version_id == KnowledgeChunkRecord.version_id,
            )
            .join(
                KnowledgeDocumentRecord,
                KnowledgeDocumentRecord.document_id == KnowledgeChunkRecord.document_id,
            )
            .where(
                KnowledgeChunkRecord.tenant_id == tenant_id,
                KnowledgeDocumentVersionRecord.tenant_id == tenant_id,
                KnowledgeDocumentRecord.tenant_id == tenant_id,
                KnowledgeDocumentRecord.status == KnowledgeDocumentStatus.PUBLISHED.value,
                KnowledgeDocumentVersionRecord.status == KnowledgeVersionStatus.PUBLISHED.value,
                or_(
                    KnowledgeChunkRecord.embedding.is_(None),
                    KnowledgeChunkRecord.embedding_model.is_(None),
                    KnowledgeChunkRecord.embedding_model != embedding_model,
                ),
            )
            .order_by(KnowledgeChunkRecord.chunk_id)
            .limit(limit)
        )
        if category_id is not None:
            statement = statement.where(KnowledgeDocumentRecord.category_id == category_id)
        with self.session_factory() as session:
            rows = session.execute(statement).all()
        return [
            KnowledgeIndexSource(
                chunk=self._chunk_domain(chunk),
                document=self._document_domain(document),
            )
            for chunk, document in rows
        ]

    def count_embedding_backfill(
        self,
        *,
        tenant_id: str,
        embedding_model: str,
        category_id: str | None,
    ) -> int:
        statement = (
            select(func.count(KnowledgeChunkRecord.chunk_id))
            .join(
                KnowledgeDocumentVersionRecord,
                KnowledgeDocumentVersionRecord.version_id == KnowledgeChunkRecord.version_id,
            )
            .join(
                KnowledgeDocumentRecord,
                KnowledgeDocumentRecord.document_id == KnowledgeChunkRecord.document_id,
            )
            .where(
                KnowledgeChunkRecord.tenant_id == tenant_id,
                KnowledgeDocumentVersionRecord.tenant_id == tenant_id,
                KnowledgeDocumentRecord.tenant_id == tenant_id,
                KnowledgeDocumentRecord.status == KnowledgeDocumentStatus.PUBLISHED.value,
                KnowledgeDocumentVersionRecord.status == KnowledgeVersionStatus.PUBLISHED.value,
                or_(
                    KnowledgeChunkRecord.embedding.is_(None),
                    KnowledgeChunkRecord.embedding_model.is_(None),
                    KnowledgeChunkRecord.embedding_model != embedding_model,
                ),
            )
        )
        if category_id is not None:
            statement = statement.where(KnowledgeDocumentRecord.category_id == category_id)
        with self.session_factory() as session:
            return int(session.execute(statement).scalar_one())

    def delete_draft_chunks(self, version_id: str, *, tenant_id: str) -> None:
        with self.session_factory.begin() as session:
            version = session.execute(
                select(KnowledgeDocumentVersionRecord).where(
                    KnowledgeDocumentVersionRecord.version_id == version_id,
                    KnowledgeDocumentVersionRecord.tenant_id == tenant_id,
                )
            ).scalar_one_or_none()
            if version is None or version.status != KnowledgeVersionStatus.DRAFT.value:
                raise ValueError("Only an accessible draft version can have chunks replaced")
            session.execute(
                delete(KnowledgeChunkRecord).where(
                    KnowledgeChunkRecord.version_id == version_id,
                    KnowledgeChunkRecord.tenant_id == tenant_id,
                )
            )

    @staticmethod
    def _document_record(value: KnowledgeDocument) -> KnowledgeDocumentRecord:
        return KnowledgeDocumentRecord(**value.model_dump(mode="python"))

    @staticmethod
    def _version_record(
        value: KnowledgeDocumentVersion, *, source_blob: bytes | None = None
    ) -> KnowledgeDocumentVersionRecord:
        return KnowledgeDocumentVersionRecord(
            **value.model_dump(mode="python"),
            source_blob=source_blob,
        )

    @staticmethod
    def _chunk_record(value: KnowledgeChunk) -> KnowledgeChunkRecord:
        return KnowledgeChunkRecord(**value.model_dump(mode="python"))

    @staticmethod
    def _document_domain(record: KnowledgeDocumentRecord) -> KnowledgeDocument:
        return KnowledgeDocument(
            document_id=record.document_id,
            tenant_id=record.tenant_id,
            document_key=record.document_key,
            title=record.title,
            category_id=record.category_id,
            source_uri=record.source_uri,
            status=record.status,
            created_by=record.created_by,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _version_domain(record: KnowledgeDocumentVersionRecord) -> KnowledgeDocumentVersion:
        return KnowledgeDocumentVersion(
            version_id=record.version_id,
            document_id=record.document_id,
            tenant_id=record.tenant_id,
            version=record.version,
            policy_version=record.policy_version,
            effective_from=record.effective_from,
            effective_to=record.effective_to,
            raw_content=record.raw_content,
            content_hash=record.content_hash,
            source_file_name=record.source_file_name,
            source_media_type=record.source_media_type,
            source_file_hash=record.source_file_hash,
            source_file_size=record.source_file_size,
            parser_version=record.parser_version,
            extraction_metadata=record.extraction_metadata or {},
            status=record.status,
            published_at=record.published_at,
            created_by=record.created_by,
            created_at=record.created_at,
        )

    @staticmethod
    def _chunk_domain(record: KnowledgeChunkRecord) -> KnowledgeChunk:
        return KnowledgeChunk(
            chunk_id=record.chunk_id,
            version_id=record.version_id,
            document_id=record.document_id,
            tenant_id=record.tenant_id,
            ordinal=record.ordinal,
            heading=record.heading,
            clause_ref=record.clause_ref,
            content=record.content,
            content_hash=record.content_hash,
            search_text=record.search_text,
            embedding=list(record.embedding) if record.embedding is not None else None,
            embedding_model=record.embedding_model,
            created_at=record.created_at,
        )

    @staticmethod
    def _active_records_statement(*, tenant_id: str, category_id: str, as_of: datetime):
        return (
            select(
                KnowledgeChunkRecord,
                KnowledgeDocumentVersionRecord,
                KnowledgeDocumentRecord,
            )
            .join(
                KnowledgeDocumentVersionRecord,
                KnowledgeDocumentVersionRecord.version_id == KnowledgeChunkRecord.version_id,
            )
            .join(
                KnowledgeDocumentRecord,
                KnowledgeDocumentRecord.document_id == KnowledgeChunkRecord.document_id,
            )
            .where(
                KnowledgeChunkRecord.tenant_id == tenant_id,
                KnowledgeDocumentRecord.tenant_id == tenant_id,
                KnowledgeDocumentVersionRecord.tenant_id == tenant_id,
                KnowledgeDocumentRecord.category_id == category_id,
                KnowledgeDocumentRecord.status == KnowledgeDocumentStatus.PUBLISHED.value,
                KnowledgeDocumentVersionRecord.status == KnowledgeVersionStatus.PUBLISHED.value,
                KnowledgeDocumentVersionRecord.effective_from <= as_of,
                or_(
                    KnowledgeDocumentVersionRecord.effective_to.is_(None),
                    KnowledgeDocumentVersionRecord.effective_to > as_of,
                ),
            )
        )
