"""Fictitious completed purchase orders, independent of procurement Run drafts."""

from datetime import UTC, datetime
from typing import Protocol

from procure_pilot.domain.references import HistoricalPurchaseOrder, ReferenceValue


class PurchaseHistoryReadPort(Protocol):
    def search(
        self,
        *,
        tenant_id: str,
        user_id: str,
        department: str,
        roles: tuple[str, ...],
        category: str,
        year: int | None = None,
    ) -> list[HistoricalPurchaseOrder]: ...


def order(order_id, tenant, owner, *, ram=32, storage=1024, month=9, day=15, status="FULFILLED"):
    values = {
        "category": "laptop",
        "purpose": "开发",
        "quantity": 20,
        "budget.maximum": "8000.00",
        "budget.tax_included": True,
        "delivery_city": "Jakarta",
        "minimum_ram_gb": ram,
        "minimum_storage_gb": storage,
        "minimum_warranty_months": 36,
        "condition": "NEW",
    }
    return HistoricalPurchaseOrder(
        order_id=order_id,
        tenant_id=tenant,
        owner_user_id=owner,
        department="R&D",
        category="laptop",
        purchased_at=datetime(2026, month, day, tzinfo=UTC),
        status=status,
        model_text="模拟研发笔记本",
        fields=[
            ReferenceValue(
                evidence_id=f"history:{order_id}:line1:{field}",
                field=field,
                value=value,
                operator="GTE" if field.startswith("minimum_") else "EQ",
                authority="REFERENCE",
                source_kind="HISTORY",
                source_ref=f"fixture-order://{tenant}/{order_id}/line1/{field}",
                quote=f"模拟采购单 {order_id}：{field} = {value}",
                source_version="1",
            )
            for field, value in values.items()
        ],
    )


class FixturePurchaseHistoryAdapter:
    def __init__(self):
        self.orders = [
            order("HIST-CN-001", "demo_cn", "employee_001", month=10, day=1),
            order("HIST-CN-002", "demo_cn", "employee_001", ram=16, storage=512),
            order("HIST-CN-003", "demo_cn", "employee_other", month=8),
            order("HIST-ID-001", "demo_id", "employee_101"),
            order("HIST-CN-CANCELLED", "demo_cn", "employee_001", status="CANCELLED"),
            order("HIST-CN-DRAFT", "demo_cn", "employee_001", status="DRAFT_NOT_SUBMITTED"),
        ]

    def search(self, *, tenant_id, user_id, department, roles, category, year=None):
        return sorted(
            [
                o.model_copy(deep=True)
                for o in self.orders
                if o.tenant_id == tenant_id
                and o.category == category
                and (year is None or o.purchased_at.year == year)
                and o.status in {"APPROVED", "FULFILLED"}
                and (
                    o.owner_user_id == user_id
                    or ("PROCUREMENT_SPECIALIST" in roles and o.department == department)
                )
            ],
            key=lambda o: o.purchased_at,
            reverse=True,
        )[:3]
