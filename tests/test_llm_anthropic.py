"""Claude provider in the LLM chain: opt-in, key-gated, refusal falls through."""

import httpx
import pytest

import utils.ollama_client as oc


class _DeadOllama:
    def list(self):
        raise httpx.ConnectError("ollama down")

    def chat(self, **kwargs):
        raise httpx.ConnectError("ollama down")


class _GoodOllama:
    def list(self):
        raise httpx.ConnectError("skip resolve")  # _resolve_model swallows this

    def chat(self, **kwargs):
        class Msg:
            content = '{"from": "ollama"}'

        class Resp:
            message = Msg()
        return Resp()


class _FakeClaude:
    """Stands in for utils.anthropic_client.anthropic_chat (no network)."""

    def __init__(self, content='{"from": "anthropic"}', fail=None):
        self.content, self.fail, self.calls = content, fail, []

    def __call__(self, prompt, system_prompt, json_mode, cfg):
        self.calls.append({"prompt": prompt, "system": system_prompt, "json": json_mode, "cfg": cfg})
        if self.fail:
            raise self.fail
        return self.content


@pytest.fixture
def dead_ollama(monkeypatch):
    monkeypatch.setattr(oc, "get_ollama_client", lambda: _DeadOllama())
    oc._model_cache.update(target=None, resolved=None, ts=0.0)


@pytest.fixture
def good_ollama(monkeypatch):
    monkeypatch.setattr(oc, "get_ollama_client", lambda: _GoodOllama())
    oc._model_cache.update(target=None, resolved=None, ts=0.0)


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture
def no_key(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def _cfg(monkeypatch, **over):
    base = {"provider": "ollama", "anthropic_model": "claude-fable-5-1",
            "allow_anthropic_fallback": False, "allow_openai_fallback": False}
    base.update(over)
    monkeypatch.setattr(oc, "_llm_cfg", lambda: base)


def test_anthropic_primary_serves_json(monkeypatch, with_key, dead_ollama):
    _cfg(monkeypatch, provider="anthropic")
    fake = _FakeClaude()
    monkeypatch.setattr(oc, "anthropic_chat", fake)
    out = oc.generate_json("rank this", system_prompt="You score jobs.", max_retries=1)
    assert out == {"from": "anthropic"}
    assert fake.calls[0]["json"] is True
    assert fake.calls[0]["cfg"]["anthropic_model"] == "claude-fable-5-1"


def test_anthropic_primary_serves_text(monkeypatch, with_key, dead_ollama):
    _cfg(monkeypatch, provider="anthropic")
    fake = _FakeClaude(content="A cover letter.")
    monkeypatch.setattr(oc, "anthropic_chat", fake)
    assert oc.generate_text("write") == "A cover letter."
    assert fake.calls[0]["json"] is False


def test_refusal_falls_back_to_ollama(monkeypatch, with_key, good_ollama):
    _cfg(monkeypatch, provider="anthropic")
    fake = _FakeClaude(fail=ConnectionError("Anthropic refused (category=cyber)"))
    monkeypatch.setattr(oc, "anthropic_chat", fake)
    assert oc.generate_json("x", max_retries=1) == {"from": "ollama"}


def test_key_alone_is_not_permission(monkeypatch, with_key, dead_ollama):
    _cfg(monkeypatch)  # provider ollama, no fallback flag
    monkeypatch.setattr(oc, "anthropic_chat",
                        lambda *a, **k: pytest.fail("must not call Anthropic"))
    with pytest.raises(ConnectionError):
        oc.generate_json("x", max_retries=1)


def test_explicit_fallback_catches_ollama_outage(monkeypatch, with_key, dead_ollama):
    _cfg(monkeypatch, allow_anthropic_fallback=True)
    fake = _FakeClaude(content='noise {"a": 1} trailing')
    monkeypatch.setattr(oc, "anthropic_chat", fake)
    assert oc.generate_json("x", max_retries=1) == {"a": 1}


def test_primary_without_key_uses_ollama_silently(monkeypatch, no_key, good_ollama):
    _cfg(monkeypatch, provider="anthropic")
    monkeypatch.setattr(oc, "anthropic_chat",
                        lambda *a, **k: pytest.fail("must not call Anthropic"))
    assert oc.generate_json("x", max_retries=1) == {"from": "ollama"}


def test_llm_status_reports_claude(monkeypatch, with_key):
    _cfg(monkeypatch, provider="anthropic")
    st = oc.llm_status()
    assert st["provider"] == "anthropic"
    assert st["model"] == "claude-fable-5-1"
    assert st["anthropic_ready"] is True
