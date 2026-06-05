# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Utilities for converting agent UI components into MCP-friendly output.

The agent yields a stream of ``UiComponent`` objects (status cards, text,
dataframes, charts, etc.). MCP tools must return a single string. This module
collapses that stream:

- :func:`components_to_blocks` walks the stream once and emits an ordered list
  of typed ``blocks`` (text / table / chart) — the single structured-data
  channel that apps-aware hosts render in order via ``_meta["sqllens/blocks"]``
  and the self-contained widget renders per-block.
- :func:`components_to_markdown` is the same walker, but the resulting block
  list is serialized to a plain Markdown answer for non-apps clients.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from sqllens.agent.core.components import UiComponent
from sqllens.agent.core.rich_component import ComponentType
from sqllens.safety import first_sql_keyword, is_introspection_query

logger = logging.getLogger("sqllens.tools._format")

# DatabaseConfig.max_rows bounds DataFrame size before it reaches this renderer;
# this cap only protects the MCP client from rendering a multi-thousand-row
# Markdown table when max_rows is raised above the rendering budget.
_MAX_ROWS_RENDERED = 500

# Serialized-size budget for one structured table block. The host pushes the
# whole CallToolResult into a sandboxed iframe; a multi-MB ``_meta`` blob is
# the only thing that actually breaks rendering, so size — not row count — is
# the cap. Measured against ``json.dumps(payload, separators=(",", ":"))``.
_MAX_TABLE_PAYLOAD_BYTES = 130 * 1024

# Same budget, same reason, for one chart block. Aliased to the table budget so
# the two cannot drift apart — both blobs share one sandboxed-iframe rendering
# ceiling.
_MAX_CHART_PAYLOAD_BYTES = _MAX_TABLE_PAYLOAD_BYTES

# Same budget, same reason, for the agent-trace ``_meta["sqllens/agent_trace"]``
# blob. The trace embeds raw tool arguments (SQL, memory-search text) per step,
# so it can grow large on a long run — and it rides the same sandboxed-iframe
# ceiling. Aliased to the table budget so all three _meta blobs share one cap.
_MAX_TRACE_PAYLOAD_BYTES = _MAX_TABLE_PAYLOAD_BYTES

# Overall ceiling across the whole ``sqllens/blocks`` array. The per-block cap
# above bounds each individual block; this caps the *sum* so a long stream of
# in-budget blocks still cannot blow the iframe ceiling. Set well above the
# per-block cap so a handful of full-budget blocks fit comfortably.
_MAX_BLOCKS_TOTAL_BYTES = 512 * 1024


def _query_info_from_sql(sql: str, row_count: int | None) -> dict:
    info: dict = {"sql": sql, "query_type": first_sql_keyword(sql)}
    if row_count is not None:
        info["row_count"] = row_count
    return info


# The agent emits one of these placeholders on a normal turn's finalization
# ChatInputUpdateComponent (agent/core/agent/agent.py emits "Ask a question...",
# "Ask a follow-up question...", "Continue the task or ask me something else...",
# and "Try again..." on the error path). None is a clarifying question, so they
# must never be surfaced as the answer when a turn produces no TEXT/DATAFRAME —
# otherwise an empty result would render the generic placeholder instead of the
# "(no answer)" fallback. Compared case-insensitively.
_GENERIC_INPUT_PLACEHOLDERS = frozenset(
    {
        "ask a question...",
        "ask a follow-up question...",
        "continue the task or ask me something else...",
        "try again...",
    }
)


def _button_label(data: object) -> str:
    if isinstance(data, dict):
        label = data.get("label")
        if isinstance(label, str):
            return label.strip()
    return ""


def _component_field(rich, name: str) -> object:  # type: ignore[no-untyped-def]
    # Read a field whether the component declared it as a top-level attribute
    # (NotificationComponent.message/title/level) or only carries it in the
    # generic RichComponent.data dict. ALERT has no first-party component class
    # in this pruned tree, so an emitted ALERT is a bare RichComponent whose
    # text lives in `data` (pydantic drops unknown top-level kwargs); reading
    # only attributes would silently render it empty.
    val = getattr(rich, name, None)
    if val is not None:
        return val
    data = getattr(rich, "data", None)
    return data.get(name) if isinstance(data, dict) else None


def _alert_text(rich) -> str:  # type: ignore[no-untyped-def]
    # An error-level notification carries the raw, unsanitized driver exception
    # (agent run_sql failure path). Surfacing it here would leak it as a normal
    # is_error=False answer, bypassing the sanitized error taxonomy — so an
    # error-level affordance is treated as "not an answer".
    level = _component_field(rich, "level")
    if isinstance(level, str) and level.strip().lower() == "error":
        return ""
    message = ""
    for attr in ("message", "content", "description"):
        val = _component_field(rich, attr)
        if isinstance(val, str) and val.strip():
            message = val.strip()
            break
    title = _component_field(rich, "title")
    if isinstance(title, str) and title.strip() and title.strip() != message:
        return f"**{title.strip()}**: {message}" if message else title.strip()
    return message


