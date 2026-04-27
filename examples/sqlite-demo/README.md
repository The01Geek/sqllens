# SQLite Chinook demo

The smallest working example — a sample SQLite music store database
([Chinook](https://github.com/lerocha/chinook-database)) and a config that
points SQL Lens at it.

```bash
cd /path/to/sqllens
pip install -e ".[dev,all]"
export SQLLENS_LLM__API_KEY=sk-ant-...
sqllens serve -c examples/sqlite-demo/sqllens.toml
```

Then connect your MCP client to the resulting stdio process. Sample questions
that should work without prior training:

- "How many albums did AC/DC release?"
- "Which 5 customers spent the most money?"
- "What's the most popular genre by track count?"

The first call wires up the agent (cold-start latency) and creates a
`./examples/sqlite-demo/chroma/` directory for ChromaDB memory. Subsequent
calls reuse both.
