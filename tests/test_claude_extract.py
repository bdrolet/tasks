from types import SimpleNamespace

import pytest

import clients.claude as claude


class _Messages:
    def __init__(self, stop, text='{"a": 1}'):
        self.stop, self.text, self.kwargs = stop, text, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            stop_reason=self.stop,
            content=[
                SimpleNamespace(type="thinking", thinking=""),
                SimpleNamespace(type="text", text=self.text),
            ],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )


def _install(monkeypatch, stop):
    m = _Messages(stop)
    monkeypatch.setattr(claude, "_get_client", lambda: SimpleNamespace(messages=m))
    return m


def test_extract_structured_shapes_the_request(monkeypatch):
    m = _install(monkeypatch, "end_turn")
    out = claude.extract_structured(
        model="claude-opus-5", system="s", user="u", schema={"type": "object"}
    )
    assert out == '{"a": 1}'
    assert m.kwargs["model"] == "claude-opus-5"
    assert m.kwargs["thinking"] == {"type": "adaptive"}
    assert m.kwargs["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": {"type": "object"}},
    }
    assert "temperature" not in m.kwargs
    assert m.kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_extract_structured_raises_on_non_end_turn(monkeypatch, stop):
    _install(monkeypatch, stop)
    with pytest.raises(RuntimeError):
        claude.extract_structured(model="claude-opus-5", system="s", user="u", schema={})
