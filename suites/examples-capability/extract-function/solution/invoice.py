"""Plain-text invoices."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

TAX_RATE = Decimal("0.08")
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Order:
    items: list[tuple[str, int, Decimal]]  # (name, quantity, unit price)
    discount_code: str | None = None


def discount_amount(subtotal: Decimal, code: str | None) -> Decimal:
    """The amount a discount code takes off `subtotal`; zero for no code or an unknown one."""
    if code == "SAVE10":
        return (subtotal * Decimal("0.10")).quantize(CENT, ROUND_HALF_UP)
    if code == "FLAT5":
        return min(Decimal("5.00"), subtotal)
    return Decimal("0")


def render_invoice(order: Order) -> str:
    lines = []
    subtotal = Decimal("0")
    for name, qty, unit in order.items:
        line_total = unit * qty
        subtotal += line_total
        lines.append(f"{name} x{qty}: {line_total:.2f}")

    discount = discount_amount(subtotal, order.discount_code)

    taxable = subtotal - discount
    tax = (taxable * TAX_RATE).quantize(CENT, ROUND_HALF_UP)
    lines.append(f"Subtotal: {subtotal:.2f}")
    if discount:
        lines.append(f"Discount: -{discount:.2f}")
    lines.append(f"Tax: {tax:.2f}")
    lines.append(f"Total: {taxable + tax:.2f}")
    return "\n".join(lines)
