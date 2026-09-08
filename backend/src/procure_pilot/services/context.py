from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, Field, model_validator

from procure_pilot.agents.contracts import (
    AgentContextPayload,
    AgentKind,
    ContextMemoryItem,
    ContextMessage,
)
from procure_pilot.domain.memory import LongTermMemory, MemoryScope, MemoryStatus
from procure_pilot.domain.models import ProcurementRun, utc_now
from procure_pilot.services.memory_registry import MemoryRegistry


class MemoryReader(Protocol):
    def list(
        self,
        *,
        tenant_id: str,
        user_id: str,
        team_id: str,
        scope: MemoryScope | None = None,
    ) -> list[LongTermMemory]: ...


class RankingWeights(BaseModel):
    task_match: float = Field(ge=0)
    category_or_key_match: float = Field(ge=0)
    authority: float = Field(ge=0)
    recency: float = Field(ge=0)
    confidence: float = Field(ge=0)

    @model_validator(mode="after")
    def sum_to_one(self) -> RankingWeights:
        if not math.isclose(sum(self.model_dump().values()), 1.0, abs_tol=0.0001):
            raise ValueError("Context ranking weights must sum to 1")
        return self


class AgentContextRule(BaseModel):
    max_context_tokens: int = Field(gt=0)
    max_recent_messages: int = Field(ge=0)
    max_profile_memories: int = Field(ge=0)
    max_team_memories: int = Field(ge=0)
    max_episodic_memories: int = Field(ge=0)
    allowed_key_patterns: list[str] = Field(min_length=1)

    def memory_limit(self, scope: MemoryScope) -> int:
        return {
            MemoryScope.PROFILE: self.max_profile_memories,
            MemoryScope.TEAM: self.max_team_memories,
            MemoryScope.EPISODIC: self.max_episodic_memories,
            MemoryScope.SEMANTIC: 0,
        }[scope]


class ContextPolicy(BaseModel):
    version: str
    ranking: RankingWeights
    agents: dict[AgentKind, AgentContextRule]

    @model_validator(mode="after")
    def require_all_agents(self) -> ContextPolicy:
        if set(self.agents) != set(AgentKind):
            raise ValueError("Context policy must define exactly Requirement and Sourcing")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> ContextPolicy:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


class ContextExclusion(BaseModel):
    memory_id: str
    reason: str


class AssembledAgentContext(BaseModel):
    payload: AgentContextPayload
    selected_memory_ids: list[str] = Field(default_factory=list)
    exclusions: list[ContextExclusion] = Field(default_factory=list)
    dropped_message_count: int = Field(default=0, ge=0)


class ContextBudgetError(RuntimeError):
    pass


DEFAULT_CONTEXT_POLICY = ContextPolicy.model_validate(
    {
        "version": "context-policy-v1",
        "ranking": {
            "task_match": 0.40,
            "category_or_key_match": 0.25,
            "authority": 0.20,
            "recency": 0.10,
            "confidence": 0.05,
        },
        "agents": {
            "REQUIREMENT": {
                "max_context_tokens": 5000,
                "max_recent_messages": 6,
                "max_profile_memories": 2,
                "max_team_memories": 3,
                "max_episodic_memories": 0,
                "allowed_key_patterns": [
                    "procurement.{category}.minimum_",
                    "procurement.default.",
                    "team.device_standard.{category}",
                    "team.device_standard.general",
                ],
            },
            "SOURCING": {
                "max_context_tokens": 7000,
                "max_recent_messages": 3,
                "max_profile_memories": 4,
                "max_team_memories": 3,
                "max_episodic_memories": 2,
                "allowed_key_patterns": [
                    "procurement.{category}.preferred_",
                    "procurement.default.delivery_",
                    "team.device_standard.{category}",
                    "team.device_standard.general",
                ],
            },
        },
    }
)