def render_interactive(components: Iterable[UiComponent]) -> str:
    """Render the agent's interactive/follow-up affordances as plain Markdown.

    Surfaces a clarifying question the agent expressed *only* as an interactive
    component — a ``CHAT_INPUT_UPDATE`` prompt, ``BUTTON``/``BUTTON_GROUP``
    choices, or an ``ALERT``/``NOTIFICATION`` message — so the calling model
    receives the question instead of a useless ``"(no answer)"``. The output is
    plain Markdown, independent of the MCP Apps widget channel, so non-apps
    clients get the question too.

    Returns ``""`` when no renderable interactive affordance is present, which
    the callers treat as "fall back to ``(no answer)``". Used only when a turn
    produced no ``TEXT``/``DATAFRAME`` answer, so a normal answer's trailing
    finalization components are never surfaced.
    """
    pieces: list[str] = []
    choices: list[str] = []
    for comp in components:
        rich = comp.rich_component
        if rich is None:
            continue
        ctype = getattr(rich, "type", None)
        if ctype == ComponentType.CHAT_INPUT_UPDATE:
            prompt = (getattr(rich, "placeholder", "") or "").strip()
            if prompt and prompt.lower() not in _GENERIC_INPUT_PLACEHOLDERS:
                pieces.append(prompt)
        elif ctype == ComponentType.BUTTON:
            label = _button_label(getattr(rich, "data", None))
            if label:
                choices.append(label)
        elif ctype == ComponentType.BUTTON_GROUP:
            data = getattr(rich, "data", None)
            buttons = data.get("buttons", []) if isinstance(data, dict) else []
            if isinstance(buttons, list):
                for button in buttons:
                    label = _button_label(button)
                    if label:
                        choices.append(label)
        elif ctype in (ComponentType.ALERT, ComponentType.NOTIFICATION):
            text = _alert_text(rich)
            if text:
                pieces.append(text)

    if choices:
        enumerated = "\n".join(f"- {choice}" for choice in choices)
        pieces.append(f"Please choose one of the following:\n\n{enumerated}")
    return "\n\n".join(pieces)


def _text_is_answer_marked(rich) -> bool:  # type: ignore[no-untyped-def]
    """True iff this TEXT component is deliberate prose, not reasoning chatter.

    The agent emits two kinds of TEXT: intermediate reasoning that accompanies
    a tool call (``agent/core/agent/agent.py``: ``RichTextComponent(content=
    response.content, ...)`` inside the LLM loop, gated on
    ``UI_FEATURE_SHOW_TOOL_INVOCATION_MESSAGE_IN_CHAT``) and the terminal
    answer / iteration-limit warning at end-of-turn — both of which set
    ``data["is_answer"] = True``. The ``EmitTextTool`` sets the same flag on
    deliberate interleaved prose. So a marked TEXT is something the user should
    see; an unmarked TEXT is reasoning chatter.
    """
    data = getattr(rich, "data", None)
    return isinstance(data, dict) and bool(data.get("is_answer"))


