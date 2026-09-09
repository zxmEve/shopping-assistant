from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml

from procure_pilot.domain.models import PolicyDecision


@dataclass(frozen=True)
class EmployeeContext:
    tenant_id: str
    user_id: str
    department: str
    cost_center: str
    location: str
    roles: tuple[str, ...]


@dataclass(frozen=True)
class BudgetSnapshot:
    cost_center: str
    available: Decimal
    currency: str = "CNY"
    version: int = 3


@dataclass(frozen=True)
class VendorRecord:
    vendor_id: str
    status: str
    invoice_capable: bool
    risk: str


@dataclass(frozen=True)
class PolicyResult:
    decision: PolicyDecision
    approval_route: list[str]
    reasons: list[str]
    evidence_refs: list[str]


class FixtureEnterpriseGateway:
    BUDGET_REF = "budget://demo_cn/rd_jakarta/v3"

    def __init__(self, policy_path: Path | None = None) -> None:
        policy_path = (
            policy_path or Path(__file__).resolve().parents[3] / "fixtures" / "policy_v3.yaml"
        )
        self.policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
        self.policy_ref = f"policy://{self.policy['policy_id']}/v{self.policy['version']}"
        self.employees = {
            ("demo_cn", "employee_001"): EmployeeContext(
                tenant_id="demo_cn",
                user_id="employee_001",
                department="R&D",
                cost_center="rd_jakarta",
                location="Shanghai",
                roles=("EMPLOYEE",),
            ),
            ("demo_id", "employee_101"): EmployeeContext(
                tenant_id="demo_id",
                user_id="employee_101",
                department="R&D",
                cost_center="rd_jakarta",
                location="Jakarta",
                roles=("EMPLOYEE",),
            ),
            ("demo_cn", "knowledge_admin_001"): EmployeeContext(
                tenant_id="demo_cn",
                user_id="knowledge_admin_001",
                department="Procurement",
                cost_center="rd_jakarta",
                location="Shanghai",
                roles=("KNOWLEDGE_STEWARD",),
            ),
        }
        self.budgets = {"rd_jakarta": BudgetSnapshot("rd_jakarta", Decimal("220000"))}
        self.vendors = {
            "vendor_internal": VendorRecord("vendor_internal", "APPROVED", True, "LOW"),
            "vendor_alpha": VendorRecord("vendor_alpha", "APPROVED", True, "MEDIUM"),
            "vendor_beta": VendorRecord("vendor_beta", "PENDING", True, "MEDIUM"),
            "vendor_reseller": VendorRecord("vendor_reseller", "PENDING", False, "HIGH"),
        }

    def identity(self, tenant_id: str, user_id: str) -> EmployeeContext:
        try:
            return self.employees[(tenant_id, user_id)]
        except KeyError as exc:
            raise PermissionError("Employee is not available in this tenant") from exc

    def budget(self, employee: EmployeeContext) -> BudgetSnapshot:
        return self.budgets[employee.cost_center]

    def evaluate(
        self,
        *,
        employee: EmployeeContext,
        vendor_id: str,
        landed_cost: Decimal,
        ram_gb: int,
        warranty_months: int,
        condition: str,
    ) -> PolicyResult:
        budget = self.budget(employee)
        vendor = self.vendors[vendor_id]
        reasons: list[str] = []
        route: list[str] = []
        decision = PolicyDecision.ALLOW

        baseline = self.policy["developer_laptop_baseline"]
        if (
            ram_gb < int(baseline["minimum_ram_gb"])
            or warranty_months < int(baseline["minimum_warranty_months"])
            or condition != baseline["condition"]
        ):
            return PolicyResult(
                decision=PolicyDecision.DENY,
                approval_route=[],
                reasons=["研发笔记本必须至少 32GB 内存、36 个月保修且为全新商品"],
                evidence_refs=[self.policy_ref],
            )
        if landed_cost > budget.available:
            return PolicyResult(
                decision=PolicyDecision.DENY,
                approval_route=[],
                reasons=[f"采购总额超过成本中心可用预算 {budget.available} CNY"],
                evidence_refs=[self.policy_ref, self.BUDGET_REF],
            )
        for threshold in self.policy["approval_thresholds"]:
            if landed_cost > Decimal(str(threshold["amount"])):
                route.append(str(threshold["approver"]))
        if vendor.status != "APPROVED":
            decision = PolicyDecision.ALLOW_WITH_APPROVAL
            route.append(str(self.policy["vendor_exception_approver"]))
            reasons.append("供应商尚未准入，需要采购专员例外审核")
        if not vendor.invoice_capable:
            decision = PolicyDecision.ALLOW_WITH_APPROVAL
            reasons.append("供应商发票能力未验证")
        if route and decision == PolicyDecision.ALLOW:
            decision = PolicyDecision.ALLOW_WITH_APPROVAL
        reasons.append(f"按 DEMO-PROC-2026-01 v3 生成 {len(route)} 节点审批路径")
        return PolicyResult(
            decision=decision,
            approval_route=list(dict.fromkeys(route)),
            reasons=reasons,
            evidence_refs=[self.policy_ref, self.BUDGET_REF],
        )
