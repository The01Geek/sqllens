# MySQL Chinook demo

A ready-to-run MySQL 8.0 database loaded with the
[Chinook](https://github.com/lerocha/chinook-database) sample music store
(release v1.4.5), plus a SQL Lens config that connects to it read-only.

You need Docker with Compose (Docker Desktop on Windows and macOS) and an
Anthropic API key.

## Start the database

From the repository root:

```bash
docker compose -f examples/mysql-demo/compose.yaml up -d --wait
```

The first start pulls the `mysql:8.0` image and loads the Chinook data, which
takes a minute or two; `--wait` returns once the database answers queries.
Later starts reuse the loaded data and take a few seconds.

The container publishes MySQL on `127.0.0.1:3306` only, with a demo user
`chinook_reader` (password `chinook_reader`) that can read the `Chinook`
database and nothing else. These credentials are for the demo only.

## Run SQL Lens against it

```bash
pip install sqllens            # or, from a checkout: pip install -e ".[dev,all]"
export SQLLENS_LLM__API_KEY=sk-ant-...
sqllens validate --check-db -c examples/mysql-demo/sqllens.toml
sqllens serve -c examples/mysql-demo/sqllens.toml
```

Then connect your MCP client to the resulting stdio process. Sample questions
that should work without prior training:

- "How many albums did AC/DC release?" (expected answer: 2)
- "Which 5 customers spent the most money?"
- "What's the most popular genre by track count?"

The first question wires up the agent and creates
`./examples/mysql-demo/chroma/` for ChromaDB memory; later questions reuse
both.

## Stop it

```bash
docker compose -f examples/mysql-demo/compose.yaml down       # keep the loaded data
docker compose -f examples/mysql-demo/compose.yaml down -v    # also delete the data volume
```

## Port 3306 already in use

If something else on your machine listens on port 3306, change the host side
of the port mapping in `compose.yaml`, for example to 3307:

```yaml
    ports:
      - "127.0.0.1:3307:3306"
```

and make the same change to the URL in `sqllens.toml`:

```toml
url = "mysql+pymysql://chinook_reader:chinook_reader@127.0.0.1:3307/Chinook"
```
