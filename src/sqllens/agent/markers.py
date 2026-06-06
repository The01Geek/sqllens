"""Marker conventions for opt-in flags on vendored UI components.

Vendored upstream components carry a generic ``data: Dict[str, Any]`` extension
field (see ``RichComponent.data``). We use it to attach SQL Lens-specific
opt-in flags without subclassing — keeping the lift surface small and the
extension shape obvious from one place.

Today's only marker is :data:`IS_ANSWER_MARKER_KEY`, set on every TEXT
``UiComponent`` whose content should reach the user as a deliberate prose
block in the rendered answer (the agent's terminal answer + iteration-limit
warning, and every ``EmitTextTool`` output). The MCP-layer block builder
(:func:`sqllens.tools._format.components_to_blocks`) reads it to discriminate
deliberate prose from intermediate reasoning chatter — assistant text yielded
during a tool call when ``UI_FEATURE_SHOW_TOOL_INVOCATION_MESSAGE_IN_CHAT`` is
on, which carries no marker and must be dropped from the rendered answer.
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
