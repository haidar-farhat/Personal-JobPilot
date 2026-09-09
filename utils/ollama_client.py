"""LLM client for JobPilot: local Ollama first, optional OpenAI / Claude cloud.

Every LLM consumer (autofill essays, cover letters, tailoring, ranking) routes
through generate_json / generate_text here, so the provider chain covers all
of them. A cloud provider is used only when settings.yaml `llm` selects it as
primary or explicitly allows it as fallback, AND its key is in the environment
(OPENAI_API_KEY / ANTHROPIC_API_KEY — never in the tracked settings.yaml).
"""

import json
import logging
import os
import time
from pathlib import Path

import requests
import yaml
import ollama

from utils.anthropic_client import (anthropic_chat, anthropic_key_present,
                                    DEFAULT_MODEL as ANTHROPIC_DEFAULT_MODEL)


logger = logging.getLogger(__name__)


# httpx is bundled with the ollama Python client; we catch its connection-level
# exceptions explicitly so the ranker/tailor degrades gracefully when ollama is
# briefly down (mid-restart, model swap, etc.) instead of crashing the whole tick.
try:
    import httpx
    _CONNECTION_ERRORS = (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError)
except ImportError:  # pragma: no cover — defensive
    _CONNECTION_ERRORS = ()


def _load_config():
    config_path = Path(__file__).parent.parent / "config" / "settings.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_ollama_client():
    """Get configured Ollama client."""
    config = _load_config()
    ollama_config = config.get("ollama", {})
    return ollama.Client(host=ollama_config.get("base_url", "http://localhost:11434"))


_model_cache: dict = {"target": None, "resolved": None, "ts": 0.0}


def _resolve_model(client, target: str) -> str:
    """The configured model if installed, else the closest available one
    (same family preferred, ':latest' first). A missing model must degrade to
    a working one — not silently fail every LLM call (2026-07-05: configured
    gemma4:e4b wasn't pulled here, so every essay draft fell back to templates)."""
    now = time.time()
    if _model_cache["target"] == target and _model_cache["resolved"] and now - _model_cache["ts"] < 300:
        return _model_cache["resolved"]
    resolved = target
    try:
        available = [m.model for m in client.list().models]
        if available and not any(n == target or n.startswith(target) for n in available):
            family = target.split(":")[0]
            fam = sorted((n for n in available if n.startswith(family)),
                         key=lambda n: (not n.endswith(":latest"), n))
            resolved = fam[0] if fam else available[0]
            logger.warning(f"[ollama] configured model {target!r} not installed; using {resolved!r}")
    except Exception:
        pass  # ollama unreachable — let the caller's own error handling report it
    _model_cache.update(target=target, resolved=resolved, ts=now)
    return resolved


def _speed_options(ollama_config: dict, temperature: float, num_ctx: int,
                   profile: str | None = None, seed: int | None = None) -> dict:
    """Sampling options, with speed knobs from settings.yaml `ollama:`.

    num_predict caps how many tokens are generated — the single biggest lever
    on wall-clock time, since generation dominates. top_k/top_p narrow the
    sampler (less deliberation per token). Lower values = faster, less careful.

    PRECEDENCE: named profile > global config > the caller's argument. The
    global config used to beat the caller unconditionally, which made per-call
    sampling impossible: the tailor asked for a warmer temperature and silently
    got the global 0.2, so a near-greedy sampler decoded a prompt that is ~84%
    identical between any two jobs and produced near-identical résumés by
    construction. Raising the global instead would degrade the ranker's JSON
    scoring and the autofill essays, so the knob has to be per-call.
    """
    prof = (ollama_config.get("profiles") or {}).get(profile or "", {}) or {}
    opts = {
        "temperature": prof.get("temperature",
                                ollama_config.get("temperature", temperature)),
        "num_predict": prof.get("num_predict", ollama_config.get("num_predict", 4096)),
        "num_ctx": prof.get("num_ctx", num_ctx),
    }
    for k in ("top_k", "top_p", "num_gpu", "num_thread", "num_batch", "seed"):
        if k in prof:
            opts[k] = prof[k]
        elif k in ollama_config:
            opts[k] = ollama_config[k]
    if seed is not None:          # explicit seed wins over profile/config
        opts["seed"] = seed
    return opts


