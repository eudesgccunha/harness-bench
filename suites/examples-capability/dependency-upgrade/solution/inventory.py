"""Text reports for the warehouse inventory."""

from vendor import tinytable

Item = tuple[str, int, float]


def format_inventory(items: list[Item]) -> str:
    """All items as a table, numbers right-aligned."""
    table = tinytable.Table(["Item", "Qty", "Price"])
    for name, qty, price in items:
        table.add_row(name, qty, f"{price:.2f}")
    return table.render(align="right")


def format_low_stock(items: list[Item], limit: int) -> str:
    """The items with fewer than `limit` units, left-aligned."""
    table = tinytable.Table(["Item", "Qty"])
    for name, qty, _ in items:
        if qty < limit:
            table.add_row(name, qty)
    return table.render(align="left")
