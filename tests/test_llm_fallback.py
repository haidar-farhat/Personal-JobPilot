"""LLM provider chain: local-first, explicit cloud authorization, no network."""

import httpx
import pytest

import utils.ollama_client as oc


class _DeadOllama:
    """Stub client: unreachable for both list() and chat()."""
    def list(self):
        raise httpx.ConnectError("ollama down")
    def chat(self, **kwargs):
        raise httpx.ConnectError("ollama down")


class _GoodOllama:
    def list(self):
        raise httpx.ConnectError("skip resolve")  # _resolve_model swallows this
    def chat(self, **kwargs):
        class Msg:  # mimics ollama's response object
            content = '{"from": "ollama"}'
        class Resp:
            message = Msg()
        return Resp()


class _FakeHTTP:
    """Captures the OpenAI request; returns a canned completion."""
    def __init__(self, content='{"from": "openai"}', status=200):
        self.content, self.status, self.calls = content, status, []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "body": json})
        fake = self
        class R:
            status_code = fake.status
            text = "err body"
            def json(self):
                return {"choices": [{"message": {"content": fake.content}}]}
        return R()


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
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture
def openai_primary(monkeypatch):
    monkeypatch.setattr(oc, "_llm_cfg",
                        lambda: {"provider": "openai", "openai_model": "gpt-5.6-terra"})


@pytest.fixture
def ollama_primary(monkeypatch):
    monkeypatch.setattr(oc, "_llm_cfg",
                        lambda: {"provider": "ollama", "openai_model": "gpt-5.6-terra",
                                 "allow_openai_fallback": False})


@pytest.fixture
def ollama_with_authorized_fallback(monkeypatch):
    monkeypatch.setattr(oc, "_llm_cfg",
                        lambda: {"provider": "ollama", "openai_model": "gpt-5.6-terra",
                                 "allow_openai_fallback": True})


# ---- openai-primary (the shipped default) ----

def test_openai_primary_serves_json(openai_primary, with_key, dead_ollama, monkeypatch):
    fake = _FakeHTTP()
    monkeypatch.setattr(oc.requests, "post", fake.post)
    out = oc.generate_json("rank this JSON", system_prompt="You output JSON.",
                           max_retries=1)
    assert out == {"from": "openai"}
    call = fake.calls[0]
    assert call["url"].endswith("/chat/completions")
    assert call["body"]["model"] == "gpt-5.6-terra"
    assert call["body"]["response_format"] == {"type": "json_object"}
    assert call["headers"]["Authorization"] == "Bearer sk-test-not-real"


def test_openai_primary_serves_text(openai_primary, with_key, dead_ollama, monkeypatch):
    fake = _FakeHTTP(content="A tailored cover letter.")
    monkeypatch.setattr(oc.requests, "post", fake.post)
    assert oc.generate_text("write a cover letter") == "A tailored cover letter."
    assert "response_format" not in fake.calls[0]["body"]


def test_openai_primary_failure_falls_back_to_ollama(openai_primary, with_key,
                                                     good_ollama, monkeypatch):
    fake = _FakeHTTP(status=500)
    monkeypatch.setattr(oc.requests, "post", fake.post)
    assert oc.generate_json("x JSON", max_retries=1) == {"from": "ollama"}


def test_openai_primary_without_key_uses_ollama_silently(openai_primary, no_key,
                                                         good_ollama, monkeypatch):
    monkeypatch.setattr(oc.requests, "post",
                        lambda *a, **k: pytest.fail("must not call OpenAI"))
    assert oc.generate_json("x JSON", max_retries=1) == {"from": "ollama"}


# ---- ollama-primary (provider: "ollama") ----

def test_ollama_primary_does_not_infer_cloud_permission_from_key(
        ollama_primary, with_key, dead_ollama, monkeypatch):
    monkeypatch.setattr(oc.requests, "post",
                        lambda *a, **k: pytest.fail("must not call OpenAI"))
    with pytest.raises(ConnectionError):
        oc.generate_json("x", max_retries=1)


def test_ollama_explicit_fallback_can_use_openai(
        ollama_with_authorized_fallback, with_key, dead_ollama, monkeypatch):
    fake = _FakeHTTP()
    monkeypatch.setattr(oc.requests, "post", fake.post)
    assert oc.generate_json("x JSON", max_retries=1) == {"from": "openai"}


def test_ollama_primary_no_key_means_original_error(ollama_primary, no_key,
                                                    dead_ollama, monkeypatch):
    monkeypatch.setattr(oc.requests, "post",
                        lambda *a, **k: pytest.fail("must not call OpenAI"))
    with pytest.raises(ConnectionError):
        oc.generate_json("x", max_retries=1)


def test_ollama_primary_healthy_never_calls_openai(ollama_primary, with_key,
                                                   good_ollama, monkeypatch):
    monkeypatch.setattr(oc.requests, "post",
                        lambda *a, **k: pytest.fail("must not call OpenAI"))
    assert oc.generate_json("x", max_retries=1) == {"from": "ollama"}


def test_both_dead_reraises_ollama_error(ollama_with_authorized_fallback, with_key,
                                         dead_ollama, monkeypatch):
    fake = _FakeHTTP(status=500)
    monkeypatch.setattr(oc.requests, "post", fake.post)
    with pytest.raises(ConnectionError) as exc:
        oc.generate_json("x", max_retries=1)
    assert "Ollama unreachable" in str(exc.value)  # original error, not the 500


# ---- shared plumbing ----

def test_fallback_json_recovers_noisy_output(openai_primary, with_key,
                                             dead_ollama, monkeypatch):
    fake = _FakeHTTP(content='noise {"a": 1} trailing')
    monkeypatch.setattr(oc.requests, "post", fake.post)
    assert oc.generate_json("x JSON", max_retries=1) == {"a": 1}


def test_json_mode_injects_json_word(openai_primary, with_key,
                                     dead_ollama, monkeypatch):
    fake = _FakeHTTP()
    monkeypatch.setattr(oc.requests, "post", fake.post)
    oc.generate_json("rank this role", system_prompt="", max_retries=1)
    sys_msgs = [m for m in fake.calls[0]["body"]["messages"] if m["role"] == "system"]
    assert sys_msgs and "JSON" in sys_msgs[0]["content"]


def test_llm_status_reflects_provider(openai_primary, with_key, no_key, monkeypatch):
    # no_key ran last → key removed
    assert oc.llm_status()["provider"] == "ollama"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    st = oc.llm_status()
    assert st["provider"] == "openai" and st["model"] == "gpt-5.6-terra"