def components_to_blocks(
    components: Iterable[UiComponent],
) -> tuple[str, bool, list[dict], dict | None, dict | None]:
    """Collapse a stream into ``(markdown, is_error, blocks, query_info, memory_info)``.

    The single source of truth behind the consolidated ``query_database`` tool
    and the narrower :func:`components_to_markdown` view. One ordered pass over
    the stream:

    - Every ``DATAFRAME`` becomes a ``{"type": "table", ...table payload...}``
      block at its stream position.
    - Every ``CHART`` becomes a ``{"type": "chart", ...chart payload...}``
      block at its stream position.
    - Every **answer-marked** ``TEXT`` (deliberate prose: ``EmitTextTool``
      output and the agent's terminal answer / iteration-limit warning) becomes
      a ``{"type": "text", "text": ...}`` block at its stream position.
      Intermediate-reasoning ``TEXT`` (the assistant prose that accompanies a
      tool call when ``UI_FEATURE_SHOW_TOOL_INVOCATION_MESSAGE_IN_CHAT`` is on)
      is excluded.
    - As a backwards-compat fallback, if *no* answer-marked TEXT was seen but
      at least one non-empty TEXT exists, the **last** such TEXT is included
      as a text block (preserves last-wins behavior for unmarked streams).

    Control-flow invariants preserved from the prior single-artifact collapse:

    - **Error short-circuit.** A terminal ``STATUS_CARD`` with ``status="error"``
      returns the error path immediately — ``blocks`` is empty and the markdown
      is the error message.
    - **Later-success-supersedes-earlier-failure.** A later *data* ``run_sql``
      success card (one carrying ``metadata["sql"]`` and not an
      ``is_introspection_query``) clears an earlier error within the same turn,
      so the recovered answer reaches the caller.
    - **Last-wins for executed SQL and memory hit/miss.** ``query_info`` is
      built from the *last* ``run_sql`` STATUS_CARD's ``metadata["sql"]``;
      ``memory_info`` is built from the *last* ``search_saved_correct_tool_uses``
      STATUS_CARD's ``metadata["memory_search"]``. The cards stream twice per
      tool call (running → completed) with identical metadata; last-wins
      de-dupes them idempotently.

    Size budgets (CLAUDE.md: never silently drop):

    - Each table / chart block is bounded by ``_MAX_TABLE_PAYLOAD_BYTES`` /
      ``_MAX_CHART_PAYLOAD_BYTES`` via the existing binary search in
      :func:`_compute_table_payload` / :func:`_compute_chart_payload` — the
      block's ``row_count`` is the kept prefix, ``truncated`` the dropped tail.
    - The whole ``blocks`` array is bounded by ``_MAX_BLOCKS_TOTAL_BYTES``;
      :func:`_trim_blocks_to_budget` keeps the largest prefix that fits and
      appends an explicit truncation-notice text block. A server-side
      ``logger.warning`` fires whenever any trimming happens.

    The returned ``markdown`` is :func:`_serialize_blocks_to_markdown` over the
    (possibly trimmed) blocks — keeping the plain-Markdown answer non-apps
    clients depend on stable in spirit (tables rendered as Markdown tables,
    text blocks verbatim, charts as a short ``_[chart: …]_`` placeholder).
    When no blocks are produced, :func:`render_interactive` is consulted for an
    interactive-affordance fallback before settling on ``"(no answer)"``.
    """
    # Materialize once: the second pass (block emission) needs random access
    # and ``render_interactive`` does another pass; the public signature accepts
    # any Iterable (incl. generators).
    components = list(components)

    # ── First pass: error state, executed SQL, memory hit/miss, text indices.
    error_message = ""
    last_sql: str | None = None
    last_memory: dict | None = None
    marked_text_indices: set[int] = set()
    last_text_idx: int | None = None

    for idx, comp in enumerate(components):
        rich = comp.rich_component
        if rich is None:
            continue
        ctype = getattr(rich, "type", None)

        if ctype == ComponentType.TEXT:
            content = (getattr(rich, "content", "") or "").strip()
            if not content:
                continue
            if _text_is_answer_marked(rich):
                marked_text_indices.add(idx)
            last_text_idx = idx
        elif ctype == ComponentType.STATUS_CARD:
            status = getattr(rich, "status", "")
            metadata = getattr(rich, "metadata", None)
            sql = metadata.get("sql") if isinstance(metadata, dict) else None
            is_run_sql = isinstance(sql, str) and bool(sql.strip())
            if status == "error":
                error_message = (
                    getattr(rich, "description", "") or "Agent reported an error"
                )
            elif (
                status == "success"
                and is_run_sql
                and not is_introspection_query(sql)
            ):
                # See the docstring's "later-success-supersedes-earlier-failure"
                # invariant: a successful *data* run_sql clears an earlier error
                # within the same turn (the agent re-issued a corrected query).
                # Scoped two ways so it cannot mask a real failure: (1) run_sql
                # only (carries metadata["sql"]) — a successful memory-search or
                # chart card never clears, and a memory search runs before the
                # query on most turns; (2) NOT a schema-introspection read — the
                # agent runs information_schema / SHOW lookups to confirm a
                # column before retrying, so an introspection success is a step
                # toward an answer, not the answer. A genuinely failing final
                # data run_sql (no later data run_sql success) still surfaces as
                # an error.
                if error_message:
                    logger.info(
                        "run_sql error superseded by a later successful run_sql "
                        "in the same turn; surfacing the recovered answer"
                    )
                    error_message = ""
            if is_run_sql:
                last_sql = sql
            if isinstance(metadata, dict):
                memory_search = metadata.get("memory_search")
                if isinstance(memory_search, dict):
                    last_memory = memory_search

    if error_message:
        return error_message, True, [], None, None

    # Determine which TEXT indices to emit as text blocks. Prefer the explicit
    # answer-marker (set by EmitTextTool and the agent's terminal answer); if
    # the stream carries no marker (e.g. tests using bare RichTextComponent),
    # fall back to the very last non-empty TEXT — the same "last TEXT wins"
    # semantics the previous collapse used as the terminal-answer heuristic.
    text_include: set[int]
    if marked_text_indices:
        text_include = marked_text_indices
    elif last_text_idx is not None:
        text_include = {last_text_idx}
    else:
        text_include = set()

    # ── Second pass: emit blocks in stream order.
    blocks: list[dict] = []
    for idx, comp in enumerate(components):
        rich = comp.rich_component
        if rich is None:
            continue
        ctype = getattr(rich, "type", None)
        if ctype == ComponentType.TEXT and idx in text_include:
            content = (getattr(rich, "content", "") or "").strip()
            if content:
                blocks.append({"type": "text", "text": content})
        elif ctype == ComponentType.DATAFRAME:
            payload = _build_table_payload(rich)
            if payload is not None:
                blocks.append({"type": "table", **payload})
        elif ctype == ComponentType.CHART:
            payload = _build_chart_payload(rich)
            if payload is not None:
                blocks.append({"type": "chart", **payload})

    blocks = _trim_blocks_to_budget(blocks)

    # Markdown answer: serialize the blocks; fall back to the interactive-only
    # surface (clarifying question, button choices) before the literal
    # ``"(no answer)"`` so a turn that produced only an interactive affordance
    # still surfaces the question to non-apps clients.
    if blocks:
        markdown = _serialize_blocks_to_markdown(blocks)
        if not markdown:
            markdown = render_interactive(components) or "(no answer)"
    else:
        markdown = render_interactive(components) or "(no answer)"

    query_info = None
    if last_sql is not None:
        # True result size, not the rendered subset: the *last* table block
        # corresponds to the executed SQL whose result reached the renderer.
        # Per-block size cap may have dropped a tail (row_count is the kept
        # prefix, truncated the dropped tail) but the SQL ran against the
        # whole set. ``.get`` keeps a partial future payload from raising an
        # unsanitized KeyError past query_database's except blocks (which
        # sanitize driver-exception strings into a stable internal-error
        # message). If the trailing table block was itself trimmed away by the
        # overall budget cut, ``row_count`` is None — the SQL was still run.
        last_table = next(
            (b for b in reversed(blocks) if b.get("type") == "table"), None
        )
        row_count: int | None
        if last_table is not None:
            row_count = last_table.get("row_count", 0) + last_table.get("truncated", 0)
        else:
            row_count = None
        query_info = _query_info_from_sql(last_sql, row_count)
    return markdown, False, blocks, query_info, last_memory


