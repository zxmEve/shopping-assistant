from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from procure_pilot.domain.memory import (
    LongTermMemory,
    MemoryCandidate,
    MemoryCandidateConflict,
    MemoryCandidateStatus,
    MemoryScope,
)
from procure_pilot.domain.models import ProcurementRun, RunState, utc_now
from procure_pilot.harness.runtime import PersistenceConflictError
from procure_pilot.persistence.memory_candidate_repository import (
    SqlAlchemyMemoryCandidateRepository,
)
from procure_pilot.persistence.memory_repository import SqlAlchemyMemoryRepository
from procure_pilot.services.memory import MemoryService
from procure_pilot.services.memory_registry import MemoryRegistry

SAFE_CANDIDATE_STATES = frozenset({RunState.DRAFT_CREATED})
PERSISTENT_MARKERS = (
    "以后",
    "今后",
    "后续",
    "默认",
    "一直",
    "长期",
    "记住",
    "每次",
    "通常",
    "经常",
    "always",
    "remember",
    "by default",
)
TRANSIENT_MARKERS = ("这次", "本次", "当前这单", "仅这一次", "only this time")
FORBIDDEN_SOURCE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b", re.IGNORECASE),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
)
FORBIDDEN_SOURCE_TERMS = (
    "密码",
    "口令",
    "api key",
    "apikey",
    "token",
    "access token",
    "secret",
    "银行卡",
    "信用卡",
    "身份证",
)
BRAND_ALIASES = {
    "lenovo": "Lenovo",
    "联想": "Lenovo",
    "thinkpad": "ThinkPad",
    "dell": "Dell",
    "戴尔": "Dell",
    "hp": "HP",
    "惠普": "HP",
    "apple": "Apple",
    "苹果": "Apple",
    "huawei": "Huawei",
    "华为": "Huawei",
    "asus": "ASUS",
    "华硕": "ASUS",
}
PROVIDER_ALIASES = {
    "internal_catalog": "internal_catalog",
    "内部协议目录": "internal_catalog",
    "内部目录": "internal_catalog",
    "marketplace_alpha": "marketplace_alpha",
    "商城 alpha": "marketplace_alpha",
    "alpha": "marketplace_alpha",
    "marketplace_beta": "marketplace_beta",
    "商城 beta": "marketplace_beta",
    "beta": "marketplace_beta",
}


@dataclass(frozen=True)
class ExtractedProposal:
    key: str
    content: str
    value_json: dict[str, Any]
    tags: list[str]
    source_message_ref: str
    confidence: float = 0.95


