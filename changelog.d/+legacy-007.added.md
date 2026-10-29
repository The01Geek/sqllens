Config schema (additive, backward-compatible — no behavior change yet):
`config_version` (top-level, default `1`, accepted-but-ignored —
reserved so a future schema migration can branch on it),
`server.log_level`, `agent.show_sql`, and an `[agent.audit]` section
(`enabled`/`log_level`/`include_response_text`/`sanitize_parameters`),
plus a `DatabaseConfig.dialect` helper property. `0.1.0` is the first
stable config schema: `config_version` defaults to `1` and is currently
ignored. Because the top-level config sets `extra="forbid"`, introducing
a new **required** field in a future schema version would break existing
TOMLs, so the compatibility contract is additive-with-defaults only. (#109)
