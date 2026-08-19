from types import SimpleNamespace

import clients.claude as claude


class _Msg(SimpleNamespace):
    pass


def _msg(stop_reason, text=None, in_tok=10, out_tok=5):
    content = [SimpleNamespace(type="text", text=text)] if text is not None else []
    return _Msg(
        stop_reason=stop_reason,
        content=content,
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )


class _FakeRunner:
    """Yields a scripted sequence of assistant messages like BetaToolRunner."""

    def __init__(self, messages):
        self._messages = list(messages)
        self.consumed = 0

    def __iter__(self):
        for m in self._messages:
            self.consumed += 1
            yield m


def _install(monkeypatch, runner, captured):
    def fake_tool_runner(**kwargs):
        captured.update(kwargs)
        return runner

    fake_client = SimpleNamespace(
        beta=SimpleNamespace(messages=SimpleNamespace(tool_runner=fake_tool_runner))
    )
    monkeypatch.setattr(claude, "_get_client", lambda: fake_client)


def test_run_agent_returns_final_text_and_passes_params(monkeypatch):
    captured = {}
    runner = _FakeRunner([_msg("tool_use"), _msg("end_turn", text='{"actionable": true}')])
    _install(monkeypatch, runner, captured)
    text, stop = claude.run_agent(
        system="SYS", user="USER", tools=["t"], output_schema={"type": "object"}, max_iterations=4
    )
    assert (text, stop) == ('{"actionable": true}', "end_turn")
    assert captured["model"] == "claude-sonnet-5"
    assert captured["max_iterations"] == 4
    assert captured["thinking"] == {"type": "adaptive"}
    assert captured["output_config"]["effort"] == "medium"
    assert captured["output_config"]["format"] == {"type": "json_schema", "schema": {"type": "object"}}
    assert captured["system"][0]["text"] == "SYS"
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert captured["messages"] == [{"role": "user", "content": "USER"}]
    assert captured["tools"] == ["t"]
    assert captured["timeout"] == 30.0


def test_run_agent_refusal(monkeypatch):
    _install(monkeypatch, _FakeRunner([_msg("refusal")]), {})
    assert claude.run_agent(system="s", user="u", tools=[], output_schema={}) == (None, "refusal")


def test_run_agent_max_iterations_when_last_turn_still_wants_tools(monkeypatch):
    _install(monkeypatch, _FakeRunner([_msg("tool_use"), _msg("tool_use")]), {})
    assert claude.run_agent(system="s", user="u", tools=[], output_schema={}, max_iterations=2) == (
        None,
        "max_iterations",
    )


def test_run_agent_timeout_between_turns(monkeypatch):
    runner = _FakeRunner([_msg("tool_use"), _msg("end_turn", text="late")])
    _install(monkeypatch, runner, {})
    clock = iter([0.0, 100.0, 200.0, 300.0])
    monkeypatch.setattr(claude.time, "monotonic", lambda: next(clock))
    assert claude.run_agent(system="s", user="u", tools=[], output_schema={}, deadline_s=60) == (
        None,
        "timeout",
    )
    assert runner.consumed == 1  # stopped before asking for the second turn


def test_run_agent_records_token_usage(monkeypatch):
    spent = []
    monkeypatch.setattr(claude.otel.claude_tokens, "add", lambda n, attrs: spent.append((n, attrs)))
    _install(monkeypatch, _FakeRunner([_msg("end_turn", text="{}", in_tok=7, out_tok=3)]), {})
    claude.run_agent(system="s", user="u", tools=[], output_schema={})
    assert (7, {"token_type": "input"}) in spent
    assert (3, {"token_type": "output"}) in spent