class ConservativeMemoryExtractor:
    """Deterministic v1 extractor that prefers false negatives over unsafe persistence."""

    def extract(
        self,
        run: ProcurementRun,
        *,
        category_id: str,
    ) -> list[ExtractedProposal]:
        latest_by_key: dict[tuple[str, str], ExtractedProposal] = {}
        for position, message in enumerate(run.messages):
            if message.role != "user":
                continue
            source_ref = f"run://{run.run_id}/messages/{position}"
            for segment in re.split(r"[。！？；;!?\n]+", message.content):
                segment = segment.strip()
                normalized = segment.casefold()
                if not segment or not any(marker in normalized for marker in PERSISTENT_MARKERS):
                    continue
                if any(marker in normalized for marker in TRANSIENT_MARKERS):
                    continue
                if self._contains_forbidden_data(segment):
                    continue
                for proposal in self._extract_segment(
                    segment,
                    source_ref=source_ref,
                    run=run,
                ):
                    latest_by_key[(proposal.key, category_id)] = proposal
        return list(latest_by_key.values())

    def _extract_segment(
        self,
        segment: str,
        *,
        source_ref: str,
        run: ProcurementRun,
    ) -> list[ExtractedProposal]:
        proposals: list[ExtractedProposal] = []
        development_tags = ["development"] if self._is_development_context(segment) else []

        ram = self._memory_size(segment, noun=r"(?:内存|RAM)")
        if ram is not None:
            proposals.append(
                ExtractedProposal(
                    key="procurement.category.minimum_ram_gb",
                    content=f"以后采购笔记本时，最低内存为 {ram}GB。",
                    value_json={"value": ram, "unit": "GB"},
                    tags=development_tags,
                    source_message_ref=source_ref,
                )
            )

        storage = self._storage_size(segment)
        if storage is not None:
            proposals.append(
                ExtractedProposal(
                    key="procurement.category.minimum_storage_gb",
                    content=f"以后采购笔记本时，最低存储为 {storage}GB。",
                    value_json={"value": storage, "unit": "GB"},
                    tags=development_tags,
                    source_message_ref=source_ref,
                )
            )

        brand = self._preferred_brand(segment)
        if brand is not None:
            proposals.append(
                ExtractedProposal(
                    key="procurement.category.preferred_brand",
                    content=f"以后采购笔记本时，优先考虑 {brand}。",
                    value_json={"brands": [brand]},
                    tags=development_tags,
                    source_message_ref=source_ref,
                )
            )

        warranty_months = self._warranty_months(segment)
        if warranty_months is not None:
            proposals.append(
                ExtractedProposal(
                    key="procurement.category.preferred_warranty_months",
                    content=f"以后采购笔记本时，优先选择至少 {warranty_months} 个月保修。",
                    value_json={"minimum_months": warranty_months},
                    tags=["warranty-priority"],
                    source_message_ref=source_ref,
                )
            )

        provider = self._preferred_provider(segment)
        if provider is not None:
            proposals.append(
                ExtractedProposal(
                    key="procurement.category.preferred_supplier",
                    content=f"以后采购笔记本时，优先查询 {provider}。",
                    value_json={"providers": [provider]},
                    tags=development_tags,
                    source_message_ref=source_ref,
                )
            )

        city = run.requirement.delivery_city
        delivery_signal = any(term in segment for term in ("送到", "交付到", "配送到", "交货到"))
        if city and delivery_signal and city.casefold() in segment.casefold():
            value: dict[str, Any] = {"city": city}
            days = self._delivery_days(segment)
            if days is not None:
                value["maximum_days"] = days
            proposals.append(
                ExtractedProposal(
                    key="procurement.default.delivery_preference",
                    content=(
                        f"以后采购笔记本时，默认交付到 {city}"
                        + (f"，期望 {days} 天内到货。" if days else "。")
                    ),
                    value_json=value,
                    tags=["delivery-priority"],
                    source_message_ref=source_ref,
                )
            )
        return proposals

    @staticmethod
    def _memory_size(text: str, *, noun: str) -> int | None:
        patterns = (
            rf"(?:最低|至少|不少于|默认)?\s*(\d{{1,3}})\s*(?:GB|G)\s*(?:的)?{noun}",
            rf"{noun}\s*(?:最低|至少|不少于|默认|要)?\s*(\d{{1,3}})\s*(?:GB|G)?",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def _storage_size(text: str) -> int | None:
        match = re.search(
            r"(?:最低|至少|不少于|默认)?\s*(\d+(?:\.\d+)?)\s*(TB|T|GB|G)\s*(?:的)?(?:硬盘|存储|SSD)",
            text,
            re.IGNORECASE,
        ) or re.search(
            r"(?:硬盘|存储|SSD)\s*(?:最低|至少|不少于|默认|要)?\s*(\d+(?:\.\d+)?)\s*(TB|T|GB|G)",
            text,
            re.IGNORECASE,
        )
        if match is None:
            return None
        value = float(match.group(1))
        return int(value * 1024) if match.group(2).casefold() in {"tb", "t"} else int(value)

    @staticmethod
    def _preferred_brand(text: str) -> str | None:
        normalized = text.casefold()
        if not any(
            term in normalized for term in ("优先", "偏好", "首选", "喜欢", "默认买", "prefer")
        ):
            return None
        if any(term in normalized for term in ("不买", "不要", "排除", "不考虑")):
            return None
        for alias, canonical in BRAND_ALIASES.items():
            if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", normalized):
                return canonical
        return None

    @staticmethod
    def _warranty_months(text: str) -> int | None:
        match = re.search(
            r"(\d{1,3})\s*(年|个月|月)\s*(?:的)?保修",
            text,
        ) or re.search(
            r"保修\s*(?:至少|最低|默认)?\s*(\d{1,3})\s*(年|个月|月)",
            text,
        )
        if match is None:
            return None
        value = int(match.group(1))
        return value * 12 if match.group(2) == "年" else value

    @staticmethod
    def _preferred_provider(text: str) -> str | None:
        normalized = text.casefold()
        if not any(term in normalized for term in ("优先", "首选", "默认", "prefer")):
            return None
        for alias, provider in PROVIDER_ALIASES.items():
            if alias in normalized:
                return provider
        return None

    @staticmethod
    def _delivery_days(text: str) -> int | None:
        match = re.search(
            r"(\d{1,3}|[一二两三四五六七八九十])\s*(天|周)\s*(?:内)?(?:到货|交付)?",
            text,
        )
        if match is None:
            return None
        chinese_numbers = {
            "一": 1,
            "二": 2,
            "两": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
        }
        value = chinese_numbers.get(
            match.group(1), int(match.group(1)) if match.group(1).isdigit() else 0
        )
        return value * 7 if match.group(2) == "周" else value

    @staticmethod
    def _is_development_context(text: str) -> bool:
        normalized = text.casefold()
        return any(term in normalized for term in ("开发", "研发", "docker"))

    @staticmethod
    def _contains_forbidden_data(text: str) -> bool:
        normalized = text.casefold()
        if any(term in normalized for term in FORBIDDEN_SOURCE_TERMS):
            return True
        if any(pattern.search(text) for pattern in FORBIDDEN_SOURCE_PATTERNS):
            return True
        for digits in re.findall(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)", text):
            number = re.sub(r"\D", "", digits)
            if 13 <= len(number) <= 19 and ConservativeMemoryExtractor._passes_luhn(number):
                return True
        return False

    @staticmethod
    def _passes_luhn(number: str) -> bool:
        total = 0
        for index, char in enumerate(reversed(number)):
            digit = int(char)
            if index % 2 == 1:
                digit *= 2
                if digit > 9:
                    digit -= 9
            total += digit
        return total % 10 == 0


class MemoryCandidateService:
    candidate_ttl_days = 14

    def __init__(
        self,
        candidate_repository: SqlAlchemyMemoryCandidateRepository,
        memory_repository: SqlAlchemyMemoryRepository,
        memory_service: MemoryService,
        registry: MemoryRegistry,
        *,
        extractor: ConservativeMemoryExtractor | None = None,
        clock=utc_now,
    ) -> None:
        self.candidate_repository = candidate_repository
        self.memory_repository = memory_repository
        self.memory_service = memory_service
        self.registry = registry
        self.extractor = extractor or ConservativeMemoryExtractor()
        self.clock = clock

    def propose_from_run(self, run: ProcurementRun, *, team_id: str) -> list[MemoryCandidate]:
        if run.state not in SAFE_CANDIDATE_STATES:
            raise ValueError("Memory candidates require a safely completed run")
        category = self.registry.resolve_category(run.requirement.category)
        existing = self.memory_repository.list(
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            team_id=team_id,
        )
        result: list[MemoryCandidate] = []
        for proposal in self.extractor.extract(run, category_id=category.category_id):
            definition, canonical_tags = self.registry.validate_memory(
                key=proposal.key,
                definition_version=None,
                category_id=category.category_id,
                scope=MemoryScope.PROFILE,
                value_json=proposal.value_json,
                tags=proposal.tags,
            )
            current = next(
                (
                    memory
                    for memory in existing
                    if memory.owner_user_id == run.user_id
                    and memory.scope == MemoryScope.PROFILE
                    and memory.key == definition.key
                    and memory.category_id == category.category_id
                ),
                None,
            )
            if current is not None and current.value_json == proposal.value_json:
                continue
            source_refs = [proposal.source_message_ref]
            fingerprint = self._fingerprint(
                {
                    "key": definition.key,
                    "key_definition_version": definition.definition_version,
                    "category_id": category.category_id,
                    "value_json": proposal.value_json,
                    "source_message_refs": source_refs,
                }
            )
            now = self.clock()
            candidate = MemoryCandidate(
                tenant_id=run.tenant_id,
                owner_user_id=run.user_id,
                origin_run_id=run.run_id,
                key=definition.key,
                key_definition_version=definition.definition_version,
                category_id=category.category_id,
                proposed_content=proposal.content,
                proposed_value_json=proposal.value_json,
                tags=canonical_tags,
                source_message_refs=source_refs,
                source_ref=proposal.source_message_ref,
                confidence=proposal.confidence,
                proposal_fingerprint=fingerprint,
                dedup_target_memory_id=current.memory_id if current else None,
                conflict_type=(
                    MemoryCandidateConflict.REPLACES_EXISTING
                    if current
                    else MemoryCandidateConflict.NEW
                ),
                expires_at=now + timedelta(days=self.candidate_ttl_days),
                created_at=now,
            )
            result.append(self.candidate_repository.create_pending(candidate))
        return result

    def list(
        self,
        *,
        tenant_id: str,
        user_id: str,
        status: MemoryCandidateStatus | None = None,
        origin_run_id: str | None = None,
    ) -> list[MemoryCandidate]:
        return self.candidate_repository.list(
            tenant_id=tenant_id,
            owner_user_id=user_id,
            status=status,
            origin_run_id=origin_run_id,
        )

    def accept(
        self,
        candidate_id: str,
        *,
        tenant_id: str,
        user_id: str,
        team_id: str,
        roles: tuple[str, ...],
        request_id: str,
        displayed_text: str,
    ) -> LongTermMemory:
        candidate = self.candidate_repository.get(
            candidate_id,
            tenant_id=tenant_id,
            owner_user_id=user_id,
        )
        if candidate.status == MemoryCandidateStatus.ACCEPTED:
            if candidate.resolution_request_id != request_id or not candidate.accepted_memory_id:
                raise ValueError("Memory candidate was accepted by another request")
            return self.memory_repository.get(
                candidate.accepted_memory_id,
                tenant_id=tenant_id,
                user_id=user_id,
                team_id=team_id,
            )
        if candidate.status != MemoryCandidateStatus.PENDING:
            raise ValueError(f"Memory candidate is already {candidate.status.value.lower()}")
        if displayed_text.strip() != candidate.proposed_content:
            raise ValueError("Consent text must exactly match the displayed memory candidate")
        current = next(
            (
                memory
                for memory in self.memory_repository.list(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    team_id=team_id,
                )
                if memory.owner_user_id == user_id
                and memory.scope == candidate.scope
                and memory.key == candidate.key
                and memory.category_id == candidate.category_id
            ),
            None,
        )
        current_id = current.memory_id if current else None
        if current_id != candidate.dedup_target_memory_id:
            raise PersistenceConflictError(
                "Memory changed after this candidate was generated; regenerate the candidate"
            )
        memory = self.memory_service.create(
            tenant_id=tenant_id,
            user_id=user_id,
            team_id=team_id,
            roles=roles,
            scope=candidate.scope,
            key=candidate.key,
            category_id=candidate.category_id,
            content=candidate.proposed_content,
            value_json=candidate.proposed_value_json,
            tags=candidate.tags,
            effective_at=None,
            expires_at=None,
            request_id=request_id,
            displayed_text=displayed_text,
            source_ref=candidate.source_ref,
            confidence=candidate.confidence,
            metadata={
                "origin_run_id": candidate.origin_run_id,
                "memory_candidate_id": candidate.candidate_id,
            },
        )
        self.candidate_repository.resolve(
            candidate_id,
            tenant_id=tenant_id,
            owner_user_id=user_id,
            status=MemoryCandidateStatus.ACCEPTED,
            request_id=request_id,
            accepted_memory_id=memory.memory_id,
        )
        return memory

    def reject(
        self,
        candidate_id: str,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str,
    ) -> MemoryCandidate:
        return self.candidate_repository.resolve(
            candidate_id,
            tenant_id=tenant_id,
            owner_user_id=user_id,
            status=MemoryCandidateStatus.REJECTED,
            request_id=request_id,
        )

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
