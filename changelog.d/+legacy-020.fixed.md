The `query_database` result widget painted text blocks as raw Markdown
source (`#`, `**`, `-` visible in the host). Text blocks are now rendered
with a vendored markdown-it 14.3.2 (`src/sqllens/ui/vendor/markdown-it.min.js`,
inlined like the other bundles) configured with `html: false` (raw HTML
escaped), its default link validation (`javascript:` / `vbscript:` / `file:`
/ `data:` targets stay literal) and image syntax disabled (no remote
fetches), so agent output cannot inject markup into the widget.
