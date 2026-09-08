from decimal import Decimal

from procure_pilot.domain.models import CostBreakdown, Money, Offer, ProcurementRequirement


def calculate_landed_cost(
    offer: Offer,
    requirement: ProcurementRequirement,
) -> CostBreakdown:
    """Calculate trusted cost from a canonical offer and server-owned requirement."""
    if requirement.quantity is None:
        raise ValueError("Quantity is required before landed cost can be calculated")
    subtotal = offer.unit_price.amount * requirement.quantity
    shipping = offer.shipping.amount
    if requirement.budget_tax_included:
        tax = (subtotal * offer.tax_rate / (Decimal("1") + offer.tax_rate)).quantize(
            Decimal("0.01")
        )
        unconditional = subtotal + shipping
    else:
        tax = (subtotal * offer.tax_rate).quantize(Decimal("0.01"))
        unconditional = subtotal + tax + shipping
    conditional_total = None
    if offer.conditional_discount.amount > 0:
        conditional_total = Money(
            amount=unconditional - offer.conditional_discount.amount * requirement.quantity,
            currency=offer.unit_price.currency,
        )
    return CostBreakdown(
        subtotal=Money(amount=subtotal, currency=offer.unit_price.currency),
        tax=Money(amount=tax, currency=offer.unit_price.currency),
        shipping=offer.shipping,
        unconditional_total=Money(amount=unconditional, currency=offer.unit_price.currency),
        conditional_total=conditional_total,
    )
