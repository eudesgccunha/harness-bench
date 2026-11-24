# tinytable changelog

## 2.0.0

- Removed `render_table(headers, rows, justify="left")`.
- Added `Table(columns)` with `Table.add_row(*cells)` and `Table.render(align="left")`.
- `justify` was renamed `align`; it is keyword-only and accepts `"left"` or `"right"`.

## 1.4.0

- `render_table(headers, rows, justify="left")` renders a table from a list of headers and a list of row tuples.