def components_to_markdown(
    components: Iterable[UiComponent],
) -> tuple[str, bool]:
    """Collapse a stream of components into ``(markdown, is_error)``.

    Thin view over :func:`components_to_blocks` that drops the structured
    payloads; returns the same ``(markdown, is_error)`` pair non-apps hosts
    already depend on (the Markdown branch is unchanged in spirit — pinned by
    ``tests/unit/test_format.py``).
    """
    markdown, is_error, _blocks, _qi, _mi = components_to_blocks(components)
    return markdown, is_error


# Title of the top-level error card the vendored ``send_message`` emits when the
# whole turn throws (an LLM/infra exception it caught and logged server-side).
# Its description is deliberately generic — the real exception never reaches the
# component stream — so the trace can only surface this generic terminal reason;
# the underlying error lives in the server logs, never in ``_meta``.
_AGENT_ERROR_CARD_TITLE = "Error Processing Message"
# The agent's per-tool-call lifecycle STATUS_CARD carries the invoked tool name
# in its title as ``Executing {tool}`` (agent/core/agent/agent.py). These cards
# are only emitted when ``UI_FEATURE_SHOW_TOOL_ARGUMENTS`` is unlocked — which
# is exactly what ``agent.show_details`` does for the single static user group —
# so a trace is only ever assembled for a deployment that already opted into the
# SQL-leaking debugging surface. The running card (status="running") carries the
# call ``arguments`` in ``metadata``; the completed card shares the same
# component ``id`` (``set_status`` copies it) and flips ``status`` to
# "success"/"error", with a ``Tool failed: ...`` description on failure.
_TOOL_CARD_PREFIX = "Executing "
# Prefix the agent puts on a failed tool's completion description. Stripped so
# the trace's per-step ``error`` is the tool's own message, not the boilerplate.
_TOOL_FAILED_PREFIX = "Tool failed: "
# Message on the ``STATUS_BAR_UPDATE`` (status="warning") the agent emits when it
# stops because it exhausted ``max_tool_iterations``. Matched as the max-iteration
# terminal signal — more accurate than counting steps, because the agent's
# iteration counter counts LLM *rounds* (a single round can fire several parallel
# tool calls), so a step count can't be compared to the cap directly.
_ITERATION_LIMIT_MESSAGE = "Tool limit reached"


def _parse_component_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _step_duration_ms(start: object, end: object) -> int | None:
    start_dt = _parse_component_ts(start)
    end_dt = _parse_component_ts(end)
    if start_dt is None or end_dt is None:
        return None
    try:
        # A naive/aware mismatch (one timestamp carried an offset, the other did
        # not) raises TypeError here. The agent stamps both with naive
        # utcnow().isoformat() today, so this is defensive — but degrade just
        # this step's duration to None rather than letting it bubble to the
        # outer best-effort guard and void the whole trace.
        delta_ms = (end_dt - start_dt).total_seconds() * 1000.0
    except TypeError:
        return None
    # Clock skew / out-of-order timestamps must never yield a negative duration.
    return int(delta_ms) if delta_ms >= 0 else 0


