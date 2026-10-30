`query_database` table blocks now carry an additive `column_labels`
list of humanized display headers (`_humanize_label` in `_format.py`):
a lowercase snake_case identifier such as `customer_id` becomes
"Customer ID" and `total_amount_usd` becomes "Total Amount USD",
while any already reader-friendly name (a SQL alias, `count(*)`, …) is
passed through unchanged. The raw `columns` keys are untouched, so the
widget's typed sort, the `column_types` lookup, and CSV export keep
matching on the original names. Both the interactive table widget and
the Markdown fallback (and the one-row small-result text path) render
the humanized header, falling back to the raw name when the field is
absent. Chart axis titles are defaulted the same way server-side
(`_compute_chart_payload` → `_default_axis_label`): an axis whose field
is snake_case and whose label the LLM left empty shows the humanized
field, while a label the LLM set on purpose is preserved. Chart legend
and series names are unchanged. (#259)
