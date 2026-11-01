"""Native OpenAI Responses API passthrough for providers that support /v1/responses.

When a client (such as Codex CLI) calls POST /v1/responses for a model whose
upstream gateway natively implements the OpenAI Responses API (such as qpcmqam),
routing through LiteLLM's /v1/responses endpoint converts the request into
/v1/chat/completions, which drops reasoning deltas and buffers output into
coarse chunks (causing multi-second freezes and loss of streaming).

This module resolves models that have native /v1/responses support upstream,
allowing the watchdog to pass the request directly to the upstream Responses API.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from sse_finalize import norm_model_key
except ImportError:
    from bridge.sse_finalize import norm_model_key

_BRIDGE_DIR = Path(__file__).resolve().parent

# Curated native responses providers and their base endpoints
NATIVE_RESPONSES_PROVIDERS = {
    "qpcmqam": {
        "api_base": "https://ai.qpcmqam.cn/v1",
        "auth_env": "QPCMQAM_API_KEY",
        "models": {
            "gpt-6.1-sol": "gpt-6.1-sol",
            "gpt-6.1-terra": "gpt-6.1-terra",
            "gemini-4-argon": "gemini-4-argon",
        },
    }
}

# Model aliases and group mappings to native upstream (provider, real_model)
NATIVE_RESPONSES_ROUTING = {
    "pro": ("qpcmqam", "gpt-6.1-sol"),
    "qpcmqam-gpt-6.1-sol": ("qpcmqam", "gpt-6.1-sol"),
    "qpcmqam-gpt-6.1-terra": ("qpcmqam", "gpt-6.1-terra"),
    "qpcmqam-gemini-4-argon": ("qpcmqam", "gemini-4-argon"),
    "gpt-6.1-sol": ("qpcmqam", "gpt-6.1-sol"),
    "gpt-6.1-terra": ("qpcmqam", "gpt-6.1-terra"),
    "gemini-4-argon": ("qpcmqam", "gemini-4-argon"),
}


def resolve_native_responses(
    model_name: str,
    path: str = "/v1/responses",
) -> tuple[str, str, str] | None:
    """Resolve a model name to (target_url, auth_token, real_model) if supported.

    Returns None if the path is not a responses endpoint or the model is not
    natively served upstream by a Responses API provider.
    """
    clean_path = path.rstrip("/")
    if clean_path not in ("/v1/responses", "/responses"):
        return None

    key = norm_model_key(model_name)
    route = NATIVE_RESPONSES_ROUTING.get(key)
    if not route:
        return None

    provider_name, real_model = route
    provider_info = NATIVE_RESPONSES_PROVIDERS.get(provider_name)
    if not provider_info:
        return None

    auth_env = provider_info["auth_env"]
    token = os.environ.get(auth_env)
    if not token:
        try:
            from profile_credential import token_from_proxy_env
            token = token_from_proxy_env(auth_env)
        except ImportError:
            try:
                from bridge.profile_credential import token_from_proxy_env
                token = token_from_proxy_env(auth_env)
            except Exception:
                token = ""
        except Exception:
            token = ""

    if not token:
        return None

    target_url = f"{provider_info['api_base']}/responses"
    return target_url, token, real_model
