from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from procure_pilot.agents.contracts import AgentKind
from procure_pilot.domain.memory import MemoryScope


class RegistryStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"


class MemorySensitivity(StrEnum):
    LOW = "LOW"
    PERSONAL = "PERSONAL"
    RESTRICTED = "RESTRICTED"


class MemoryMergeStrategy(StrEnum):
    REPLACE = "REPLACE"
    SET_UNION = "SET_UNION"
    KEEP_LATEST = "KEEP_LATEST"
    MANUAL = "MANUAL"


class CategoryDefinition(BaseModel):
    category_id: str
    code: str
    key_segment: str
    parent_id: str | None = None
    localized_names: dict[str, str]
    aliases: list[str] = Field(default_factory=list)
    status: RegistryStatus = RegistryStatus.ACTIVE


class TagDefinition(BaseModel):
    tag_id: str
    code: str
    aliases: list[str] = Field(default_factory=list)
    applicable_categories: list[str]
    definition_version: int = Field(default=1, gt=0)
    status: RegistryStatus = RegistryStatus.ACTIVE


class MemoryKeyDefinition(BaseModel):
    key: str
    definition_version: int = Field(gt=0)
    value_schema: dict[str, Any]
    allowed_scopes: list[MemoryScope] = Field(min_length=1)
    applicable_categories: list[str] = Field(min_length=1)
    applies_to_agents: list[AgentKind] = Field(min_length=1)
    sensitivity: MemorySensitivity
    merge_strategy: MemoryMergeStrategy
    default_ttl_days: int | None = Field(default=None, gt=0)
    requires_confirmation: bool = True
    status: RegistryStatus = RegistryStatus.ACTIVE


class MemoryRegistryDocument(BaseModel):
    version: str
    categories: list[CategoryDefinition]
    tags: list[TagDefinition]
    keys: list[MemoryKeyDefinition]

    @model_validator(mode="after")
    def unique_ids_and_references(self) -> MemoryRegistryDocument:
        category_ids = [item.category_id for item in self.categories]
        tag_ids = [item.tag_id for item in self.tags]
        key_versions = [(item.key, item.definition_version) for item in self.keys]
        if len(category_ids) != len(set(category_ids)):
            raise ValueError("Category IDs must be unique")
        if len(tag_ids) != len(set(tag_ids)):
            raise ValueError("Tag IDs must be unique")
        if len(key_versions) != len(set(key_versions)):
            raise ValueError("Memory key/version pairs must be unique")
        known_categories = set(category_ids)
        for category in self.categories:
            if category.parent_id and category.parent_id not in known_categories:
                raise ValueError(f"Unknown parent category: {category.parent_id}")
        for item in [*self.tags, *self.keys]:
            unknown = set(item.applicable_categories) - known_categories
            if unknown:
                raise ValueError(f"Unknown applicable categories: {sorted(unknown)}")
        return self


