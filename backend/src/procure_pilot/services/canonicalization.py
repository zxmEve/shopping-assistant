from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from procure_pilot.domain.models import (
    BundleType,
    MatchDecision,
    MatchDecisionType,
    Offer,
    OfferMatchAssessment,
    Product,
    ProductCondition,
    ProviderRawOffer,
    Variant,
)

MATCH_THRESHOLD = 0.90
CRITICAL_FIELDS = (
    "brand",
    "model",
    "cpu",
    "ram_gb",
    "storage_gb",
    "operating_system",
    "condition",
    "bundle",
)
FIELD_WEIGHTS = {
    "brand": 0.08,
    "model": 0.18,
    "cpu": 0.10,
    "ram_gb": 0.15,
    "storage_gb": 0.15,
    "operating_system": 0.10,
    "condition": 0.12,
    "bundle": 0.12,
}


@dataclass(frozen=True)
class CanonicalizationResult:
    assessment: OfferMatchAssessment
    offer: Offer | None


@dataclass
class ParsedFields:
    product: Product | None = None
    cpu: str | None = None
    ram_gb: int | None = None
    storage_gb: int | None = None
    operating_system: str | None = None
    warranty_months: int | None = None
    condition: ProductCondition | None = None
    bundle: BundleType | None = None
    matched_fields: list[str] = field(default_factory=list)
    conflict_fields: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    source_counts: dict[str, int] = field(default_factory=dict)


