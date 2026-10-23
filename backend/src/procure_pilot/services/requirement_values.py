"""Canonical requirement values shared by parsing, validation and domain mapping.

Only exact registered aliases are converted. Evidence quotes, display values and
unknown/versioned names are never rewritten by this module.
"""

from __future__ import annotations

from typing import Any

from procure_pilot.domain.requirements import RequirementSpec

_CITY_ALIASES = {
    "上海": "Shanghai",
    "上海市": "Shanghai",
    "shanghai": "Shanghai",
    "深圳": "Shenzhen",
    "深圳市": "Shenzhen",
    "shenzhen": "Shenzhen",
    "雅加达": "Jakarta",
    "jakarta": "Jakarta",
}
_OPERATING_SYSTEMS = {"windows": "windows", "linux": "linux", "macos": "macos"}


def canonical_city(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return _CITY_ALIASES.get(value.strip().casefold(), value)


def canonical_operating_system(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return _OPERATING_SYSTEMS.get(value.strip().casefold(), value)


def _canonical(field: str | None, value: Any) -> Any:
    if field == "delivery_city":
        return canonical_city(value)
    if field == "operating_system":
        return canonical_operating_system(value)
    return value


def _delta_value(field: str | None, value: Any) -> Any:
    if isinstance(value, dict):
        # A full constraint carries its own field; its value is semantic data.
        if value.get("field") in {"delivery_city", "operating_system"} and "value" in value:
            return {**value, "value": _canonical(value["field"], value["value"])}
        # Sourced values keep the original `value` and evidence references intact.
        if field in {"delivery_city", "operating_system"} and "normalized_value" in value:
            return {**value, "normalized_value": _canonical(field, value["normalized_value"])}
        if field in {"delivery_city", "operating_system"} and "value" in value:
            return {**value, "value": _canonical(field, value["value"])}
        return value
    if isinstance(value, list):
        return [_delta_value(None, item) for item in value]
    return _canonical(field, value)


def normalize_requirement_values(spec: RequirementSpec) -> None:
    """Normalize an already checked copy; never mutate its confirmed base snapshot."""
    if spec.delivery_city is not None:
        spec.delivery_city.normalized_value = canonical_city(spec.delivery_city.normalized_value)
    constraints = [*spec.hard_constraints, *spec.soft_preferences]
    for constraint in constraints:
        constraint.value = _canonical(constraint.field, constraint.value)
    fields_by_id = {c.constraint_id: c.field for c in constraints}
    for operation in spec.delta.operations:
        path = operation.path.strip("/").replace(".", "/")
        parts = path.split("/")
        field = parts[-1]
        if parts[0] in {"hard_constraints", "soft_preferences"} and len(parts) > 1:
            field = fields_by_id.get(parts[1], parts[1])
        operation.old_value = _delta_value(field, operation.old_value)
        operation.new_value = _delta_value(field, operation.new_value)
