"""Text reports for the warehouse inventory."""

from vendor import tinytable

Item = tuple[str, int, float]


def format_inventory(items: list[Item]) -> str:
    """All items as a table, numbers right-aligned."""
    rows = [(name, qty, f"{price:.2f}") for name, qty, price in items]
    return tinytable.render_table(["Item", "Qty", "Price"], rows, justify="right")


def format_low_stock(items: list[Item], limit: int) -> str:
    """The items with fewer than `limit` units, left-aligned."""
    rows = [(name, qty) for name, qty, _ in items if qty < limit]
    return tinytable.render_table(["Item", "Qty"], rows, justify="left")