class CanonicalizationPipeline:
    """Normalize raw provider payloads and enforce a precision-first server-side merge gate."""

    def __init__(self, products: list[Product], variants: list[Variant]) -> None:
        self.products = products
        self.variants = variants
        self._products_by_key = {
            (_token(product.brand), _token(product.model)): product for product in products
        }
        self._cpus = {_token(variant.cpu): variant.cpu for variant in variants}
        self._variants_by_signature = {
            self._variant_signature(variant): variant for variant in variants
        }

    def canonicalize(
        self,
        raw: ProviderRawOffer,
        *,
        proposed_product: Product | None = None,
        proposed_variant: Variant | None = None,
    ) -> CanonicalizationResult:
        parsed = self._parse(raw)
        if proposed_product is None and parsed.product is not None:
            proposed_product = parsed.product
        if (
            proposed_variant is None
            and proposed_product is not None
            and not parsed.missing_fields
            and not parsed.conflict_fields
        ):
            proposed_variant = self._variant_for(proposed_product, parsed)

        proposal_conflicts = self._proposal_conflicts(
            parsed,
            proposed_product=proposed_product,
            proposed_variant=proposed_variant,
        )
        conflicts = list(dict.fromkeys([*parsed.conflict_fields, *proposal_conflicts]))
        evidence_refs = [raw.evidence.evidence_id, raw.evidence.source_ref]

        if conflicts:
            decision = MatchDecision(
                canonical_product_id=(proposed_product.product_id if proposed_product else None),
                canonical_variant_id=(proposed_variant.variant_id if proposed_variant else None),
                confidence=0.0,
                matched_fields=parsed.matched_fields,
                conflict_fields=conflicts,
                decision=MatchDecisionType.REJECTED,
                reasons=[f"关键字段冲突，服务端拒绝合并：{', '.join(conflicts)}"],
                evidence_refs=evidence_refs,
            )
            return self._result(raw, parsed, decision, offer=None)

        missing = list(parsed.missing_fields)
        if proposed_product is None:
            missing.extend(field for field in ("brand", "model") if field not in missing)
        if proposed_variant is None:
            missing.append("canonical_variant_id")
        missing = list(dict.fromkeys(missing))
        confidence = self._confidence(parsed)
        if missing or confidence < MATCH_THRESHOLD:
            reasons = []
            if missing:
                reasons.append(f"关键字段无法确认：{', '.join(missing)}")
            if confidence < MATCH_THRESHOLD:
                reasons.append(f"匹配置信度 {confidence:.2f} 低于可比价门槛 {MATCH_THRESHOLD:.2f}")
            decision = MatchDecision(
                canonical_product_id=(proposed_product.product_id if proposed_product else None),
                canonical_variant_id=None,
                confidence=confidence,
                matched_fields=parsed.matched_fields,
                conflict_fields=[],
                decision=MatchDecisionType.REVIEW_REQUIRED,
                reasons=reasons,
                evidence_refs=evidence_refs,
            )
            return self._result(raw, parsed, decision, offer=None)

        assert proposed_product is not None
        assert proposed_variant is not None
        decision = MatchDecision(
            canonical_product_id=proposed_product.product_id,
            canonical_variant_id=proposed_variant.variant_id,
            confidence=confidence,
            matched_fields=parsed.matched_fields,
            conflict_fields=[],
            decision=MatchDecisionType.MATCH,
            reasons=["关键 Product/Variant 字段一致，允许进入确定性比价"],
            evidence_refs=evidence_refs,
        )
        offer = Offer(
            offer_id=raw.raw_offer_id,
            provider=raw.provider,
            seller=raw.seller,
            vendor_id=raw.vendor_id,
            product=proposed_product,
            variant=proposed_variant,
            title=raw.title,
            unit_price=raw.unit_price,
            tax_rate=raw.tax_rate,
            shipping=raw.shipping,
            conditional_discount=raw.conditional_discount,
            discount_condition=raw.discount_condition,
            stock=raw.stock,
            delivery_days=raw.delivery_days,
            evidence=raw.evidence,
            match_decision=decision,
        )
        return self._result(raw, parsed, decision, offer=offer)

    def _parse(self, raw: ProviderRawOffer) -> ParsedFields:
        parsed = ParsedFields()
        title_token = _token(raw.title)
        structured_product = self._product_from_structured(raw.brand, raw.model)
        title_product = self._product_from_title(raw.title)

        if structured_product and title_product and structured_product != title_product:
            parsed.conflict_fields.extend(["brand", "model"])
        elif structured_product:
            parsed.product = structured_product
            if _token(structured_product.model) not in title_token:
                parsed.conflict_fields.append("model")
            elif _token(structured_product.brand) not in title_token:
                parsed.conflict_fields.append("brand")
            else:
                parsed.matched_fields.extend(["brand", "model"])
                parsed.source_counts.update({"brand": 2, "model": 2})
        elif title_product:
            parsed.product = title_product
            parsed.matched_fields.extend(["brand", "model"])
            parsed.source_counts.update({"brand": 1, "model": 1})
        else:
            parsed.missing_fields.extend(["brand", "model"])

        self._reconcile(parsed, "cpu", _canonical_cpu(raw.cpu, self._cpus), self._cpu(raw.title))
        self._reconcile(parsed, "ram_gb", _memory_gb(raw.ram), _title_ram(raw.title))
        self._reconcile(
            parsed,
            "storage_gb",
            _storage_gb(raw.storage),
            _title_storage(raw.title),
        )
        self._reconcile(
            parsed,
            "operating_system",
            _operating_system(raw.operating_system),
            _operating_system(raw.title),
        )
        self._reconcile(
            parsed,
            "warranty_months",
            _warranty_months(raw.warranty),
            _warranty_months(raw.title),
            required=False,
        )
        self._reconcile(
            parsed,
            "condition",
            _condition(raw.condition),
            _condition(raw.title),
        )
        self._reconcile(parsed, "bundle", _bundle(raw.bundle), _bundle(raw.title))
        return parsed

    @staticmethod
    def _reconcile(
        parsed: ParsedFields,
        field_name: str,
        structured_value: object | None,
        title_value: object | None,
        *,
        required: bool = True,
    ) -> None:
        if structured_value is not None and title_value is not None:
            if structured_value != title_value:
                parsed.conflict_fields.append(field_name)
                return
            value = structured_value
            parsed.source_counts[field_name] = 2
        else:
            value = structured_value if structured_value is not None else title_value
            if value is not None:
                parsed.source_counts[field_name] = 1
        if value is None:
            if required:
                parsed.missing_fields.append(field_name)
            return
        setattr(parsed, field_name, value)
        parsed.matched_fields.append(field_name)

    def _product_from_structured(self, brand: str | None, model: str | None) -> Product | None:
        if not brand or not model:
            return None
        return self._products_by_key.get((_token(brand), _token(model)))

    def _product_from_title(self, title: str) -> Product | None:
        normalized = _token(title)
        matches = [
            product
            for product in self.products
            if _token(product.brand) in normalized and _token(product.model) in normalized
        ]
        return matches[0] if len(matches) == 1 else None

    def _cpu(self, title: str) -> str | None:
        normalized = _token(title)
        matches = [value for key, value in self._cpus.items() if key in normalized]
        return matches[0] if len(matches) == 1 else None

    def _variant_for(self, product: Product, parsed: ParsedFields) -> Variant:
        assert parsed.cpu is not None
        assert parsed.ram_gb is not None
        assert parsed.storage_gb is not None
        assert parsed.operating_system is not None
        assert parsed.condition is not None
        assert parsed.bundle is not None
        warranty = parsed.warranty_months or 0
        candidate = Variant(
            variant_id="pending",
            product_id=product.product_id,
            cpu=parsed.cpu,
            ram_gb=parsed.ram_gb,
            storage_gb=parsed.storage_gb,
            operating_system=parsed.operating_system,
            warranty_months=warranty,
            condition=parsed.condition,
            bundle=parsed.bundle,
        )
        known = self._variants_by_signature.get(self._variant_signature(candidate))
        if known:
            return known
        digest = hashlib.sha256(
            "|".join(str(item) for item in self._variant_signature(candidate)).encode("utf-8")
        ).hexdigest()[:12]
        return candidate.model_copy(update={"variant_id": f"variant_c_{digest}"})

    @staticmethod
    def _variant_signature(variant: Variant) -> tuple[object, ...]:
        return (
            variant.product_id,
            _token(variant.cpu),
            variant.ram_gb,
            variant.storage_gb,
            _token(variant.operating_system),
            variant.warranty_months,
            variant.condition,
            variant.bundle,
        )

    @staticmethod
    def _proposal_conflicts(
        parsed: ParsedFields,
        *,
        proposed_product: Product | None,
        proposed_variant: Variant | None,
    ) -> list[str]:
        conflicts: list[str] = []
        if (
            parsed.product
            and proposed_product
            and parsed.product.product_id != proposed_product.product_id
        ):
            conflicts.extend(["brand", "model"])
        if not proposed_variant:
            return conflicts
        checks = {
            "product_id": parsed.product.product_id if parsed.product else None,
            "cpu": parsed.cpu,
            "ram_gb": parsed.ram_gb,
            "storage_gb": parsed.storage_gb,
            "operating_system": parsed.operating_system,
            "warranty_months": parsed.warranty_months,
            "condition": parsed.condition,
            "bundle": parsed.bundle,
        }
        for field_name, actual in checks.items():
            if actual is not None and actual != getattr(proposed_variant, field_name):
                conflicts.append(field_name)
        return conflicts

    @staticmethod
    def _confidence(parsed: ParsedFields) -> float:
        confidence = 0.0
        for field_name, weight in FIELD_WEIGHTS.items():
            sources = parsed.source_counts.get(field_name, 0)
            if sources == 2:
                confidence += weight
            elif sources == 1:
                confidence += weight * 0.82
        return round(min(confidence, 0.99), 4)

    @staticmethod
    def _result(
        raw: ProviderRawOffer,
        parsed: ParsedFields,
        decision: MatchDecision,
        *,
        offer: Offer | None,
    ) -> CanonicalizationResult:
        assessment = OfferMatchAssessment(
            raw_offer_id=raw.raw_offer_id,
            provider=raw.provider,
            title=raw.title,
            condition=parsed.condition,
            bundle=parsed.bundle,
            comparable=decision.decision == MatchDecisionType.MATCH,
            match_decision=decision,
        )
        return CanonicalizationResult(assessment=assessment, offer=offer)


