# Sales report

`data.csv` holds the sales per region of one quarter (`region`, `amount`).

Write two files in this directory:

1. `report.html`: a short, self-contained HTML report. It needs a title (`h1`), a `table` with one row per region
   and its amount, and one sentence that names the region with the highest sales. No external scripts or
   stylesheets, and no links to files that do not exist.
2. `summary.json`: a JSON object with `total` (the sum of all amounts), `rows` (the number of regions) and
   `regions` (the region names, as a list of strings).

Do not edit `data.csv`.
