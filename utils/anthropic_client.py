"""Claude (Anthropic) provider for the JobPilot LLM chain — opt-in.

Selected from settings.yaml `llm` (provider: "anthropic", or
allow_anthropic_fallback: true) — see utils/ollama_client.py for the chain.
Credentials come only from the environment (ANTHROPIC_API_KEY, or an
`ant auth login` profile the SDK resolves itself); never from settings.yaml.

Claude Fable 5.1 specifics baked in: thinking is always on (no `thinking`
param), no assistant prefill (JSON mode is an instruction + brace extraction
in the caller), sampling params are rejected (none sent), and a policy
refusal is an HTTP 200 with stop_reason "refusal" — raised here so the chain
falls through to local Ollama like any other provider failure.
"""

import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-fable-5-1"
_FALLBACK_BETA = "server-side-fallback-2026-06-01"
_FALLBACKS = [{"model": "claude-opus-4-8"}]   # the server re-runs a refused request here


def anthropic_key_present() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def anthropic_chat(prompt: str, system_prompt: str, json_mode: bool, cfg: dict) -> str:
    """One Claude completion. Raises ConnectionError on refusal or API failure."""
    import anthropic  # optional dependency — imported only when the provider is on

    model = cfg.get("anthropic_model", DEFAULT_MODEL)
    system = system_prompt or ""
    if json_mode:
        system = (system + "\nRespond with a single JSON object and nothing else — "
                  "no prose, no code fences.").strip()
    kwargs = dict(
        model=model,
        max_tokens=int(cfg.get("anthropic_max_tokens", 4096)),
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": cfg.get("anthropic_effort", "medium")},
    )
    if system:
        kwargs["system"] = system

    client = anthropic.Anthropic(timeout=float(cfg.get("anthropic_timeout", 180)))
    try:
        resp = client.beta.messages.create(betas=[_FALLBACK_BETA], fallbacks=_FALLBACKS, **kwargs)
    except anthropic.APIError as e:
        raise ConnectionError(f"Anthropic API error: {e}") from e

    if resp.stop_reason == "refusal":
        det = getattr(resp, "stop_details", None)
        raise ConnectionError(
            f"Anthropic refused (category={getattr(det, 'category', None)}): "
            f"{getattr(det, 'explanation', '') or ''}"
        )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if not text:
        raise ConnectionError(f"Anthropic returned no text (stop_reason={resp.stop_reason})")
    logger.info(f"[llm] served by Anthropic ({resp.model})")
    return text
