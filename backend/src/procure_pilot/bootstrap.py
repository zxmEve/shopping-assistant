from __future__ import annotations

from procure_pilot.config import Settings
from procure_pilot.persistence.knowledge_repository import SqlAlchemyKnowledgeRepository
from procure_pilot.services.document_ingestion import PolicyFileParser, TesseractOcrProvider
from procure_pilot.services.embeddings import HashingEmbeddingProvider, OpenAIEmbeddingProvider
from procure_pilot.services.knowledge import PolicyKnowledgeService


def build_policy_knowledge_service(
    repository: SqlAlchemyKnowledgeRepository,
    settings: Settings,
) -> PolicyKnowledgeService:
    if settings.knowledge_embedding_provider == "hash":
        embedding_provider = HashingEmbeddingProvider()
    elif settings.knowledge_embedding_provider == "openai":
        if not settings.knowledge_embedding_api_key:
            raise RuntimeError(
                "PROCURE_KNOWLEDGE_EMBEDDING_API_KEY is required when "
                "PROCURE_KNOWLEDGE_EMBEDDING_PROVIDER=openai"
            )
        embedding_provider = OpenAIEmbeddingProvider(
            api_key=settings.knowledge_embedding_api_key,
            api_model=settings.knowledge_embedding_model,
            base_url=settings.knowledge_embedding_base_url,
            batch_size=settings.knowledge_embedding_batch_size,
            timeout_seconds=settings.knowledge_embedding_timeout_seconds,
        )
    else:
        raise RuntimeError(
            "Unsupported PROCURE_KNOWLEDGE_EMBEDDING_PROVIDER. Use hash or openai."
        )

    if settings.knowledge_ocr_provider == "none":
        ocr_provider = None
    elif settings.knowledge_ocr_provider == "tesseract":
        ocr_provider = TesseractOcrProvider(
            languages=settings.knowledge_ocr_languages,
            timeout_seconds=settings.knowledge_ocr_timeout_seconds,
        )
    else:
        raise RuntimeError("Unsupported PROCURE_KNOWLEDGE_OCR_PROVIDER. Use none or tesseract.")

    return PolicyKnowledgeService(
        repository,
        embedding_provider=embedding_provider,
        file_parser=PolicyFileParser(
            ocr_provider=ocr_provider,
            max_file_bytes=settings.knowledge_max_file_bytes,
            max_pdf_pages=settings.knowledge_max_pdf_pages,
            max_docx_uncompressed_bytes=settings.knowledge_max_docx_uncompressed_bytes,
        ),
    )
