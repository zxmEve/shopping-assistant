from __future__ import annotations

import argparse

from procure_pilot.bootstrap import build_policy_knowledge_service
from procure_pilot.config import Settings
from procure_pilot.persistence.knowledge_repository import SqlAlchemyKnowledgeRepository


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill published policy chunks with the configured embedding model"
    )
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--category-id")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-batches", type=int)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    settings = Settings()
    if settings.repository_backend != "postgres" or not settings.database_url:
        raise SystemExit(
            "Knowledge backfill requires PROCURE_REPOSITORY_BACKEND=postgres and "
            "PROCURE_DATABASE_URL"
        )
    repository = SqlAlchemyKnowledgeRepository(settings.database_url)
    service = build_policy_knowledge_service(repository, settings)
    result = service.backfill_embeddings(
        tenant_id=args.tenant_id,
        category_id=args.category_id,
        batch_size=args.batch_size,
        max_batches=args.max_batches,
        dry_run=args.dry_run,
    )
    print(result.model_dump_json(indent=2))
