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
from sqllens.agent.markers import IS_ANSWER_MARKER_KEY
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

    The agent emits two distinct kinds of TEXT, and only one sets the marker:

    - **Intermediate reasoning** that accompanies a tool call
      (``agent/core/agent/agent.py``: ``RichTextComponent(content=response.
      content, ...)`` inside the LLM loop, gated on
      ``UI_FEATURE_SHOW_TOOL_INVOCATION_MESSAGE_IN_CHAT``). This branch does
      **NOT** set the marker — that is the discriminator.
    - The **terminal answer** and **iteration-limit warning** at end-of-turn,
      both of which DO set the
      :data:`~sqllens.agent.markers.IS_ANSWER_MARKER_KEY` flag in ``data``.

    The ``EmitTextTool`` sets the same flag on deliberate interleaved prose.
    So a marked TEXT is something the user should see; an unmarked TEXT is
    reasoning chatter. The check is strict-identity (``is True``) so a
    producer drift that stuffs a non-bool value under the marker key (string
    "yes", truthy list) does not silently flip the discrimination.
    """
    data = getattr(rich, "data", None)
    return isinstance(data, dict) and data.get(IS_ANSWER_MARKER_KEY) is True


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
    (possibly trimmed) blocks — tables rendered as Markdown tables, text
    blocks verbatim, charts as a short ``_[chart: …]_`` placeholder. Order
    follows the agent's stream rather than the previous collapse's
    tables-then-text convention, so a ``[text, table]`` stream now renders
    text before the table; the per-element rendering shape is unchanged.
    When no blocks are produced, :func:`render_interactive` is consulted for
    an interactive-affordance fallback before settling on ``"(no answer)"``.
    """
    # Materialize once: ``render_interactive`` (the no-blocks fallback below)
    # does another pass over the stream; the public signature accepts any
    # Iterable (incl. generators).
    components = list(components)

    # Single forward pass: collect block candidates in stream order (each text
    # block tagged with its is_answer marker so we can filter unmarked TEXTs
    # post-walk), plus the cross-cutting error / last_sql / last_memory state.
    error_message = ""
    last_sql: str | None = None
    last_memory: dict | None = None
    # Row count attributable to the *last* run_sql, tracked in stream order so
    # query_info reports the correct total even when the agent runs multiple
    # run_sql calls in one turn. The walker resets this on every NEW run_sql
    # invocation (the next DATAFRAME will be its result) and updates it on
    # every DATAFRAME (the rows of the latest run_sql). End-state:
    #
    #   - No DATAFRAME component followed the last run_sql at all (zero-row
    #     SELECTs whose driver emits no component, or a tool error before any
    #     DATAFRAME yield) → stays None → query_info omits ``row_count``.
    #   - DATAFRAME component present, payload built successfully → set to
    #     ``payload["row_count"] + payload["truncated"]`` (the TRUE size, not
    #     the rendered subset; per-block size cap may have dropped a tail).
    #   - DATAFRAME component present but payload computer rejected the
    #     result → use the raw row count from the component itself
    #     (``len(rich.rows)``) so the over-budget / payload-exception case
    #     doesn't silently misattribute the 0-rows count to a SQL that
    #     returned N > 0 rows. This is the CLAUDE.md "lossy success needs
    #     loud warning, not green output" trap.
    #
    # Preventing the misattribution where an earlier table's row count would
    # be reported as the LATEST SQL's is the regression
    # ``test_query_info_row_count_attributed_to_last_run_sql_when_empty``
    # pins; the over-budget / payload-fail nuance is pinned by
    # ``test_query_info_row_count_recovered_from_raw_rows_on_payload_reject``.
    last_sql_row_count: int | None = None
    # Candidate blocks. Text blocks carry a private "_is_answer" tag we strip
    # before emitting — never expose internal bookkeeping fields in the public
    # ``sqllens/blocks`` wire shape.
    candidates: list[dict] = []
    any_marked_text = False

    for comp in components:
        rich = comp.rich_component
        if rich is None:
            continue
        ctype = getattr(rich, "type", None)

        if ctype == ComponentType.TEXT:
            content = (getattr(rich, "content", "") or "").strip()
            if not content:
                continue
            is_marked = _text_is_answer_marked(rich)
            if is_marked:
                any_marked_text = True
            candidates.append(
                {"type": "text", "text": content, "_is_answer": is_marked}
            )
        elif ctype == ComponentType.DATAFRAME:
            payload = _build_table_payload(rich)
            if payload is not None:
                candidates.append({"type": "table", **payload})
                # The size-capped payload may have dropped a tail; row_count
                # is the kept prefix and truncated the dropped tail, so the
                # sum is the TRUE row count the SQL produced.
                last_sql_row_count = payload.get("row_count", 0) + payload.get(
                    "truncated", 0
                )
            else:
                # ``_build_table_payload`` returns None in three distinct
                # cases: (a) the DataFrame is genuinely empty (no columns,
                # no rows), (b) the header-only form busts the per-block
                # size cap, (c) the broad ``except`` caught a payload-
                # construction exception. Only case (a) means "0 rows" —
                # cases (b) and (c) have the real rows on the component
                # itself. Source the count from ``rich.rows`` so we don't
                # silently misreport "0 rows" to a user whose SQL actually
                # returned N > 0 rows.
                raw_rows = getattr(rich, "rows", None) or []
                last_sql_row_count = len(raw_rows)
        elif ctype == ComponentType.CHART:
            payload = _build_chart_payload(rich)
            if payload is not None:
                candidates.append({"type": "chart", **payload})
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
                # Reset the row-count tracker on EITHER edge that uniquely
                # marks the start of a new run_sql call: the ``running`` card
                # (today the agent's emission pattern), OR a SQL-text change
                # against the previous card (defence in depth for streams
                # where the running card is missing or dropped). Combining
                # the two predicates catches three failure modes:
                #
                #   1. The running/completed pair share the same SQL metadata
                #      (last-wins de-dupe in ``_format`` relies on this), so
                #      resetting on the completed card alone would wipe the
                #      row count that the intervening DATAFRAME just
                #      populated. ``status == "running"`` only fires once
                #      per normal call, so the reset lands at the right
                #      moment (before the call's DataFrame is emitted).
                #   2. A real retry of the *same* SQL text (agent re-runs an
                #      identical query, e.g. after a deadlock) where the
                #      running card IS emitted — covered by clause 1.
                #   3. Defensive: a future producer that emits a completion-
                #      only card (no preceding ``running``), or an upstream
                #      exception between the running and completion yields
                #      that drops the running emission. The text-change
                #      clause catches the new-SQL-without-running case and
                #      keeps the iter-1 regression
                #      ``test_query_info_row_count_attributed_to_last_run_sql
                #      _when_empty`` honest against emission drift.
                if status == "running" or sql != last_sql:
                    last_sql_row_count = None
                last_sql = sql
            if isinstance(metadata, dict):
                memory_search = metadata.get("memory_search")
                if isinstance(memory_search, dict):
                    last_memory = memory_search

    if error_message:
        return error_message, True, [], None, None

    # Resolve text-block inclusion: prefer the explicit answer-marker (set by
    # EmitTextTool and the agent's terminal answer); if the stream carries no
    # marker (e.g. tests using bare RichTextComponent, or an abnormal
    # termination path where the agent never reached its terminal-answer
    # yield), fall back to the last non-empty TEXT alone — the same "last
    # TEXT wins" semantics the previous collapse used as the terminal-answer
    # heuristic. In either case, strip the private "_is_answer" tag before
    # emitting the public block.
    blocks: list[dict] = []
    last_unmarked_text_idx: int | None = None
    for i, cand in enumerate(candidates):
        if cand["type"] != "text":
            continue
        if not cand["_is_answer"]:
            last_unmarked_text_idx = i
    for i, cand in enumerate(candidates):
        if cand["type"] == "text":
            if cand["_is_answer"]:
                blocks.append({"type": "text", "text": cand["text"]})
            elif not any_marked_text and i == last_unmarked_text_idx:
                blocks.append({"type": "text", "text": cand["text"]})
        else:
            blocks.append(cand)
    # The agent's production terminal-answer / iteration-limit-warning yields
    # always set the marker; the EmitTextTool also always marks its emission.
    # So a stream with at least one TEXT but no marked TEXT typically means
    # either (a) a test fixture using bare ``RichTextComponent`` (common, not
    # a problem), or (b) an abnormal termination (the agent exited before
    # reaching its terminal-answer yield, e.g. a caught-and-re-raised
    # exception inside the LLM loop) or a producer-side marker-emission
    # regression (rare, but worth a trail). Log at ``info`` rather than
    # ``warning`` so the diagnostic is captured under verbose logging without
    # polluting the warning-level operator surface with the test-fixture
    # case. An operator watching for the abnormal-termination signal can
    # raise the log level to INFO and grep for this exact message.
    if not any_marked_text and last_unmarked_text_idx is not None:
        logger.info(
            "components_to_blocks: no answer-marked TEXT in stream; falling "
            "back to last unmarked TEXT as the rendered answer. In production "
            "this signals either an abnormal termination (the agent never "
            "reached its terminal-answer yield) or a marker-emission "
            "regression on one of the producers; in tests it commonly fires "
            "when fixtures use unmarked RichTextComponent on purpose."
        )

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
        # Row count is captured in the walker against the LAST run_sql card so
        # the count is correctly attributed to the SQL that produced it — not
        # to whatever the trailing table block in ``blocks`` happens to be.
        # The two diverge when the last run_sql returned 0 rows (no DataFrame
        # / empty DataFrame → no table block) but an earlier run_sql in the
        # same turn had rows: walking blocks backward would land on the
        # earlier table and misattribute its count to the later SQL.
        # ``last_sql_row_count`` is None when the SQL ran but no DATAFRAME was
        # seen (zero rows on stdout-only output, or a tool error after the
        # SQL was logged) — query_info correctly omits row_count in that case.
        query_info = _query_info_from_sql(last_sql, last_sql_row_count)
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


