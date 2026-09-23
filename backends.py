"""
backends.py — LLM backend registry and fallback logic.

To add or reorder backends, edit BACKEND_PIPELINE.
The agent will try each entry in order, top to bottom.
"""

import os
import json
import logging
import requests


# -----------------------------------
# LOGGER
# -----------------------------------
# This module just declares a logger — it never configures it.
# Configuration (level, format, handlers) is done once in main.py,
# cli.py, or app.py at startup via logging.basicConfig() or similar.
#
# To silence this module from the outside:
#   logging.getLogger("backends").setLevel(logging.WARNING)
#
# To silence everything:
#   logging.disable(logging.CRITICAL)

logger = logging.getLogger(__name__)  # resolves to "backends"


# -----------------------------------
# BACKEND DEFINITIONS
# -----------------------------------
# Each backend is a dict with:
#   name        : display label shown in logs and the "Answered by" footer
#   url         : OpenAI-compatible chat completions endpoint
#   model       : model string sent in the request payload
#   headers     : callable () -> dict  (keeps secrets out of module-level state)
#   timeout     : seconds before giving up on this backend (default 60)
#   requires_env: optional env var name — backend is skipped entirely if the
#                 var is missing or empty (useful to skip paid APIs in local dev)
#   on_success  : optional callable(backend) called after a successful response
#                 (e.g. Ollama uses this to unload the model from RAM)

def _openrouter_headers():
    return {
        "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY', '')}",
        "Content-Type": "application/json",
    }

def _ollama_headers():
    return {"Content-Type": "application/json"}

def _ollama_unload(backend):
    """Release Ollama model from RAM after a successful response."""
    try:
        requests.post(
            url="http://localhost:11434/api/generate",
            json={"model": backend["model"], "keep_alive": 0},
            timeout=10,
        )
        logger.debug("Ollama: model unloaded from RAM")
    except Exception:
        pass  # Non-critical


# -----------------------------------
# PIPELINE — edit this to add/reorder/remove backends
# -----------------------------------
BACKEND_PIPELINE = [
    {
        "name": "OpenRouter / gpt-oss-120b (free)",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "openai/gpt-oss-120b:free",
        "headers": _openrouter_headers,
        "timeout": 60,
        "requires_env": "OPENROUTER_API_KEY",
    },
    {
        "name": "OpenRouter / Z.ai: GLM 4.5 Air (free)",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "z-ai/glm-4.5-air:free",
        "headers": _openrouter_headers,
        "timeout": 60,
        "requires_env": "OPENROUTER_API_KEY",
    },
    {
        "name": "OpenRouter / Google: Gemma 4 31B (free)",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "google/gemma-4-31b-it:free",
        "headers": _openrouter_headers,
        "timeout": 60,
        "requires_env": "OPENROUTER_API_KEY",
    },
    # Example: official provider (e.g. Mistral AI directly)
    # {
    #     "name": "Mistral AI / mistral-small",
    #     "url": "https://api.mistral.ai/v1/chat/completions",
    #     "model": "mistral-small-latest",
    #     "headers": lambda: {
    #         "Authorization": f"Bearer {os.getenv('MISTRAL_API_KEY', '')}",
    #         "Content-Type": "application/json",
    #     },
    #     "timeout": 60,
    #     "requires_env": "MISTRAL_API_KEY",
    # },
    {
        "name": f"Ollama / {os.getenv('OLLAMA_MODEL', 'qwen2.5:7b')}",
        "url": "http://localhost:11434/v1/chat/completions",
        "model": os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
        "headers": _ollama_headers,
        "timeout": 180,  # Local models are slower; allow up to 3 minutes
        "on_success": _ollama_unload,
    },
]


# -----------------------------------
# BACKEND RUNNER
# -----------------------------------

# How long a failed backend is skipped before being retried (seconds).
FAILURE_COOLDOWN = 300  # 5 minutes

# Registry: backend name -> timestamp of last failure (module-level, persists
# across calls for the lifetime of the process).
_failure_times: dict[str, float] = {}


def _is_available(backend: dict) -> bool:
    """Return False if a required env var is missing."""
    env_var = backend.get("requires_env")
    if env_var and not os.getenv(env_var):
        return False
    return True