class MemoryRegistry:
    """Version-controlled business vocabulary; models and users cannot publish definitions."""

    def __init__(self, document: MemoryRegistryDocument) -> None:
        self.document = document
        self._categories = {item.category_id: item for item in document.categories}
        self._definitions = {
            (item.key, item.definition_version): item
            for item in document.keys
            if item.status == RegistryStatus.ACTIVE
        }
        self._latest_definitions: dict[str, MemoryKeyDefinition] = {}
        for definition in self._definitions.values():
            current = self._latest_definitions.get(definition.key)
            if current is None or definition.definition_version > current.definition_version:
                self._latest_definitions[definition.key] = definition
        self._category_aliases = self._build_category_aliases(document.categories)
        self._tag_aliases = self._build_tag_aliases(document.tags)

    @classmethod
    def from_yaml(cls, path: Path) -> MemoryRegistry:
        document = MemoryRegistryDocument.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        )
        return cls(document)

    @property
    def version(self) -> str:
        return self.document.version

    def list_definitions(self) -> list[MemoryKeyDefinition]:
        return sorted(self._latest_definitions.values(), key=lambda item: item.key)

    def list_categories(self) -> list[CategoryDefinition]:
        return sorted(self._categories.values(), key=lambda item: item.category_id)

    def list_tags(self) -> list[TagDefinition]:
        return sorted(self.document.tags, key=lambda item: item.tag_id)

    def get_category(self, category_id: str) -> CategoryDefinition:
        try:
            category = self._categories[category_id]
        except KeyError as exc:
            raise ValueError(f"Unknown category_id: {category_id}") from exc
        if category.status != RegistryStatus.ACTIVE:
            raise ValueError(f"Category is not active: {category_id}")
        return category

    def resolve_category(self, value: str) -> CategoryDefinition:
        matches = self._category_aliases.get(self._normalize(value), set())
        if len(matches) != 1:
            if not matches:
                raise ValueError(f"Unknown category: {value}")
            raise ValueError(f"Ambiguous category alias: {value}")
        return self.get_category(next(iter(matches)))

    def get_definition(
        self,
        key: str,
        *,
        definition_version: int | None = None,
        category_id: str | None = None,
    ) -> MemoryKeyDefinition:
        definition = (
            self._definitions.get((key, definition_version))
            if definition_version is not None
            else self._latest_definitions.get(key)
        )
        if definition is None and category_id is not None:
            category = self.get_category(category_id)
            for candidate in self._latest_definitions.values():
                legacy_key = candidate.key.replace(".category.", f".{category.key_segment}.")
                if key == legacy_key:
                    definition = candidate
                    break
        if definition is None:
            raise ValueError(f"Memory key is not registered: {key}")
        return definition

    def validate_memory(
        self,
        *,
        key: str,
        definition_version: int | None,
        category_id: str | None,
        scope: MemoryScope,
        value_json: dict[str, Any],
        tags: list[str],
    ) -> tuple[MemoryKeyDefinition, list[str]]:
        definition = self.get_definition(
            key,
            definition_version=definition_version,
            category_id=category_id,
        )
        if scope not in definition.allowed_scopes:
            raise ValueError(f"Memory key {definition.key} does not allow scope {scope}")
        if category_id is None:
            raise ValueError(f"Memory key {definition.key} requires category_id")
        self.get_category(category_id)
        if category_id not in definition.applicable_categories:
            raise ValueError(f"Memory key {definition.key} does not apply to {category_id}")
        self._validate_schema(value_json, definition.value_schema, path="value_json")
        canonical_tags = self.canonicalize_tags(tags, category_id=category_id)
        return definition, canonical_tags

    def applies_to_agent(
        self,
        *,
        key: str,
        definition_version: int,
        category_id: str | None,
        agent: AgentKind,
        current_category_id: str | None,
    ) -> bool:
        try:
            definition = self.get_definition(
                key,
                definition_version=definition_version,
                category_id=category_id,
            )
        except ValueError:
            return False
        return (
            agent in definition.applies_to_agents
            and category_id is not None
            and category_id == current_category_id
        )

    def canonicalize_tags(self, values: list[str], *, category_id: str) -> list[str]:
        result: list[str] = []
        for value in values:
            matches = self._tag_aliases.get(self._normalize(value), set())
            if len(matches) != 1:
                if not matches:
                    raise ValueError(f"Tag is not registered: {value}")
                raise ValueError(f"Ambiguous tag alias: {value}")
            tag_id = next(iter(matches))
            tag = next(item for item in self.document.tags if item.tag_id == tag_id)
            if tag.status != RegistryStatus.ACTIVE or category_id not in tag.applicable_categories:
                raise ValueError(f"Tag {tag_id} does not apply to {category_id}")
            if tag_id not in result:
                result.append(tag_id)
        return result

    @classmethod
    def _build_category_aliases(cls, categories: list[CategoryDefinition]) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for category in categories:
            values = [
                category.category_id,
                category.code,
                category.key_segment,
                *category.localized_names.values(),
                *category.aliases,
            ]
            for value in values:
                result.setdefault(cls._normalize(value), set()).add(category.category_id)
        return result

    @classmethod
    def _build_tag_aliases(cls, tags: list[TagDefinition]) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for tag in tags:
            for value in [tag.tag_id, tag.code, *tag.aliases]:
                result.setdefault(cls._normalize(value), set()).add(tag.tag_id)
        return result

    @staticmethod
    def _normalize(value: str) -> str:
        return "".join(value.casefold().strip().split())

    @classmethod
    def _validate_schema(cls, value: Any, schema: dict[str, Any], *, path: str) -> None:
        expected_type = schema.get("type")
        type_matches = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
        }
        if expected_type and not type_matches.get(expected_type, False):
            raise ValueError(f"{path} must be {expected_type}")
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError(f"{path} must be one of {schema['enum']}")
        if isinstance(value, dict):
            required = set(schema.get("required", []))
            missing = required - set(value)
            if missing:
                raise ValueError(f"{path} is missing required fields: {sorted(missing)}")
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False:
                unexpected = set(value) - set(properties)
                if unexpected:
                    raise ValueError(f"{path} has unexpected fields: {sorted(unexpected)}")
            for name, item in value.items():
                if name in properties:
                    cls._validate_schema(item, properties[name], path=f"{path}.{name}")
        elif isinstance(value, list):
            if len(value) < schema.get("minItems", 0):
                raise ValueError(f"{path} has too few items")
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                raise ValueError(f"{path} has too many items")
            for index, item in enumerate(value):
                cls._validate_schema(item, schema.get("items", {}), path=f"{path}[{index}]")
        elif isinstance(value, str):
            if len(value) < schema.get("minLength", 0):
                raise ValueError(f"{path} is too short")
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                raise ValueError(f"{path} is too long")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                raise ValueError(f"{path} is below the minimum")
            if "maximum" in schema and value > schema["maximum"]:
                raise ValueError(f"{path} is above the maximum")
