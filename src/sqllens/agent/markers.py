"""Marker conventions for opt-in flags on vendored UI components.

Vendored upstream components carry a generic ``data: Dict[str, Any]`` extension
field (see ``RichComponent.data``). We use it to attach SQL Lens-specific
opt-in flags without subclassing — keeping the lift surface small and the
extension shape obvious from one place.

The first marker is :data:`IS_ANSWER_MARKER_KEY`, set on every TEXT
``UiComponent`` whose content should reach the user as a deliberate prose
block in the rendered answer (the agent's terminal answer + iteration-limit
warning, and every ``EmitTextTool`` output). The MCP-layer block builder
(:func:`sqllens.tools._format.components_to_blocks`) reads it to discriminate
deliberate prose from intermediate reasoning chatter — assistant text yielded
during a tool call when ``UI_FEATURE_SHOW_TOOL_INVOCATION_MESSAGE_IN_CHAT`` is
on, which carries no marker and must be dropped from the rendered answer.
The agent loop also reads it: a successful tool result carrying the marker
hides that request's final reply (only ``EmitTextTool`` marks a tool result).

The second is :data:`ROW_CAP_MARKER_KEY`, set on a DATAFRAME component whose
query stopped at the row cap.
"""

# The ``data`` key set to ``True`` on TEXT UiComponents whose content the user
# should see. Centralized here so a rename or typo can't silently desynchronize
# the producers (agent terminal yields, EmitTextTool) from the consumer
# (components_to_blocks's text-block selector).
IS_ANSWER_MARKER_KEY = "is_answer"


def answer_marker_data() -> dict:
    """Build the ``data`` dict that marks a TEXT component as a deliberate answer.

    A tiny helper so the producers (agent, emit_text tool) never spell out the
    literal mapping shape — the consumer side reads ``IS_ANSWER_MARKER_KEY``
    out of whatever ``data`` dict ends up on the component.
    """
    return {IS_ANSWER_MARKER_KEY: True}


def is_answer_marked(rich: object) -> bool:
    """True iff ``rich`` carries the answer marker set by :func:`answer_marker_data`.

    Strict identity (``is True``): a non-bool value under the key must not
    count as a marker.
    """
    data = getattr(rich, "data", None)
    return isinstance(data, dict) and data.get(IS_ANSWER_MARKER_KEY) is True


# The ``data`` key holding the row cap (an ``int``) on a DATAFRAME component
# whose query stopped at that cap. ``RunSqlTool`` sets it; the shared table
# payload (``sqllens._table_payload``) copies it into the table block.
ROW_CAP_MARKER_KEY = "row_cap"


def row_cap_marker_data(cap: int) -> dict:
    """Build the ``data`` dict marking a DATAFRAME component as stopped at ``cap`` rows."""
    return {ROW_CAP_MARKER_KEY: int(cap)}


def row_cap_of(rich: object) -> int | None:
    """Return the row cap ``rich`` was stopped at, or ``None`` when it was not.

    Only a positive ``int`` (not a ``bool``) counts, so a malformed value never
    produces a notice with a nonsense cap.
    """
    data = getattr(rich, "data", None)
    if not isinstance(data, dict):
        return None
    cap = data.get(ROW_CAP_MARKER_KEY)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        return None
    return cap