def _token(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


def _canonical_cpu(value: str | None, aliases: dict[str, str]) -> str | None:
    if not value:
        return None
    return aliases.get(_token(value))


def _memory_gb(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(\d+)\s*(mb|gb|g)\b", value.lower())
    if not match:
        return None
    amount = int(match.group(1))
    if match.group(2) == "mb":
        amount //= 1024
    return amount if 4 <= amount <= 512 else None


def _storage_gb(value: str | None) -> int | None:
    if not value:
        return None
    lowered = value.lower()
    match = re.search(r"(\d+)\s*(tb|t|gb|g)\b", lowered)
    if not match:
        return None
    amount = int(match.group(1))
    if match.group(2) in {"tb", "t"}:
        amount *= 1024
    return amount if 128 <= amount <= 16384 else None


def _title_ram(title: str) -> int | None:
    values: list[int] = []
    for amount, unit in re.findall(r"(\d+)\s*(mb|gb|g)\b", title.lower()):
        value = int(amount)
        if unit == "mb":
            value //= 1024
        if 4 <= value <= 128:
            values.append(value)
    return values[0] if values else None


def _title_storage(title: str) -> int | None:
    lowered = title.lower()
    tb = re.search(r"(\d+)\s*(tb|t)\b", lowered)
    if tb:
        return int(tb.group(1)) * 1024
    values = [
        int(amount) for amount, _ in re.findall(r"(\d+)\s*(gb|g)\b", lowered) if int(amount) >= 128
    ]
    return values[0] if values else None


def _operating_system(value: str | None) -> str | None:
    if not value:
        return None
    normalized = _token(value)
    aliases = {
        "win11pro": "Windows 11 Pro",
        "windows11pro": "Windows 11 Pro",
        "windows11professional": "Windows 11 Pro",
        "ubuntu2404lts": "Ubuntu 24.04 LTS",
        "ubuntu2404": "Ubuntu 24.04 LTS",
    }
    for alias, canonical in aliases.items():
        if alias in normalized:
            return canonical
    return None


def _warranty_months(value: str | None) -> int | None:
    if not value:
        return None
    month_match = re.search(r"(\d+)\s*(?:个月|月|months?|mo)\b", value.lower())
    if month_match:
        return int(month_match.group(1))
    year_match = re.search(r"(\d+)\s*(?:年|years?|yr)\b", value.lower())
    return int(year_match.group(1)) * 12 if year_match else None


def _condition(value: str | None) -> ProductCondition | None:
    if not value:
        return None
    normalized = value.lower()
    if any(marker in normalized for marker in ("翻新", "refurbished", "renewed")):
        return ProductCondition.REFURBISHED
    if any(marker in normalized for marker in ("二手", "used", "pre-owned")):
        return ProductCondition.USED
    if any(marker in normalized for marker in ("全新", "新品", "brand new")) or re.search(
        r"\bnew\b", normalized
    ):
        return ProductCondition.NEW
    return None


def _bundle(value: str | None) -> BundleType | None:
    if not value:
        return None
    normalized = value.lower()
    if any(marker in normalized for marker in ("扩展坞", "dock bundle", "with dock")):
        return BundleType.DOCK_BUNDLE
    if any(marker in normalized for marker in ("单机", "device only", "standalone")):
        return BundleType.DEVICE_ONLY
    return None
