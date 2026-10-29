Agent looped on tool calls cut off by the output-token cap until
`max_tool_iterations` (#247). A `tool_use` block in a `max_tokens` response is
now marked `truncated` and refused by `ToolRegistry.execute` with an
explanatory error instead of running; missing tool input is parsed as `{}`
instead of the `{"_raw": None}` placeholder the model kept copying; and the
agent stops after 3 consecutive rounds of invalid or truncated tool calls,
reported as an error.