def build_agent_trace(
    components: Iterable[UiComponent],
    *,
    total_duration_ms: int | None,
    max_iterations: int,
) -> dict:
    """Assemble a step-by-step trace of the agent loop from its component stream.

    Gated by the caller on ``agent.show_details`` (see
    :func:`~sqllens.tools.query_database.query_database_impl_with_widgets`); this
    function does no gating itself. It is a single forward pass over the buffered
    ``UiComponent`` stream — it never re-runs or re-instruments the agent.

    Each invoked tool surfaces as a pair of ``STATUS_CARD`` components sharing
    one component ``id``: a ``running`` card carrying the call ``arguments`` in
    its ``metadata``, then a ``success``/``error`` completion. The pair is folded
    into one step ``{index, tool, arguments, status, duration_ms, error?}``:
    ``status`` is ``"ok"`` on success, ``"error"`` on failure, ``"incomplete"``
    if the run ended before the completion card was emitted; ``duration_ms`` is
    the wall-clock between the two cards' framework timestamps (``None`` if a
    timestamp is unparseable; clamped to ``0`` on clock skew / out-of-order
    timestamps); ``error`` is present only on a failed step and is the tool's
    own message (the ``Tool failed: `` boilerplate stripped).

    ``terminal_error`` is the run-ending reason derived from the same stream,
    in precedence order:

    1. the generic top-level error card (an LLM/infra exception the agent caught
       and logged server-side — the real text is *not* in the stream);
    2. else the last failed tool step (a tool failure / DB timeout — its real
       message *is* in the stream, via the completion card description);
    3. else, when the agent emitted its ``max_tool_iterations`` warning, the
       max-iteration stop (detected from the warning ``STATUS_BAR_UPDATE``, not
       a step count — the agent counts LLM rounds, and one round can fire
       several parallel tool calls, so a step count is not the iteration count);
    4. else, when the last step is ``"incomplete"`` (the run ended mid tool
       call), a note that the run ended in progress — so a truncated run is not
       silently reported as a clean finish;
    5. else ``None`` — a clean run.

    Over-budget traces are size-capped by :func:`_cap_trace_size`, which may add
    ``arguments_truncated``/``steps_truncated`` flags and drop per-step
    ``arguments`` or trailing steps to fit the ``_meta`` iframe ceiling.

    ``iterations`` is the number of tool steps seen; ``total_duration_ms`` is the
    caller-measured wall-clock of the whole turn (``None`` if unmeasured).
    """
    steps: list[dict] = []
    by_id: dict[str, dict] = {}
    agent_error: str | None = None
    hit_iteration_limit = False

    for comp in components:
        rich = comp.rich_component
        if rich is None:
            continue
        ctype = getattr(rich, "type", None)
        if ctype == ComponentType.STATUS_BAR_UPDATE:
            if (
                getattr(rich, "status", "") == "warning"
                and getattr(rich, "message", "") == _ITERATION_LIMIT_MESSAGE
            ):
                hit_iteration_limit = True
            continue
        if ctype != ComponentType.STATUS_CARD:
            continue
        title = getattr(rich, "title", "") or ""
        status = getattr(rich, "status", "") or ""
        description = getattr(rich, "description", "") or ""

        if title == _AGENT_ERROR_CARD_TITLE and status == "error":
            # Generic top-level failure (real exception is server-side only). The
            # vendored card appends "\n\nConversation ID: <id>" to its
            # description (agent/core/agent/agent.py) — strip that tail so
            # terminal_error is the human reason only; the id already rides the
            # dedicated sqllens/conversation channel.
            reason = description.split("\n\nConversation ID:", 1)[0].strip()
            agent_error = reason or "agent reported an unexpected error"
            continue
        if not title.startswith(_TOOL_CARD_PREFIX):
            continue

        card_id = getattr(rich, "id", None)
        tool = title[len(_TOOL_CARD_PREFIX):]
        timestamp = getattr(rich, "timestamp", None)
        metadata = getattr(rich, "metadata", None)
        arguments = metadata if isinstance(metadata, dict) else {}

        step = by_id.get(card_id) if isinstance(card_id, str) else None
        if step is None:
            # First card for this tool call — the ``running`` open.
            step = {
                "index": len(steps),
                "tool": tool,
                "arguments": arguments,
                "status": "incomplete",
                "duration_ms": None,
                "_start": timestamp,
                "_error": None,
            }
            steps.append(step)
            if isinstance(card_id, str):
                by_id[card_id] = step
            continue

        # A later card for the same call — the completion. Record the outcome
        # and the wall-clock between the two cards' timestamps.
        if status == "success":
            step["status"] = "ok"
        elif status == "error":
            step["status"] = "error"
            msg = description.strip()
            if msg.startswith(_TOOL_FAILED_PREFIX):
                msg = msg[len(_TOOL_FAILED_PREFIX):].strip()
            step["_error"] = msg or "tool failed"
        step["duration_ms"] = _step_duration_ms(step["_start"], timestamp)

    last_failed = next(
        (s for s in reversed(steps) if s["status"] == "error"), None
    )
    # ``agent_error`` and the per-step error text below are agent-authored card
    # descriptions, surfaced verbatim — the same #91 decision the
    # ``_SQL_EXECUTION_ERROR_PREFIX`` path in query_database.py already applies:
    # the agent's structured error is actionable detail the caller needs, not an
    # infra-string leak, so it is deliberately not scrubbed here either.
    if agent_error is not None:
        terminal_error: str | None = agent_error
    elif last_failed is not None:
        terminal_error = f"tool '{last_failed['tool']}' failed: {last_failed['_error']}"
    elif hit_iteration_limit:
        terminal_error = (
            f"reached the max_tool_iterations limit ({max_iterations}); "
            "the agent stopped before completing the task"
        )
    elif steps and steps[-1]["status"] == "incomplete":
        # No error card, no failed step, no limit warning — but the last tool
        # call never completed. The run ended mid-step; reporting None here would
        # read as a clean finish, hiding the truncation.
        terminal_error = (
            f"run ended with tool '{steps[-1]['tool']}' still in progress "
            "(no completion was emitted)"
        )
    else:
        terminal_error = None

    public_steps = []
    for step in steps:
        public: dict = {
            "index": step["index"],
            "tool": step["tool"],
            "arguments": step["arguments"],
            "status": step["status"],
            "duration_ms": step["duration_ms"],
        }
        if step["status"] == "error" and step["_error"]:
            public["error"] = step["_error"]
        public_steps.append(public)

    return _cap_trace_size(
        {
            "iterations": len(public_steps),
            "max_iterations": max_iterations,
            "total_duration_ms": total_duration_ms,
            "steps": public_steps,
            "terminal_error": terminal_error,
        }
    )


def _cap_trace_size(trace: dict) -> dict:
    """Bound the serialized trace to the iframe ``_meta`` budget (issue #180).

    The trace rides the same ``CallToolResult._meta`` blob as the table/chart
    payloads, which the host pushes into a sandboxed iframe — a multi-MB blob is
    what actually breaks rendering (see ``_MAX_TABLE_PAYLOAD_BYTES``). Per-step
    ``arguments`` (raw SQL, memory-search text) are the unbounded part, so when
    the trace is over budget we drop them first (flagging ``arguments_truncated``
    and stripping ``arguments`` from each step), then, only if still over, keep
    the largest contiguous step prefix that fits (recording ``steps_truncated``).
    A loud flag beats a silently-broken widget on the debugging path.
    """
    if _serialized_len(trace) <= _MAX_TRACE_PAYLOAD_BYTES:
        return trace

    for step in trace["steps"]:
        step.pop("arguments", None)
    trace["arguments_truncated"] = True
    if _serialized_len(trace) <= _MAX_TRACE_PAYLOAD_BYTES:
        return trace

    full_steps = trace["steps"]
    total = len(full_steps)
    trace["steps"] = []
    if _serialized_len(trace) > _MAX_TRACE_PAYLOAD_BYTES:
        # Even the step-stripped trace busts the budget — nothing more to drop.
        trace["steps_truncated"] = total
        return trace

    lo, hi = 0, total
    while lo < hi:
        mid = (lo + hi + 1) // 2
        trace["steps"] = full_steps[:mid]
        if _serialized_len(trace) <= _MAX_TRACE_PAYLOAD_BYTES:
            lo = mid
        else:
            hi = mid - 1
    trace["steps"] = full_steps[:lo]
    trace["steps_truncated"] = total - lo
    return trace