def _serialized_len(payload: dict | list) -> int:
    # json.dumps defaults to ensure_ascii=True, so the result is pure ASCII and
    # len(str) == the serialized byte size the host actually receives — non-ASCII
    # cells escape to \uXXXX rather than inflating bytes past this measure.
    # Accepts both dicts (individual payloads) and lists (the whole ``blocks``
    # array) so the per-block cap and the blocks-total cap measure size the
    # same way and cannot drift apart.
    return len(json.dumps(payload, separators=(",", ":")))


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
    the iframe ``_meta`` ceiling. When over budget, drop trailing blocks one
    at a time until the kept prefix + an appended truncation-notice text block
    fits; emit a server-side ``logger.warning`` whenever any trimming happens
    (CLAUDE.md: never silently drop).
    """
    if not blocks or _serialized_len(blocks) <= _MAX_BLOCKS_TOTAL_BYTES:
        return blocks
    total = len(blocks)
    kept = list(blocks)
    # Shrink the kept prefix one block at a time, re-checking against the
    # budget WITH the notice appended (the notice's size grows by ~one digit
    # as the dropped count rises, but stays under a hundred bytes — its
    # contribution is bounded and we don't need to predict it).
    while kept:
        kept.pop()
        notice = _truncation_notice_block(total - len(kept))
        if _serialized_len([*kept, notice]) <= _MAX_BLOCKS_TOTAL_BYTES:
            logger.warning(
                "sqllens/blocks budget exceeded; trimmed %d trailing block(s) "
                "of %d (kept %d) to fit %d byte ceiling",
                total - len(kept),
                total,
                len(kept),
                _MAX_BLOCKS_TOTAL_BYTES,
            )
            return [*kept, notice]
    # Even the notice alone busts the budget — only reachable if the budget
    # has been configured below the ~100 byte notice size (a configuration
    # error, not a normal state — the production ceiling is hundreds of
    # kilobytes). Per CLAUDE.md's "Lossy / empty success needs a loud
    # warning, not green output" rule, returning an empty list here would
    # let the caller's interactive/no-answer fallback render `"(no answer)"`
    # to the user — a silent success on a turn that actually produced data.
    # Instead, return a single notice block so the user sees the truncation
    # signal rather than nothing.
    logger.error(
        "sqllens/blocks budget exceeded with zero in-budget prefix; emitting "
        "notice-only blocks list (budget %d bytes is below the notice size, "
        "likely a misconfiguration)",
        _MAX_BLOCKS_TOTAL_BYTES,
    )
    return [_truncation_notice_block(total)]


def _truncation_notice_block(dropped: int) -> dict:
    plural = "s" if dropped != 1 else ""
    return {
        "type": "text",
        "text": (
            f"_Response truncated: {dropped} trailing block{plural} dropped "
            "to fit the rendering size budget._"
        ),
    }


def _serialize_blocks_to_markdown(blocks: list[dict]) -> str:
    """Render the ordered blocks as a plain-Markdown answer for non-apps clients.

    Tables become Markdown tables (the same shape :func:`_render_dataframe`
    produces), text blocks pass through verbatim, charts collapse to a short
    ``_[chart: …]_`` placeholder (apps-aware hosts render the real ECharts
    chart via ``_meta["sqllens/blocks"]``). Parts are joined with ``\\n\\n``;
    element order follows the agent's stream (unlike the previous collapse
    which always put tables before text).
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
            label = b.get("title") or b.get("chart_type")
            if not label:
                # No meaningful identifier — emit a generic "unavailable"
                # placeholder rather than the ambiguous "_[chart: chart]_"
                # which reads as a literal-string-named chart.
                parts.append("_[chart unavailable]_")
            else:
                # Escape Markdown-significant characters so a chart title
                # containing underscores or asterisks (e.g. an SQL identifier
                # title like "user_id_*by*_month") does not break the
                # surrounding italics formatting.
                escaped = str(label).replace("\\", "\\\\").replace(
                    "_", r"\_"
                ).replace("*", r"\*")
                parts.append(f"_[chart: {escaped}]_")
        else:
            # Unknown discriminator — a producer-side regression (typo, case
            # mismatch, future block type the renderer doesn't know yet, or
            # a missing ``type`` key). CLAUDE.md: never silently drop. Surface
            # the loss to both the operator log AND the rendered output so a
            # regression cannot ship with the contents invisibly missing.
            #
            # The btype value is wrapped in a fenced-code span (`` ` ``)
            # rather than the bare ``{btype!r}`` interpolation an earlier
            # iteration used: a typical producer-drift btype is an
            # identifier-like string (e.g. ``"my_block"``), and its
            # ``_`` characters would prematurely terminate the surrounding
            # italic span, mangling the rest of the rendered Markdown
            # downstream. The fenced-code wrapper is XSS-safe in any
            # markdown renderer and renders correctly regardless of which
            # markdown-significant characters the discriminator contains.
            logger.warning(
                "_serialize_blocks_to_markdown: dropping unknown block type "
                "%r from markdown serialization (likely producer drift)",
                btype,
            )
            parts.append(f"_[unsupported block type: `{btype}`]_")
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
    """Render a DataFrame component as a Markdown table (delegates to block path).

    Retained because ``tests/unit/test_format.py`` pins the precise Markdown
    shape (header, separator, cell coercion, 500-row truncation footer) by
    calling this directly. The production path goes through
    :func:`components_to_blocks` → :func:`_serialize_blocks_to_markdown`, which
    invokes :func:`_table_block_to_markdown` — the same function this helper
    delegates to, so the two paths emit byte-identical Markdown.
    """
    columns, rows = _columns_and_rows(rich)
    if not columns and not rows:
        return ""
    block = {
        "columns": [_coerce_cell(c) for c in columns],
        "rows": [[_coerce_cell(row.get(c, "")) for c in columns] for row in rows],
        "row_count": len(rows),
        "truncated": 0,
    }
    return _table_block_to_markdown(block)