class ContextAssembler:
    """Build a tenant-scoped, agent-specific and budgeted context projection."""

    def __init__(
        self,
        memory_reader: MemoryReader | None = None,
        *,
        policy: ContextPolicy = DEFAULT_CONTEXT_POLICY,
        memory_registry: MemoryRegistry | None = None,
        clock=utc_now,
    ) -> None:
        self.memory_reader = memory_reader
        self.policy = policy
        self.memory_registry = memory_registry
        self.clock = clock

    def assemble(
        self,
        *,
        agent: AgentKind,
        run: ProcurementRun,
        team_id: str,
    ) -> AssembledAgentContext:
        rule = self.policy.agents[agent]
        message_window = (
            run.messages[-rule.max_recent_messages :] if rule.max_recent_messages else []
        )
        recent_messages = [
            ContextMessage(role=item.role, content=item.content)
            for item in message_window
        ]
        initial_message_count = len(recent_messages)
        memories = (
            self.memory_reader.list(
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                team_id=team_id,
            )
            if self.memory_reader is not None
            else []
        )
        scored: list[tuple[float, LongTermMemory]] = []
        exclusions: list[ContextExclusion] = []
        category = run.requirement.category.lower()
        current_category_id = None
        if self.memory_registry is not None:
            try:
                current_category_id = self.memory_registry.resolve_category(category).category_id
            except ValueError:
                current_category_id = None
        prefixes = tuple(pattern.format(category=category) for pattern in rule.allowed_key_patterns)

        for memory in memories:
            if memory.status != MemoryStatus.ACTIVE:
                continue
            if not self._is_effective(memory):
                exclusions.append(
                    ContextExclusion(memory_id=memory.memory_id, reason="NOT_CURRENTLY_EFFECTIVE")
                )
                continue
            if memory.scope == MemoryScope.SEMANTIC:
                exclusions.append(
                    ContextExclusion(memory_id=memory.memory_id, reason="KNOWLEDGE_NOT_MEMORY")
                )
                continue
            if rule.memory_limit(memory.scope) == 0:
                exclusions.append(
                    ContextExclusion(memory_id=memory.memory_id, reason="SCOPE_NOT_ALLOWED")
                )
                continue
            is_structured = memory.category_id is not None or bool(memory.value_json)
            if self.memory_registry is not None and is_structured:
                try:
                    definition = self.memory_registry.get_definition(
                        memory.key,
                        definition_version=memory.key_definition_version,
                        category_id=memory.category_id,
                    )
                except ValueError:
                    exclusions.append(
                        ContextExclusion(
                            memory_id=memory.memory_id,
                            reason="MEMORY_KEY_NOT_REGISTERED",
                        )
                    )
                    continue
                if agent not in definition.applies_to_agents:
                    exclusions.append(
                        ContextExclusion(
                            memory_id=memory.memory_id,
                            reason="KEY_NOT_ALLOWED_FOR_AGENT",
                        )
                    )
                    continue
                if memory.category_id != current_category_id:
                    exclusions.append(
                        ContextExclusion(memory_id=memory.memory_id, reason="CATEGORY_MISMATCH")
                    )
                    continue
            elif not self._matches_key_pattern(memory.key, prefixes):
                exclusions.append(
                    ContextExclusion(memory_id=memory.memory_id, reason="KEY_NOT_ALLOWED_FOR_AGENT")
                )
                continue
            if self._current_requirement_wins(memory.key, run):
                exclusions.append(
                    ContextExclusion(memory_id=memory.memory_id, reason="CURRENT_INPUT_WINS")
                )
                continue
            scored.append((self._score(memory, category, current_category_id), memory))

        scored = self._resolve_conflicts(scored, exclusions)
        scored.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
        scope_counts: dict[MemoryScope, int] = defaultdict(int)
        selected: list[ContextMemoryItem] = []
        token_limit = min(
            rule.max_context_tokens,
            max(0, run.execution_budget.max_input_tokens - run.execution_usage.input_tokens),
        )
        fixed_tokens = self._estimate_tokens(
            {
                "requirement": run.requirement.model_dump(mode="json"),
                "messages": [item.model_dump() for item in recent_messages],
            }
        )
        # The newest user message is part of the current task, so only older history
        # may be discarded. If the required context still cannot fit, fail closed.
        while len(recent_messages) > 1 and fixed_tokens > token_limit:
            recent_messages.pop(0)
            fixed_tokens = self._estimate_tokens(
                {
                    "requirement": run.requirement.model_dump(mode="json"),
                    "messages": [item.model_dump() for item in recent_messages],
                }
            )
        if fixed_tokens > token_limit:
            raise ContextBudgetError(
                "The required procurement context exceeds the remaining input-token budget"
            )
        used_tokens = fixed_tokens

        for score, memory in scored:
            if scope_counts[memory.scope] >= rule.memory_limit(memory.scope):
                exclusions.append(
                    ContextExclusion(memory_id=memory.memory_id, reason="SCOPE_COUNT_LIMIT")
                )
                continue
            item = ContextMemoryItem(
                memory_id=memory.memory_id,
                scope=memory.scope,
                key=memory.key,
                key_definition_version=memory.key_definition_version,
                category_id=memory.category_id,
                content=memory.content,
                value_json=memory.value_json,
                tags=memory.tags,
                source_ref=memory.source_ref,
                version=memory.version,
                confidence=memory.confidence,
                relevance_score=round(score, 4),
            )
            item_tokens = self._estimate_tokens(item.model_dump(mode="json"))
            if used_tokens + item_tokens > token_limit:
                exclusions.append(
                    ContextExclusion(memory_id=memory.memory_id, reason="TOKEN_BUDGET_EXCEEDED")
                )
                continue
            selected.append(item)
            scope_counts[memory.scope] += 1
            used_tokens += item_tokens

        return AssembledAgentContext(
            payload=AgentContextPayload(
                policy_version=self.policy.version,
                recent_messages=recent_messages,
                memories=selected,
                estimated_tokens=used_tokens,
            ),
            selected_memory_ids=[item.memory_id for item in selected],
            exclusions=exclusions,
            dropped_message_count=initial_message_count - len(recent_messages),
        )

    def _score(
        self,
        memory: LongTermMemory,
        category: str,
        current_category_id: str | None,
    ) -> float:
        weights = self.policy.ranking
        task_match = 1.0
        category_match = (
            1.0
            if memory.category_id is not None and memory.category_id == current_category_id
            else 1.0
            if f".{category}." in f"{memory.key}."
            else 0.8
        )
        authority = {
            MemoryScope.TEAM: 1.0,
            MemoryScope.PROFILE: 0.85,
            MemoryScope.EPISODIC: 0.65,
            MemoryScope.SEMANTIC: 0.0,
        }[memory.scope]
        now = self.clock()
        updated_at = memory.updated_at
        # SQLite drops timezone information even when production PostgreSQL preserves it.
        if now.tzinfo is not None and updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=now.tzinfo)
        elif now.tzinfo is None and updated_at.tzinfo is not None:
            now = now.replace(tzinfo=updated_at.tzinfo)
        age_days = max(0.0, (now - updated_at).total_seconds() / 86_400)
        recency = 1 / (1 + age_days / 30)
        confidence = memory.confidence
        return min(
            1.0,
            weights.task_match * task_match
            + weights.category_or_key_match * category_match
            + weights.authority * authority
            + weights.recency * recency
            + weights.confidence * confidence,
        )

    def _is_effective(self, memory: LongTermMemory) -> bool:
        now = self.clock()
        effective_at = memory.effective_at
        expires_at = memory.expires_at
        if now.tzinfo is not None and effective_at.tzinfo is None:
            effective_at = effective_at.replace(tzinfo=now.tzinfo)
            expires_at = expires_at.replace(tzinfo=now.tzinfo) if expires_at else None
        elif now.tzinfo is None and effective_at.tzinfo is not None:
            now = now.replace(tzinfo=effective_at.tzinfo)
        return effective_at <= now and (expires_at is None or expires_at > now)

    @staticmethod
    def _matches_key_pattern(key: str, prefixes: tuple[str, ...]) -> bool:
        return any(
            key.startswith(prefix)
            if prefix.endswith((".", "_"))
            else key == prefix or key.startswith(f"{prefix}.")
            for prefix in prefixes
        )

    @staticmethod
    def _resolve_conflicts(
        scored: list[tuple[float, LongTermMemory]],
        exclusions: list[ContextExclusion],
    ) -> list[tuple[float, LongTermMemory]]:
        """Return one deterministic winner for each memory key."""
        by_key: dict[tuple[str, str | None], list[tuple[float, LongTermMemory]]] = defaultdict(list)
        for item in scored:
            by_key[(item[1].key, item[1].category_id)].append(item)

        resolved: list[tuple[float, LongTermMemory]] = []
        for (key, _), candidates in by_key.items():
            hard_standard = ".minimum_" in key or key.startswith("team.device_standard.")
            scope_priority = (
                {
                    MemoryScope.TEAM: 3,
                    MemoryScope.PROFILE: 2,
                    MemoryScope.EPISODIC: 1,
                }
                if hard_standard
                else {
                    MemoryScope.PROFILE: 3,
                    MemoryScope.TEAM: 2,
                    MemoryScope.EPISODIC: 1,
                }
            )
            candidates.sort(
                key=lambda item: (
                    scope_priority.get(item[1].scope, 0),
                    item[1].version,
                    item[1].updated_at,
                    item[0],
                ),
                reverse=True,
            )
            winner = candidates[0]
            resolved.append(winner)
            winner_content = " ".join(winner[1].content.split()).casefold()
            for _, rejected in candidates[1:]:
                rejected_content = " ".join(rejected.content.split()).casefold()
                reason = (
                    "DUPLICATE_SUPERSEDED"
                    if rejected_content == winner_content
                    else "CONFLICT_SUPERSEDED"
                )
                exclusions.append(ContextExclusion(memory_id=rejected.memory_id, reason=reason))
        return resolved

    @staticmethod
    def _current_requirement_wins(key: str, run: ProcurementRun) -> bool:
        field_by_suffix = {
            "quantity": "quantity",
            "unit_budget": "unit_budget",
            "budget_tax_included": "budget_tax_included",
            "minimum_ram_gb": "minimum_ram_gb",
            "minimum_storage_gb": "minimum_storage_gb",
            "delivery_city": "delivery_city",
            "delivery_days": "delivery_days",
        }
        return any(
            key.endswith(suffix) and getattr(run.requirement, field) is not None
            for suffix, field in field_by_suffix.items()
        )

    @staticmethod
    def _estimate_tokens(value: object) -> int:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        # Deterministic conservative approximation until provider tokenizers are wired in.
        return max(1, math.ceil(len(text.encode("utf-8")) / 3))