def append_conversation_footer(markdown: str, conversation_id: str | None) -> str:
    """Append the conversation id as a plain-Markdown footer (text fallback).

    The structured ``_meta`` channel is the source of truth for apps-aware
    hosts; this footer is how a non-apps client learns the id it must pass back
    as the ``conversation_id`` argument to continue the conversation. A falsy
    ``conversation_id`` returns ``markdown`` unchanged.
    """
    if not conversation_id:
        return markdown
    return (
        f"{markdown}\n\n_Conversation ID: `{conversation_id}` — pass it back as the "
        f"`conversation_id` argument to continue this conversation._"
    )


def _coerce_cell(value: object) -> str:
    # Coercion contract shared by the widget payload and the Markdown table
    # (None->"None", Decimal("1.50")->"1.50", datetime->"2026-01-02 03:04:05").
    return str(value)


# Cell strings the widget treats as "no value" — excluded from numeric sniffing
# so an all-NULL or partially-empty column still types correctly on its real
# values. Mirrors how `_coerce_cell` stringifies SQL NULLs.
_EMPTY_CELLS = frozenset({"", "None", "none", "null", "NULL", "NaN", "nan"})


def _looks_numeric(text: str) -> bool:
    # A cell counts as numeric only if it parses to a *finite* float. Bare
    # "inf"/"nan" parse via float() but must not type a column "number" (the
    # widget right-aligns and sorts numerically on that flag).
    try:
        return math.isfinite(float(text))
    except (ValueError, OverflowError):
        return False


def _infer_column_types(
    columns: list[str], coerced_rows: list[list[str]]
) -> dict[str, str]:
    # The vendored DataFrameComponent producers never populate `column_types`
    # (`from_records` hard-codes `{}`), so without this every column would sort
    # lexicographically in the widget. Sniff each column from its coerced cell
    # values: a column whose every non-empty cell parses as a finite number is
    # typed "number"; everything else is left untyped (widget → string sort).
    inferred: dict[str, str] = {}
    for ci, col in enumerate(columns):
        seen_value = False
        all_numeric = True
        for row in coerced_rows:
            cell = row[ci] if ci < len(row) else ""
            if cell in _EMPTY_CELLS:
                continue
            seen_value = True
            if not _looks_numeric(cell):
                all_numeric = False
                break
        if seen_value and all_numeric:
            inferred[col] = "number"
    return inferred


def _safe_column_types(rich) -> dict[str, str]:  # type: ignore[no-untyped-def]
    # Explicit `column_types` is a non-essential hint. A producer handing back a
    # non-mapping (or one whose items() raises) must degrade to "no explicit
    # types" — never take down the whole widget payload via the broad handler
    # in `_build_table_payload`.
    try:
        raw_types = getattr(rich, "column_types", {}) or {}
        return {_coerce_cell(k): _coerce_cell(v) for k, v in dict(raw_types).items()}
    except Exception:
        logger.warning(
            "column_types on DataFrame component was not a usable mapping; "
            "falling back to inferred types only",
            exc_info=True,
        )
        return {}


def _columns_and_rows(rich) -> tuple[list[str], list[dict]]:  # type: ignore[no-untyped-def]
    columns: list[str] = list(getattr(rich, "columns", []) or [])
    rows: list[dict] = list(getattr(rich, "rows", []) or [])
    if not columns and rows:
        columns = list(rows[0].keys())
    return columns, rows


def _build_table_payload(rich) -> dict | None:  # type: ignore[no-untyped-def]
    # The widget is best-effort: if anything in payload construction raises
    # (a pathological column object whose __str__ throws, a json.dumps edge),
    # degrade to "no widget" and let the Markdown answer stand, rather than
    # letting the exception escape *after* query_database_impl_with_widgets's
    # except blocks and bypass the sanitized error taxonomy.
    try:
        return _compute_table_payload(rich)
    except Exception:
        n_cols = len(getattr(rich, "columns", []) or [])
        n_rows = len(getattr(rich, "rows", []) or [])
        logger.warning(
            "table payload construction failed; serving Markdown only "
            "(columns=%d, rows=%d)",
            n_cols,
            n_rows,
            exc_info=True,
        )
        return None


