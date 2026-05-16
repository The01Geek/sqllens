# Agent system prompt

How SQL Lens builds the system prompt that goes to Anthropic on every turn — the abstract builder, the default implementation, what content it generates, how it's wired through the agent, and the post-build enhancer hook. Source-of-truth reference for [src/sqllens/agent/core/system_prompt/](../../src/sqllens/agent/core/system_prompt/) and the consumers in [src/sqllens/agent/core/agent/agent.py](../../src/sqllens/agent/core/agent/agent.py).

## The builder interface

`SystemPromptBuilder` ([src/sqllens/agent/core/system_prompt/base.py:15](../../src/sqllens/agent/core/system_prompt/base.py#L15)) is a one-method ABC:

```python
async def build_system_prompt(
    self, user: "User", tools: List["ToolSchema"]
) -> Optional[str]:
```

A builder gets the resolved user and the **list of tools currently exposed to the LLM** for this turn. Returning `None` means "no system prompt this turn" — the agent honors that and skips the prompt entirely.

## Default implementation

`DefaultSystemPromptBuilder` ([src/sqllens/agent/core/system_prompt/default.py:18](../../src/sqllens/agent/core/system_prompt/default.py#L18)) is the only concrete builder. Two modes:

1. **Override mode** — if constructed with `base_prompt="..."`, `build_system_prompt` returns that string verbatim. Tools and user are ignored.
2. **Dynamic mode** — if `base_prompt` is `None` (the default), the builder composes a prompt from a fixed preamble plus optional memory-workflow sections that only appear when the relevant memory tools are present.

### Dynamic-mode preamble

Always present ([src/sqllens/agent/core/system_prompt/default.py:60-67](../../src/sqllens/agent/core/system_prompt/default.py#L60-L67)):

```
You are SQL Lens, an AI data analyst created to help users with data analysis tasks. Today's date is YYYY-MM-DD.

Response Guidelines:
- Any summary of what you did or observations should be the final step.
- Use the available tools to help the user accomplish their goals.
- When you execute a query, that raw result is shown to the user outside of your response so YOU DO NOT need to include it in your response. Focus on summarizing and interpreting the results.
```

Followed by `You have access to the following tools: <comma-separated tool names>` if any tools are exposed.

### Memory sections (conditional)

The builder inspects `tools` for three names and appends sections per match:

| Tool name | Section appended | Source lines |
|---|---|---|
| `search_saved_correct_tool_uses` | "BEFORE executing any tool, you MUST first call search_saved_correct_tool_uses…" | [default.py:84-92](../../src/sqllens/agent/core/system_prompt/default.py#L84-L92) |
| `save_question_tool_args` | "AFTER successfully executing a tool, you MUST call save_question_tool_args…" | [default.py:94-100](../../src/sqllens/agent/core/system_prompt/default.py#L94-L100) |
| `save_text_memory` | "Use text memory to save database schema details, terminology, query patterns…" | [default.py:126-151](../../src/sqllens/agent/core/system_prompt/default.py#L126-L151) |

If any of the first two are present, the builder also appends a short "Example workflow" + carve-outs (`The only exceptions to searching first are: …`).

### What the prompt does NOT say

The default prompt currently has **no guidance** on:

- How to handle tool errors (silent vs. surface to user; verbatim vs. paraphrase)
- How to format final answers (Markdown? plain text? tables?)
- How to behave when the database schema is unknown (the agent currently runs `SELECT name FROM sqlite_master WHERE type='table'` itself, which works but isn't directed)
- How to behave when `max_tool_iterations` is approaching

Item 1 (tool errors) is the one most often felt by users — see "Known rough edges" below.

## How the builder is wired

The `Agent` constructor ([src/sqllens/agent/core/agent/agent.py:90](../../src/sqllens/agent/core/agent/agent.py#L90)) defaults `system_prompt_builder = DefaultSystemPromptBuilder()`:

```python
def __init__(
    self,
    ...
    system_prompt_builder: SystemPromptBuilder = DefaultSystemPromptBuilder(),
    ...
):
    self.system_prompt_builder = system_prompt_builder
```

`build_agent()` in [src/sqllens/agent/factory.py](../../src/sqllens/agent/factory.py) does **not** pass a custom builder, so the default applies. There's currently no config knob to swap or extend the prompt without editing code.

## Per-turn flow

For every user message ([src/sqllens/agent/core/agent/agent.py:595-635](../../src/sqllens/agent/core/agent/agent.py#L595-L635)):

1. The agent enumerates `tool_schemas` available for this turn (after filtering by access groups).
2. `await self.system_prompt_builder.build_system_prompt(user, tool_schemas)` produces a string.
3. If `self.llm_context_enhancer` is set and the prompt isn't `None`, the enhancer can **rewrite** the prompt before send. The default wiring uses `DefaultLlmContextEnhancer(agent_memory)` ([agent.py:127-128](../../src/sqllens/agent/core/agent/agent.py#L127-L128)) which augments the prompt with memory recall context.
4. The (possibly enhanced) prompt is sent as the `system` parameter on the Anthropic request.

The system prompt is rebuilt every turn — it is not cached across turns. This matters: changing `DefaultSystemPromptBuilder` takes effect on the next message without restart, but the same prompt is sent (and re-billed) on every turn.

## Tool error contract

The system prompt is one half of the loop; the other half is how tools surface errors. `Tool.execute()` returns a `ToolResult` ([src/sqllens/agent/core/tool/models.py:47](../../src/sqllens/agent/core/tool/models.py#L47)):

```python
class ToolResult(BaseModel):
    success: bool
    result_for_llm: str           # what the LLM sees
    ui_component: Optional[UiComponent]
    error: Optional[str]
    metadata: Dict[str, Any]
```

When a tool fails, only `result_for_llm` is fed back into the model's context. `success`, `error`, and `metadata` are not surfaced into the conversation. Consequently the LLM cannot distinguish "the database rejected my SQL" from "the tool's own scratch-write failed" — they look the same in the message stream.

`RunSqlTool.execute()` ([src/sqllens/agent/tools/run_sql.py:151-153](../../src/sqllens/agent/tools/run_sql.py#L151-L153)) on exception:

```python
error_message = f"Error executing query: {str(e)}"
return ToolResult(success=False, result_for_llm=error_message, ...)
```

The string `"Error executing query: [WinError 5] Access is denied: '241162b15abe2ec8'"` is what the model sees. Without a directive in the system prompt about how to handle errors, the model paraphrases — often inventing plausible-sounding root causes.

## Known rough edges

### 1. No guidance for tool errors → user-facing confabulation

The biggest gap. With the current prompt, when `run_sql` returns an internal error, the LLM produces text like *"I'm encountering access issues with the database. Could you please check if the database file has the correct permissions…"* — none of which is the actual cause. See [tool-scratch-storage.md](tool-scratch-storage.md) for the recurring example.

Adding a short directive in the preamble — *"When a tool result is an error, surface the verbatim error text to the user. Do not invent or speculate about underlying causes."* — would address the symptom. The proper fix is to give tools a way to flag "internal error, not a query failure" via a structured field, then have the prompt handle the two categories distinctly.

### 2. No configuration seam for prompt customisation

Users who want to inject domain context ("this is a SaaS metrics database; MRR means…") have to fork the code or rely on the `save_text_memory` memory tool — which only takes effect after the first query has been answered. A `[prompt] custom_preamble = "..."` TOML field that flows into `DefaultSystemPromptBuilder(base_prompt=...)` would let users override without code changes. Note that override mode currently replaces the *entire* prompt, including memory sections — a "preamble append" mode would be more useful.

### 3. Memory workflow is normative without escape

The dynamic prompt says `you MUST first call search_saved_correct_tool_uses` and `you MUST call save_question_tool_args`. With caches cold, this doubles the tool-iteration count per turn and contributes to `max_tool_iterations` exhaustion against unfamiliar schemas. The carve-outs ("user is asking about the tools themselves") cover only edge cases. Consider softening MUST → SHOULD, or making the requirement conditional on the user-question type.

### 4. Pruned-but-referenced tool name

Line 88 of `default.py` says *"BEFORE executing any tool (run_sql, **visualize_data**, or calculator)…"* — `visualize_data` and `calculator` were pruned during the extraction ([CLAUDE.md](../../CLAUDE.md) "The pruning choice"). The prompt mentions tools the agent can't call. Harmless but rotten — should be `run_sql` only until those tools come back.