def _ollama_generate_json(prompt: str, system_prompt: str = "", max_retries: int = 3,
                          *, profile: str | None = None, seed: int | None = None) -> dict:
    config = _load_config()
    ollama_config = config.get("ollama", {})
    prof_cfg = (ollama_config.get("profiles") or {}).get(profile or "", {}) or {}
    # A profile may name its own model: the tailor benefits from a larger model's
    # instruction-following (bullet-length compliance), while the ranker's cheap
    # JSON scoring does not and would just get slower.
    model = prof_cfg.get("model") or ollama_config.get("model", "gemma3:27b")
    num_ctx = prof_cfg.get("num_ctx", ollama_config.get("num_ctx", 16384))

    client = get_ollama_client()
    model = _resolve_model(client, model)

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    last_content = ""
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            response = client.chat(
                model=model,
                messages=messages,
                options=_speed_options(ollama_config, 0.3, num_ctx, profile, seed),
                format="json",
            )
            last_content = response.message.content.strip()
            return json.loads(last_content)
        except json.JSONDecodeError as e:
            last_error = e
            # Try to recover by extracting the embedded JSON object
            if last_content:
                start = last_content.find("{")
                end = last_content.rfind("}") + 1
                if start >= 0 and end > start:
                    try:
                        return json.loads(last_content[start:end])
                    except json.JSONDecodeError:
                        pass
            if attempt < max_retries - 1:
                time.sleep(1)
        except _CONNECTION_ERRORS as e:
            last_error = e
            if attempt < max_retries - 1:
                backoff = 2 ** attempt  # 1s, 2s, 4s
                logger.warning(f"[ollama] connection error (attempt {attempt + 1}/{max_retries}): {e} — retrying in {backoff}s")
                time.sleep(backoff)
            else:
                raise ConnectionError(f"Ollama unreachable after {max_retries} attempts: {e}") from e
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                raise

    # Exhausted retries on JSON failures
    raise ValueError(
        f"Failed to parse JSON after {max_retries} attempts. "
        f"Last error: {last_error}. Last response: {last_content[:500]}"
    )


def _ollama_generate_text(prompt: str, system_prompt: str = "",
                          *, profile: str | None = None, seed: int | None = None) -> str:
    config = _load_config()
    ollama_config = config.get("ollama", {})
    model = ollama_config.get("model", "gemma3:27b")
    num_ctx = ollama_config.get("num_ctx", 16384)

    client = get_ollama_client()
    model = _resolve_model(client, model)

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    response = client.chat(
        model=model,
        messages=messages,
        options=_speed_options(ollama_config, 0.4, num_ctx, profile, seed),
    )
    return response.message.content.strip()


# ============================================================
# OpenAI provider (official API, key from env — see settings.yaml `llm`)
# ============================================================

def _llm_cfg() -> dict:
    return _load_config().get("llm", {}) or {}


def _openai_ready() -> bool:
    """True only when cloud use is both configured and credentialed.

    Merely finding a key in the process environment is not permission to turn
    an Ollama outage into a paid network request. OpenAI remains available
    when explicitly selected as the primary provider, or when the operator
    opts into fallback with ``allow_openai_fallback``.
    """
    cfg = _llm_cfg()
    authorized = cfg.get("provider", "ollama") == "openai" or bool(
        cfg.get("allow_openai_fallback", False)
    )
    return authorized and bool(os.environ.get("OPENAI_API_KEY"))


def _anthropic_ready() -> bool:
    """Same contract as _openai_ready, for Claude (`llm` settings + ANTHROPIC_API_KEY)."""
    cfg = _llm_cfg()
    authorized = cfg.get("provider", "ollama") == "anthropic" or bool(
        cfg.get("allow_anthropic_fallback", False)
    )
    return authorized and anthropic_key_present()


_CLOUD = {"openai": _openai_ready, "anthropic": _anthropic_ready}


def _cloud_primary() -> str | None:
    """The cloud provider selected as primary, if it's actually credentialed."""
    name = _llm_cfg().get("provider", "ollama")
    return name if name in _CLOUD and _CLOUD[name]() else None


def _cloud_fallback() -> str | None:
    """A cloud provider authorized to catch an Ollama failure (first ready wins)."""
    return next((n for n, ready in _CLOUD.items() if ready()), None)


def _openai_primary() -> bool:
    return _cloud_primary() == "openai"


def _cloud_model(name: str) -> str:
    cfg = _llm_cfg()
    if name == "anthropic":
        return cfg.get("anthropic_model", ANTHROPIC_DEFAULT_MODEL)
    return cfg.get("openai_model", "gpt-5.6-terra")


def llm_status() -> dict:
    """What's actually serving requests right now (for /api/autofill/health)."""
    primary = _cloud_primary()
    return {
        "provider": primary or "ollama",
        "openai_ready": _openai_ready(),
        "anthropic_ready": _anthropic_ready(),
        "model": _cloud_model(primary) if primary else None,
    }


