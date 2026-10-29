The opt-in `import_memory` MCP tool now checks write authorization before
parsing or importing a bundle, matching the other memory-writing tools.
Calls require bearer auth or the explicit closed-network `auth.insecure`
opt-in; CLI import/export is unaffected. (#237, #261)
