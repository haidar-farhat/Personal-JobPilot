"""Ollama API wrapper for JobPilot LLM calls."""

import json
import logging
import time
from pathlib import Path

import yaml
import ollama


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


def generate_json(prompt: str, system_prompt: str = "", max_retries: int = 3) -> dict:
    """Send a prompt to Ollama and parse the JSON response.

    Args:
        prompt: The user prompt to send.
        system_prompt: Optional system prompt for context.
        max_retries: Number of retries if JSON parsing fails.

    Returns:
        Parsed JSON dict from the model response.

    Raises:
        ConnectionError: After all retries are exhausted on network failure.
        ValueError: After all retries are exhausted on JSON parse failure.
    """
    config = _load_config()
    ollama_config = config.get("ollama", {})
    model = ollama_config.get("model", "gemma3:27b")
    num_ctx = ollama_config.get("num_ctx", 16384)

    client = get_ollama_client()

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
                options={"temperature": 0.3, "num_predict": 4096, "num_ctx": num_ctx},
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


def generate_text(prompt: str, system_prompt: str = "") -> str:
    """Send a prompt to Ollama and return plain text response.

    Args:
        prompt: The user prompt to send.
        system_prompt: Optional system prompt for context.

    Returns:
        The model's text response.
    """
    config = _load_config()
    ollama_config = config.get("ollama", {})
    model = ollama_config.get("model", "gemma3:27b")
    num_ctx = ollama_config.get("num_ctx", 16384)

    client = get_ollama_client()

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    response = client.chat(
        model=model,
        messages=messages,
        options={"temperature": 0.4, "num_predict": 4096, "num_ctx": num_ctx},
    )
    return response.message.content.strip()


def check_ollama_health() -> bool:
    """Check if Ollama is running and the model is available."""
    try:
        client = get_ollama_client()
        models = client.list()
        config = _load_config()
        target_model = config.get("ollama", {}).get("model", "gemma3:27b")
        available = [m.model for m in models.models]
        return any(target_model in name for name in available)
    except Exception:
        return False