def llm_fallback_available() -> bool:
    """True when another provider can catch a failure of the primary."""
    return True if _cloud_primary() else _cloud_fallback() is not None  # ollama always installed here


def _cloud_chat(name: str, prompt: str, system_prompt: str, json_mode: bool,
                temperature: float) -> str:
    if name == "anthropic":
        return anthropic_chat(prompt, system_prompt, json_mode=json_mode, cfg=_llm_cfg())
    return _openai_chat(prompt, system_prompt, json_mode=json_mode, temperature=temperature)


def _cloud_json(name: str, prompt: str, system_prompt: str) -> dict:
    return _extract_json(_cloud_chat(name, prompt, system_prompt, json_mode=True, temperature=0.3))


def _openai_chat(prompt: str, system_prompt: str, json_mode: bool,
                 temperature: float) -> str:
    cfg = _llm_cfg()
    model = cfg.get("openai_model", "gpt-5.6-terra")
    messages = []
    sys_content = system_prompt or ""
    if json_mode and "json" not in (sys_content + prompt).lower():
        # OpenAI's json_object mode requires the word JSON in the messages
        sys_content = (sys_content + "\nRespond with a single JSON object.").strip()
    if sys_content:
        messages.append({"role": "system", "content": sys_content})
    messages.append({"role": "user", "content": prompt})
    body = {"model": model, "messages": messages, "temperature": temperature,
            "max_completion_tokens": 4096}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json=body, timeout=cfg.get("openai_timeout", 120),
    )
    if r.status_code != 200:
        raise ConnectionError(f"OpenAI fallback HTTP {r.status_code}: {r.text[:300]}")
    content = r.json()["choices"][0]["message"]["content"] or ""
    logger.info(f"[llm] served by OpenAI fallback ({model})")
    return content.strip()


def _extract_json(content: str) -> dict:
    """Parse a cloud completion as JSON, tolerating prose around the object."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(content[start:end])
        raise


def generate_json(prompt: str, system_prompt: str = "", max_retries: int = 3,
                  *, profile: str | None = None, seed: int | None = None) -> dict:
    """JSON completion via the configured provider chain (settings.yaml `llm`).

    provider "openai"/"anthropic" + key set: that cloud first, Ollama catches
    failures. Otherwise: Ollama first; a cloud provider is considered only when
    its key exists AND its ``allow_*_fallback`` flag is explicitly true.
    Raises ConnectionError/ValueError only after the whole chain failed.
    """
    primary = _cloud_primary()
    if primary:
        try:
            return _cloud_json(primary, prompt, system_prompt)
        except Exception as e:
            logger.warning(f"[llm] {primary} primary failed ({e}) — falling back to Ollama")
            return _ollama_generate_json(prompt, system_prompt, max_retries,
                                        profile=profile, seed=seed)
    try:
        return _ollama_generate_json(prompt, system_prompt, max_retries,
                                    profile=profile, seed=seed)
    except (ConnectionError, ValueError) as ollama_err:
        fallback = _cloud_fallback()
        if not fallback:
            raise
        logger.warning(f"[llm] ollama failed ({ollama_err}) — trying {fallback} fallback")
        try:
            return _cloud_json(fallback, prompt, system_prompt)
        except Exception as e:
            logger.error(f"[llm] {fallback} fallback also failed: {e}")
            raise ollama_err


def generate_text(prompt: str, system_prompt: str = "",
                  *, profile: str | None = None, seed: int | None = None) -> str:
    """Text completion via the configured provider chain (settings.yaml `llm`)."""
    primary = _cloud_primary()
    if primary:
        try:
            return _cloud_chat(primary, prompt, system_prompt, json_mode=False, temperature=0.4)
        except Exception as e:
            logger.warning(f"[llm] {primary} primary failed ({e}) — falling back to Ollama")
            return _ollama_generate_text(prompt, system_prompt, profile=profile, seed=seed)
    try:
        return _ollama_generate_text(prompt, system_prompt, profile=profile, seed=seed)
    except Exception as ollama_err:
        fallback = _cloud_fallback()
        if not fallback:
            raise
        logger.warning(f"[llm] ollama failed ({ollama_err}) — trying {fallback} fallback")
        try:
            return _cloud_chat(fallback, prompt, system_prompt, json_mode=False, temperature=0.4)
        except Exception as e:
            logger.error(f"[llm] {fallback} fallback also failed: {e}")
            raise ollama_err


def check_ollama_health() -> bool:
    """Check if Ollama is running with ANY model available — generation
    falls back to the closest installed model when the configured one is
    missing, so a name mismatch is degraded quality, not an outage."""
    try:
        client = get_ollama_client()
        return bool(client.list().models)
    except Exception:
        return False
