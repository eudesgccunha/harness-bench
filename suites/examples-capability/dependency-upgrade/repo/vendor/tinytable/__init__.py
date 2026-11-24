"""tinytable 2.0: plain-text tables.

Changes since 1.x (see CHANGELOG.md): `render_table()` is gone, tables are built with `Table` instead, and the
`justify` option is now called `align`.
"""

from collections.abc import Sequence

ALIGNMENTS = ("left", "right")


class Table:
    """A table with fixed columns. Add rows with `add_row`, then call `render`."""

    def __init__(self, columns: Sequence[str]) -> None:
        self._columns = tuple(columns)
        self._rows: list[tuple[str, ...]] = []

    def add_row(self, *cells: object) -> None:
        if len(cells) != len(self._columns):
            raise ValueError(f"expected {len(self._columns)} cells, got {len(cells)}")
        self._rows.append(tuple(str(cell) for cell in cells))

    def render(self, *, align: str = "left") -> str:
        if align not in ALIGNMENTS:
            raise ValueError(f"align must be one of {ALIGNMENTS}, got {align!r}")
        widths = [max(len(row[i]) for row in [self._columns, *self._rows]) for i in range(len(self._columns))]

        def fit(cell: str, width: int) -> str:
            return cell.rjust(width) if align == "right" else cell.ljust(width)

        def line(cells: Sequence[str]) -> str:
            return " | ".join(fit(cell, width) for cell, width in zip(cells, widths, strict=True)).rstrip()

        rule = "-+-".join("-" * width for width in widths)
        return "\n".join([line(self._columns), rule, *(line(row) for row in self._rows)])
