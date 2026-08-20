"""Anthropic client for task enrichment. Trimmed migration of inbox
clients/claude.py — summarize() and extract() only."""

import logging
import os
import time

import anthropic

import clients.otel as otel

logger = logging.getLogger(__name__)

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _record_usage(response) -> None:
    usage = response.usage
    otel.claude_tokens.add(usage.input_tokens, {"token_type": "input"})
    otel.claude_tokens.add(usage.output_tokens, {"token_type": "output"})


def extract(prompt: str) -> str:
    """Single-turn extraction call. Temperature 0, max_tokens 20. Returns raw stripped text."""
    # temperature is gone from the Messages overloads in newer SDKs (the 4.7+
    # models reject it outright) but is still accepted at runtime by the older
    # models these two calls pin. Keep the determinism; silence the typing.
    response = _get_client().messages.create(  # type: ignore[call-overload]
        model="claude-sonnet-4-6",
        max_tokens=20,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    _record_usage(response)
    return response.content[0].text.strip()  # type: ignore[union-attr]


def summarize(prompt: str) -> str:
    """Extract structured summary. Haiku, temperature 0, max_tokens 400. Returns raw text."""
    response = _get_client().messages.create(  # type: ignore[call-overload]
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    _record_usage(response)
    return response.content[0].text.strip()  # type: ignore[union-attr]


AGENT_MODEL = "claude-sonnet-5"


def run_agent(
    *,
    system: str,
    user: str,
    tools: list,
    output_schema: dict,
    max_iterations: int = 6,
    deadline_s: float = 60.0,
    request_timeout: float = 30.0,
) -> tuple[str | None, str]:
    """Run a tool-use loop with the SDK tool runner. Returns (final_text, stop)
    where stop is the SDK's stop reason verbatim — 'end_turn' is the only
    success value. Anything else means the caller should fail open: a
    non-tool-use, non-refusal stop such as 'max_tokens' (a turn truncated
    before it finished) surfaces here under its own name instead of being
    collapsed into 'end_turn', plus the sentinels 'max_iterations' and
    'timeout' this function itself produces on the corresponding failure.
    final_text is the last assistant message's text (JSON per output_schema)
    for 'end_turn' and any other stop reached by breaking out of the loop
    (e.g. 'max_tokens'); it is None for 'refusal', 'max_iterations', and
    'timeout'. Callers should only read the text on 'end_turn' (see
    services/triage.py::_parse). Exceptions propagate — the caller owns
    fail-open.

    deadline_s is checked only between turns (after a message completes,
    before the next tool-use turn is requested) — it bounds when a new turn
    may start, not a hard wall-clock stop on a turn already in flight."""
    runner = (
        _get_client()
        .with_options(max_retries=1)
        .beta.messages.tool_runner(
            model=AGENT_MODEL,
            max_tokens=4096,
            max_iterations=max_iterations,
            thinking={"type": "adaptive"},
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": output_schema},
            },
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            tools=tools,
            timeout=request_timeout,
        )
    )
    started = time.monotonic()
    last = None
    for message in runner:
        last = message
        _record_usage(message)
        if message.stop_reason == "refusal":
            return None, "refusal"
        if message.stop_reason != "tool_use":
            break
        if time.monotonic() - started > deadline_s:
            return None, "timeout"
    if last is None or last.stop_reason == "tool_use":
        return None, "max_iterations"
    text = "".join(
        b.text  # type: ignore[union-attr]
        for b in last.content
        if getattr(b, "type", None) == "text"
    )
    # A terminal message always carries a stop reason; treat a missing one as
    # unknown rather than success — _parse fails open on anything but end_turn.
    return text, last.stop_reason or "unknown"