def _compute_table_payload(rich) -> dict | None:  # type: ignore[no-untyped-def]
    # Returns the table payload WITHOUT the ``"type": "table"`` discriminator —
    # ``components_to_blocks`` wraps it with the discriminator before appending
    # to the blocks list. This keeps the size-budget binary search ignorant of
    # the discriminator while still bounding the per-block size to
    # ``_MAX_TABLE_PAYLOAD_BYTES`` (the discriminator key adds < 20 bytes).
    columns, rows = _columns_and_rows(rich)
    if not columns and not rows:
        return None

    # Stringify column labels and column_types too, not just cells, so a non-str
    # label or type value cannot make json.dumps raise inside _serialized_len.
    str_columns = [_coerce_cell(c) for c in columns]
    coerced_rows = [[_coerce_cell(row.get(c, "")) for c in columns] for row in rows]

    # column_types must be keyed by the same strings as columns for the widget's
    # typed sort to engage; a mismatch silently degrades to string sort, never
    # errors. Production producers (`DataFrameComponent.from_records`) never set
    # `column_types`, so infer "number" from the data first, then let any
    # explicit producer-supplied type override the inferred value.
    column_types = _infer_column_types(str_columns, coerced_rows)
    column_types.update(_safe_column_types(rich))

    payload: dict = {
        "columns": str_columns,
        "rows": coerced_rows,
        "column_types": column_types,
        "row_count": len(coerced_rows),
        "truncated": 0,
    }

    if _serialized_len(payload) <= _MAX_TABLE_PAYLOAD_BYTES:
        return payload

    # Over budget: keep the largest *contiguous prefix* of rows that fits, so
    # the widget's row_count + truncated == total invariant holds. ``truncated``
    # reports how many tail rows were dropped.
    total = len(coerced_rows)
    payload["rows"] = []
    if _serialized_len(payload) > _MAX_TABLE_PAYLOAD_BYTES:
        # Header-only form alone exceeds the budget — nothing useful to send.
        return None

    lo, hi = 0, total
    while lo < hi:
        mid = (lo + hi + 1) // 2
        payload["rows"] = coerced_rows[:mid]
        if _serialized_len(payload) <= _MAX_TABLE_PAYLOAD_BYTES:
            lo = mid
        else:
            hi = mid - 1

    payload["rows"] = coerced_rows[:lo]
    payload["row_count"] = lo
    payload["truncated"] = total - lo
    return payload


def _serialized_len(payload: dict) -> int:
    # json.dumps defaults to ensure_ascii=True, so the result is pure ASCII and
    # len(str) == the serialized byte size the host actually receives — non-ASCII
    # cells escape to \uXXXX rather than inflating bytes past this measure.
    return len(json.dumps(payload, separators=(",", ":")))


def _serialized_len_list(blocks: list[dict]) -> int:
    """Serialized byte size of the whole ``blocks`` array under the ``_meta`` cap."""
    return len(json.dumps(blocks, separators=(",", ":")))


def _coerce_chart_value(value: object) -> object:
    # Unlike the table payload (everything → str so the grid renders text),
    # ECharts needs *real numbers* for axes/series, so numerics pass through
    # un-stringified. bool is JSON-native and kept as-is. Decimal is numeric
    # but not JSON-serializable, so it becomes float. Non-finite floats
    # (inf/NaN) are not valid JSON and would render as broken points, so they
    # degrade to None (ECharts skips null). Everything else (str, datetime,
    # None, ...) collapses to the same naive str() the table path uses, except
    # None which stays None so the widget can treat it as "no value".
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        f = float(value)
        return f if math.isfinite(f) else None
    return str(value)


def _build_chart_payload(rich) -> dict | None:  # type: ignore[no-untyped-def]
    # Same best-effort contract as _build_table_payload: payload construction
    # must never escape query_database's sanitized error taxonomy. On any
    # failure, degrade to "no widget" (the Markdown answer still stands).
    try:
        return _compute_chart_payload(rich)
    except Exception:
        spec = getattr(rich, "data", None)
        n_rows = len(spec.get("data", [])) if isinstance(spec, dict) else 0
        logger.warning(
            "chart payload construction failed; serving Markdown only "
            "(rows=%d)",
            n_rows,
            exc_info=True,
        )
        return None


def _compute_chart_payload(rich) -> dict | None:  # type: ignore[no-untyped-def]
    # Returns the chart payload WITHOUT the ``"type": "chart"`` discriminator —
    # the wrapping is done by ``components_to_blocks``. The per-block size cap
    # is enforced here against ``_MAX_CHART_PAYLOAD_BYTES``.
    spec = getattr(rich, "data", None)
    if not isinstance(spec, dict):
        return None

    rows = spec.get("data", [])
    if not isinstance(rows, list):
        return None

    coerced_rows = [
        {_coerce_cell(k): _coerce_chart_value(v) for k, v in row.items()}
        for row in rows
        if isinstance(row, dict)
    ]
    # Producer-side regression detector: any non-dict row indicates chart-
    # contract drift (e.g. tuple-shaped rows leaking from a future producer).
    # Always log when at least one row was dropped — a partial drop is the
    # more dangerous case because the chart still renders, just with a
    # silently shortened series, so the operator needs the server-side
    # signal symmetrically with the all-dropped case.
    dropped = len(rows) - len(coerced_rows)
    if dropped:
        logger.warning(
            "chart payload dropped %d non-dict row(s) of %d (kept %d)",
            dropped,
            len(rows),
            len(coerced_rows),
        )
    total = len(coerced_rows)

    payload: dict = {
        "chart_type": spec.get("chart_type"),
        "title": spec.get("title"),
        "x": spec.get("x"),
        "y": spec.get("y"),
        "series": spec.get("series"),
        "data": coerced_rows,
        "row_count": total,
        "truncated": 0,
    }

    if _serialized_len(payload) <= _MAX_CHART_PAYLOAD_BYTES:
        return payload

    # Over budget: keep the largest contiguous row prefix that fits, so the
    # widget's row_count + truncated == total invariant holds (mirrors the
    # table path's binary search exactly).
    payload["data"] = []
    if _serialized_len(payload) > _MAX_CHART_PAYLOAD_BYTES:
        # Even the data-stripped spec busts the budget — nothing useful to send.
        return None

    lo, hi = 0, total
    while lo < hi:
        mid = (lo + hi + 1) // 2
        payload["data"] = coerced_rows[:mid]
        if _serialized_len(payload) <= _MAX_CHART_PAYLOAD_BYTES:
            lo = mid
        else:
            hi = mid - 1

    payload["data"] = coerced_rows[:lo]
    payload["row_count"] = lo
    payload["truncated"] = total - lo
    return payload


