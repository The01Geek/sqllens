Self-driving MCP App widget at `ui://sqllens/memory-admin.html` that
renders the memory-administration surface (Browse / Import / Stats /
Danger zone) and drives the seven backing tools (`list_memories`,
`get_memory`, `delete_memory`, `clear_memories`, `add_memories`,
`export_memories`, `get_memory_stats`) directly via the App SDK's
`callServerTool`. Registered only inside `cfg.memory.allow_admin_tools`,
so a host never advertises a widget whose tools are off. Topology-
agnostic: the widget makes no direct network call to the server
(no `fetch`, `XHR`, `WebSocket`, or remote `<script src>`); every
interaction is brokered by the host as a `tools/call`, so the widget
renders identically whether the host reaches the server directly or
through a proxy. Partial `add_memories` and lossy `export_memories`
render as explicit errors per the `isError` contract; destructive
actions are confirm-gated and surface the server's write-auth
refusal. Dev preview shim at `scripts/memory_admin_preview.html`.
(#188)