def _is_on_cooldown(backend: dict) -> bool:
    """Return True if this backend failed recently and should be skipped."""
    import time
    failed_at = _failure_times.get(backend["name"])
    if failed_at is None:
        return False
    elapsed = time.time() - failed_at
    return elapsed < FAILURE_COOLDOWN


def _mark_failed(backend: dict):
    import time
    _failure_times[backend["name"]] = time.time()


def _mark_recovered(backend: dict):
    """Clear cooldown on success so the backend is tried first next time."""
    _failure_times.pop(backend["name"], None)


def _cooldown_remaining(backend: dict) -> int:
    """Seconds left on this backend's cooldown (0 if none)."""
    import time
    failed_at = _failure_times.get(backend["name"])
    if failed_at is None:
        return 0
    return max(0, int(FAILURE_COOLDOWN - (time.time() - failed_at)))


def _call_llm(messages: list, tools: list, backend: dict) -> tuple[dict, int]:
    """
    Make a single LLM API call.
    Returns (response_data, http_status_code).
    Never raises — errors are encoded in the returned dict.
    """
    payload = {
        "model": backend["model"],
        "messages": messages,
        "tools": tools,
    }
    timeout = backend.get("timeout", 60)
    try:
        response = requests.post(
            url=backend["url"],
            headers=backend["headers"](),
            json=payload,
            timeout=timeout,
        )
        return response.json(), response.status_code
    except requests.exceptions.ReadTimeout:
        return {"error": {"message": f"Request timed out after {timeout}s", "code": "timeout"}}, 408
    except requests.exceptions.ConnectionError:
        return {"error": {"message": f"Could not connect to {backend['url']} — is the service running?", "code": "connection_error"}}, 503
    except Exception as e:
        return {"error": {"message": str(e), "code": "unknown"}}, 500


def _is_error(data: dict) -> bool:
    return "error" in data or "choices" not in data


def _log_failure(backend: dict, data: dict, status_code: int):
    error_obj = data.get("error", {})
    message   = error_obj.get("message", "unexpected response format")
    code      = error_obj.get("code") or error_obj.get("type") or "N/A"
    raw_snip  = json.dumps(data)[:300]
    logger.error(
        "FAILED: %s | HTTP %s | code: %s | message: %s | raw: %s",
        backend["name"], status_code, code, message, raw_snip,
    )


def call_with_fallback(messages: list, tools: list) -> tuple[dict, int, dict]:
    """
    Try each backend in BACKEND_PIPELINE in order, skipping any that are
    on cooldown from a recent failure. Falls back through the full list
    if needed (including cooldown backends as a last resort).

    Returns (response_data, status_code, winning_backend).
    """
    available = [b for b in BACKEND_PIPELINE if _is_available(b)]

    if not available:
        logger.critical("No backends available. Check your env vars and pipeline config.")
        return (
            {"error": {"message": "No backends available. Check your env vars and pipeline config.", "code": "no_backends"}},
            503,
            {"name": "none"},
        )

    # Split into ready (not on cooldown) and cooling down, preserving order.
    ready   = [b for b in available if not _is_on_cooldown(b)]
    skipped = [b for b in available if _is_on_cooldown(b)]

    if skipped:
        names = ", ".join(f"{b['name']} ({_cooldown_remaining(b)}s left)" for b in skipped)
        logger.warning("Skipping backends on cooldown: %s", names)

    # If everything is on cooldown, try them all anyway rather than giving up.
    candidates = ready if ready else available
    if not ready:
        logger.warning("All backends on cooldown — retrying anyway")

    last_data, last_status, last_backend = None, None, None

    for i, backend in enumerate(candidates):
        logger.debug("Trying backend %d/%d: %s", i + 1, len(candidates), backend["name"])
        data, status_code = _call_llm(messages, tools, backend)

        if _is_error(data):
            _log_failure(backend, data, status_code)
            _mark_failed(backend)
            last_data, last_status, last_backend = data, status_code, backend
            if i < len(candidates) - 1:
                logger.debug("Falling back to next backend")
            continue

        # Success — clear any prior cooldown so it's tried first next time.
        _mark_recovered(backend)
        logger.info("Backend responded OK: %s", backend["name"])
        return data, status_code, backend

    # All candidates failed.
    logger.error("All backends exhausted — returning last error")
    return last_data, last_status, last_backend