"""Tests for native OpenAI Responses API passthrough."""

import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

from native_responses import litellm_alias_for, resolve_native_responses


def test_fallback_alias_uses_the_litellm_deployment_name():
    """The native path sends the upstream id; LiteLLM needs its own name.

    Without this the fallback (native /responses 5xx) forwards `gpt-6.1-sol`
    to LiteLLM, which does not serve that name, and the user sees a 400
    "Invalid model name" instead of the relay's actual failure.
    """
    assert litellm_alias_for("gpt-6.1-sol") == "qpcmqam-gpt-6.1-sol"
    assert litellm_alias_for("gpt-6.1-terra") == "qpcmqam-gpt-6.1-terra"
    assert litellm_alias_for("gemini-4-argon") == "qpcmqam-gemini-4-argon"
    assert litellm_alias_for("pro[1m]") == "qpcmqam-gpt-6.1-sol"
    assert litellm_alias_for("main") is None
    assert litellm_alias_for(None) is None


def test_resolve_pro():
    route = resolve_native_responses("pro", "/v1/responses")
    assert route is not None
    url, token, real_model = route
    assert url == "https://ai.qpcmqam.cn/v1/responses"
    assert real_model == "gpt-6.1-sol"
    assert token.startswith("sk-")


def test_resolve_pro_bracketed():
    route = resolve_native_responses("pro[1m]", "/v1/responses")
    assert route is not None
    url, token, real_model = route
    assert url == "https://ai.qpcmqam.cn/v1/responses"
    assert real_model == "gpt-6.1-sol"


def test_resolve_qpcmqam_models():
    route_sol = resolve_native_responses("qpcmqam-gpt-6.1-sol", "/v1/responses")
    assert route_sol is not None
    assert route_sol[2] == "gpt-6.1-sol"

    route_terra = resolve_native_responses("qpcmqam-gpt-6.1-terra", "/v1/responses")
    assert route_terra is not None
    assert route_terra[2] == "gpt-6.1-terra"

    route_argon = resolve_native_responses("gemini-4-argon", "/v1/responses")
    assert route_argon is not None
    assert route_argon[2] == "gemini-4-argon"


def test_non_responses_path_ignored():
    assert resolve_native_responses("pro", "/v1/chat/completions") is None
    assert resolve_native_responses("pro", "/v1/models") is None


def test_non_native_models_ignored():
    assert resolve_native_responses("main", "/v1/responses") is None
    assert resolve_native_responses("flash", "/v1/responses") is None
    assert resolve_native_responses("unknown-model", "/v1/responses") is None
