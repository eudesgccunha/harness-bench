"""Plain-text invoices."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

TAX_RATE = Decimal("0.08")
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Order:
    items: list[tuple[str, int, Decimal]]  # (name, quantity, unit price)
    discount_code: str | None = None


def render_invoice(order: Order) -> str:
    lines = []
    subtotal = Decimal("0")
    for name, qty, unit in order.items:
        line_total = unit * qty
        subtotal += line_total
        lines.append(f"{name} x{qty}: {line_total:.2f}")

    discount = Decimal("0")
    if order.discount_code == "SAVE10":
        discount = (subtotal * Decimal("0.10")).quantize(CENT, ROUND_HALF_UP)
    elif order.discount_code == "FLAT5":
        discount = min(Decimal("5.00"), subtotal)

    taxable = subtotal - discount
    tax = (taxable * TAX_RATE).quantize(CENT, ROUND_HALF_UP)
    lines.append(f"Subtotal: {subtotal:.2f}")
    if discount:
        lines.append(f"Discount: -{discount:.2f}")
    lines.append(f"Tax: {tax:.2f}")
    lines.append(f"Total: {taxable + tax:.2f}")
    return "\n".join(lines)