def _trim_blocks_to_budget(blocks: list[dict]) -> list[dict]:
    """Bound the whole ``blocks`` array to ``_MAX_BLOCKS_TOTAL_BYTES``.

    Each individual block is already capped by the per-block budget; this
    bounds the *sum* so a long stream of in-budget blocks still cannot blow
    the iframe ``_meta`` ceiling. When over budget, keep the longest prefix
    that fits with a single appended truncation-notice text block; emit a
    server-side ``logger.warning`` whenever any trimming happens (CLAUDE.md:
    never silently drop).
    """
    if not blocks:
        return blocks
    if _serialized_len_list(blocks) <= _MAX_BLOCKS_TOTAL_BYTES:
        return blocks
    total = len(blocks)
    # Try progressively shorter prefixes (with the notice appended) until one
    # fits. Block counts are bounded — a few dozen per turn at most — so linear
    # is fine and the code is clearer than a binary search here.
    for keep in range(total - 1, -1, -1):
        notice = _truncation_notice_block(total - keep)
        candidate = [*blocks[:keep], notice]
        if _serialized_len_list(candidate) <= _MAX_BLOCKS_TOTAL_BYTES:
            logger.warning(
                "sqllens/blocks budget exceeded; trimmed %d trailing block(s) "
                "of %d (kept %d) to fit %d byte ceiling",
                total - keep,
                total,
                keep,
                _MAX_BLOCKS_TOTAL_BYTES,
            )
            return candidate
    # Even the notice alone busts the budget — nothing we can return that
    # honours the ceiling AND surfaces the truncation. Log loud, return empty
    # so the caller's interactive/no-answer fallback engages.
    logger.error(
        "sqllens/blocks budget exceeded with zero in-budget prefix; emitting "
        "no blocks (truncation notice itself busts the %d byte ceiling)",
        _MAX_BLOCKS_TOTAL_BYTES,
    )
    return []


def _truncation_notice_block(dropped: int) -> dict:
    plural = "s" if dropped != 1 else ""
    return {
        "type": "text",
        "text": (
            f"_⚠️ Response truncated: {dropped} trailing block{plural} dropped "
            "to fit the rendering size budget._"
        ),
    }


def _serialize_blocks_to_markdown(blocks: list[dict]) -> str:
    """Render the ordered blocks as a plain-Markdown answer for non-apps clients.

    Tables become Markdown tables (the same shape :func:`_render_dataframe`
    produces), text blocks pass through verbatim, charts collapse to a short
    ``_[chart: …]_`` placeholder — apps-aware hosts render the real ECharts
    chart via ``_meta["sqllens/blocks"]``. Parts are joined with blank lines
    to match the previous tables-then-text Markdown shape.
    """
    parts: list[str] = []
    for b in blocks:
        btype = b.get("type")
        if btype == "text":
            text = b.get("text", "")
            if isinstance(text, str) and text:
                parts.append(text)
        elif btype == "table":
            md = _table_block_to_markdown(b)
            if md:
                parts.append(md)
        elif btype == "chart":
            label = (
                b.get("title") or b.get("chart_type") or "chart"
            )
            parts.append(f"_[chart: {label}]_")
    return "\n\n".join(parts)


def _table_block_to_markdown(block: dict) -> str:
    """Render one table block as a Markdown table (mirrors :func:`_render_dataframe`)."""
    columns = block.get("columns") or []
    rows = block.get("rows") or []
    if not columns and not rows:
        return ""

    header = "| " + " | ".join(str(c) for c in columns) + " |"
    separator = "|" + "|".join(["---"] * len(columns)) + "|"
    body_rows = []
    for row in rows[:_MAX_ROWS_RENDERED]:
        cells = [str(c) for c in row]
        body_rows.append("| " + " | ".join(cells) + " |")
    note = ""
    # row_count is the kept prefix after the per-block size cap; truncated is
    # the dropped tail. The "true" total served by the SQL is their sum.
    total = block.get("row_count", len(rows)) + block.get("truncated", 0)
    if total > _MAX_ROWS_RENDERED:
        note = f"\n\n_Showing first {_MAX_ROWS_RENDERED} of {total} rows._"
    return "\n".join([header, separator, *body_rows]) + note


def _render_dataframe(rich) -> str:  # type: ignore[no-untyped-def]
    """Render a DataFrame component as a Markdown table (legacy helper, tests).

    Retained because ``tests/unit/test_format.py`` pins the precise Markdown
    shape (header, separator, cell coercion, 500-row truncation footer) by
    calling this directly. The production path goes through
    :func:`components_to_blocks` → :func:`_serialize_blocks_to_markdown`, which
    produces the same shape.
    """
    columns, rows = _columns_and_rows(rich)
    if not columns and not rows:
        return ""

    header = "| " + " | ".join(columns) + " |"
    separator = "|" + "|".join(["---"] * len(columns)) + "|"
    body_rows = []
    for row in rows[:_MAX_ROWS_RENDERED]:
        body_rows.append("| " + " | ".join(_coerce_cell(row.get(c, "")) for c in columns) + " |")

    note = ""
    if len(rows) > _MAX_ROWS_RENDERED:
        note = f"\n\n_Showing first {_MAX_ROWS_RENDERED} of {len(rows)} rows._"
    return "\n".join([header, separator, *body_rows]) + note
