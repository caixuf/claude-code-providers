#!/usr/bin/env python3
"""Auto-discover model capabilities from providers and emit registry/models.yaml.

Instead of hand-filling context window / vision / output caps for every model,
this pulls them from, in priority order:

  1. The provider's own ``GET .../models`` endpoint (authoritative when present):
       - Cline       : id/name only
       - DeepSeek    : standard OpenAI /v1/models shape
       - MiniMax     : ids only, still listing the legacy M2 rungs — the
                       allow-list and per-model ``hints`` carry the facts
       - OpenDesign  : metadata.context_limit / context_budget
       - Qianfan     : context_length / max_completions_tokens /
                       architecture.input_modalities (the richest of the lot)
       - Trae        : no /models at all — trae_bridge.py's own catalogue RPC
   2. LiteLLM's bundled cost map (``litellm.model_cost``, ~4500 entries) for
      max_input_tokens / max_output_tokens / supports_vision.
   3. A tiny built-in defaults table for the few facts neither source knows.

Only the providers ccp currently keeps are registered here. Archived ones
(CommandCode, CodeBuddy, Copilot) are described in
docs/archived-providers.md; restoring one means putting its entry back and
re-running discovery.

Output is a *generated but committed* ``registry/models.yaml`` that a human can
still hand-edit; re-running merges (never clobbers manual ``aliases``/``tiers``).

Usage:
  python3 discover_models.py                  # refresh all providers into registry
  python3 discover_models.py --provider cline # just one
  python3 discover_models.py --dry-run        # print, don't write
  python3 discover_models.py --diff           # show what changed vs current file
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent
REPO_DIR = BRIDGE_DIR.parent
REGISTRY_PATH = REPO_DIR / "registry" / "models.yaml"

sys.path.insert(0, str(BRIDGE_DIR))
from loopback import ensure_loopback_bypass  # noqa: E402

# Provider definitions. ``auth_file`` points at the deployed litellm config or
# env so we can read the token without hard-coding secrets here.
PROVIDERS = {
    "cline": {
        "models_url": "https://api.cline.bot/api/v1/models",
        "api_base": "https://api.cline.bot/api/v1",
        "endpoint_kind": "openai",
        "litellm_prefix": "openai/",
        "auth_env": "CLINE_API_KEY",
        "auth_file": "~/.config/litellm/config.yaml",
        "auth_grep": "api.cline.bot",
        # ClinePass advertises 469 ids but its plan serves a small subset. Every
        # family it does NOT serve — Google (42 ids), OpenAI, Anthropic and xAI
        # included — answers `{"error":"model not found"}` when called, so the
        # catalogue is not a capability list. ``only_models`` keeps the registry
        # to the ids verified by direct probe; without it `ccp models` claims 469
        # models of which ~6 work.
        #
        # Verified 2026-10-14 against https://api.cline.bot/api/v1 with
        # `model: cline-pass/<id>`. The prefix is mandatory: without it the
        # gateway answers "invalid model format. Expected format: modelType/model".
        # Re-probe when the plan changes, and extend this list rather than
        # trusting the catalogue again.
        "only_models": [
            "deepseek/deepseek-v4.1-flash",
            "deepseek/deepseek-v4-pro",
            "z-ai/glm-5.3",
            "moonshotai/kimi-k3",
            "minimax/minimax-m3",
            "qwen/qwen3.7-max",
        ],
        "max_output_default": None,
    },
    # Native Anthropic-compatible endpoints (no bridge). Rich metadata where
    # the provider exposes it (DeepSeek gives context_window + input_modalities).
    "deepseek": {
        "models_url": "https://api.deepseek.com/models",
        "api_base": "https://api.deepseek.com/anthropic",
        "endpoint_kind": "anthropic",
        "auth_env": "DEEPSEEK_API_KEY",
        "profile_file": "~/.claude/providers/deepseek.json",
        "max_output_default": 393216,
    },
    # MiniMax keeps two models, and this is a curation decision the vendor backs:
    # its own Anthropic-SDK documentation says the Anthropic-compatible interface
    # supports MiniMax-M3.1-Flash and MiniMax-M3 only, with the older M2 rungs
    # reached through the standard MiniMax API instead.
    #
    # Two properties of this endpoint shape the discovery entry:
    #   * ``GET /v1/models`` returns ids and nothing else — no context window, no
    #     output cap, no modality flag — and it still lists the whole M2 family
    #     (nine ids). Read online, it would re-introduce every legacy rung, so
    #     ``only_models`` narrows it to the two current models.
    #   * because the catalogue carries no facts, the ones the registry needs
    #     cannot come from it either. LiteLLM's cost map gets M3's window and
    #     vision right but not its 512K output cap, and knows nothing about
    #     M3.1-Flash at all, so ``hints`` pins the measured values.
    #
    # The endpoint does not reject a name it does not serve: asking for
    # `MiniMax-M3.1` returns 200 and a completion from M3, and any unknown name
    # falls back to M3 the same way. `ccp_pick verify` is what catches that, by
    # reading the `model` field the response carries back.
    #
    # Measured 2026-10-20 against https://api.minimax.cn/anthropic:
    #   * ctx 1M — a ~950k-token prompt is accepted and answered by both models
    #     (`usage.input_tokens=950080`), matching the documented 1,000,000.
    #     `count_tokens` is not a way to probe the limit: above ~917k it returns
    #     `input_tokens: 0` instead of an error, and a real completion at 1.02M
    #     was silently truncated to 70k input tokens on M3.1-Flash — a request
    #     that succeeds is not proof it was read whole.
    #   * max_output 524288 — `max_tokens=524288` answers 200, `524289` answers
    #     `400 … does not support max tokens > 524288`. Both models.
    #   * vision — a 8x8 PNG pasted as an image block is read back correctly.
    #   * base URL is the host MiniMax's own Anthropic-SDK page prints.
    "minimax": {
        "models_url": "https://api.minimax.cn/v1/models",
        "api_base": "https://api.minimax.cn/anthropic",
        "endpoint_kind": "anthropic",
        # The M Plan subscription key is what this account actually runs on —
        # it is the one the profile and proxy.env carry.
        #
        # Not a capability boundary: measured 2026-10-20, the standard key
        # answers MiniMax-M3.1-Flash and MiniMax-M3 exactly as the plan key
        # does. It used to be one — the standard key answered a request for the
        # then-current `MiniMax-M3.1-Flash-Preview` with a completion from M3 —
        # which is why `ccp_pick verify` still keys its cache on the credential
        # digest rather than the model name alone.
        "auth_env": "MINIMAX_PLAN_API_KEY",
        "profile_file": "~/.claude/providers/minimax.json",
        "only_models": [
            "MiniMax-M3.1-Flash",
            "MiniMax-M3",
        ],
        # Per-model facts the catalogue does not publish. `hints` is the
        # discovery mechanism for exactly this shape: a value here survives a
        # refresh instead of being re-derived (and lost) from an empty entry.
        "hints": {
            "MiniMax-M3.1-Flash": {"ctx": 1000000, "max_output": 524288, "vision": True},
            "MiniMax-M3": {"ctx": 1000000, "max_output": 524288, "vision": True},
        },
        "max_output_default": None,
    },
    # OpenDesign (amr-link.open-design.ai) — an Anthropic-native aggregator, so
    # it needs no bridge at all. Its /v1/models is unusually good: every entry
    # carries metadata.context_limit (input window), metadata.context_budget
    # (output cap) and architecture.input_modalities, so nothing has to be
    # guessed or cross-checked against LiteLLM's cost map.
    "opendesign": {
        "models_url": "https://amr-link.open-design.ai/v1/models",
        "api_base": "https://amr-link.open-design.ai",
        "endpoint_kind": "anthropic",
        "auth_env": "OPENDESIGN_API_KEY",
        "profile_file": "~/.claude/providers/opendesign.json",
        "max_output_default": None,
    },
    # Baidu Qianfan (文心 / 千帆) Token Plan, personal tier. Standard
    # OpenAI-compatible Chat Completions, so it goes through the normal bridge
    # with no adapter. Two quirks, both verified against the live endpoint:
    #   * the Token Plan key is only accepted on the `/v2/tokenplan/personal/`
    #     prefix — the same key gets `token_plan_person_api_key_not_allowed`
    #     on plain `/v2/chat/completions`;
    #   * the catalogue lists ~35 models but Token Plan Person only serves the
    #     GLM / DeepSeek families; the ERNIE, Qwen, VL, OCR and embedding ids
    #     all answer `token_plan_person_model_not_supported`. ``only_models``
    #     narrows the registry to the verified set so no profile advertises a
    #     model this key cannot call.
    #
    # The verified set is re-derived per subscription, not inherited: the
    # 2026-10-24 key answers for nine ids where the previous one answered for
    # six — `deepseek-v4.1-flash-1017`, `deepseek-v4-pro-0813` and `glm-5.1`
    # are new. `bridge/qianfan_probe.py` walks the whole catalogue against a
    # key and prints the allow-list to paste here.
    "qianfan": {
        "models_url": "https://qianfan.baidubce.com/v2/models",
        "api_base": "https://qianfan.baidubce.com/v2/tokenplan/personal",
        "endpoint_kind": "openai",
        "litellm_prefix": "openai/",
        "auth_env": "QIANFAN_API_KEY",
        "auth_file": "~/.config/litellm/config.yaml",
        "auth_grep": "qianfan.baidubce.com",
        # Verified to answer on the Token Plan Person endpoint.
        "only_models": [
            "deepseek-v4.1-flash",
            "deepseek-v4.1-flash-1017",
            "deepseek-v4-pro",
            "deepseek-v4-pro-0813",
            "deepseek-v4-flash-0731",
            "glm-5.3",
            "glm-5.3-flash",
            "glm-5.2",
            "glm-5.1",
        ],
        "max_output_default": 32000,
    },
    # TraeCLI (ByteDance's enterprise coding agent). There is no public API and
    # no /models endpoint: `bridge/trae_bridge.py` owns the private
    # `llm_raw_chat` protocol and is what `ccp trae` points at. Discovery and
    # the runtime therefore share one catalogue source — the bridge's own
    # `get_config_list` call — via ``catalogue_command``. The PAT never has to
    # be duplicated here: the bridge reads it from proxy.env itself.
    "trae": {
        "models_url": None,
        "api_base": "http://127.0.0.1:4100",
        "endpoint_kind": "anthropic",
        "auth_env": "TRAECLI_PERSONAL_ACCESS_TOKEN",
        "catalogue_command": ["trae_bridge.py", "--list-models-json"],
        # Verified by needle-in-a-haystack probe through :4100 (2026-10-15):
        # 333k / 555k / 888k all retrieve a code placed at the very end of the
        # context; 1.25M returns 200 with empty content. Real window is 1M —
        # the same as every other provider here — not the 96k–184k Trae reports.
        "ctx_default": 1000000,
        "max_output_default": None,
    },
    # Codex (a local gateway on :56895). It speaks the Anthropic Messages API
    # itself, so this is `native: true` in the strictest sense: no bridge, no
    # clamps, the profile points straight at :56895.
    #
    # The loopback means discovery fails whenever the gateway is not running,
    # and `build()` then keeps the previous entries rather than emptying the
    # registry — that is the intended behaviour, not an accident.
    #
    # Measured 2026-10-23 against http://localhost:56895/v1:
    #   * ctx 272000 — the catalogue's own context_length, and the same for
    #     every model here.
    #   * every model is TEXT-ONLY (`input_modalities: ["text"]`); a pasted
    #     screenshot has nowhere to go.
    #   * `max_output` is left null: the gateway accepts max_tokens=300000 on a
    #     272k model and answers normally, so it does not enforce a cap and a
    #     number taken from it would be fiction.
    #   * two catalogue ids answer 503 `no_available_account`
    #     (gpt-image-2.5, gpt-reserve). They are excluded by `only_models`, the
    #     same rule ClinePass and MiniMax's legacy family are held to: the
    #     registry lists what has been verified to answer.
    "codex": {
        "models_url": "http://localhost:56895/v1/models",
        "api_base": "http://localhost:56895/v1",
        "endpoint_kind": "anthropic",
        "auth_env": "CODEX_API_KEY",
        "profile_file": "~/.claude/providers/codex.json",
        "only_models": [
            "gpt-6.1-sol",
            "gpt-6-astra",
            "gpt-6-sol",
            "gpt-6-luna",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "codex-auto-review",
        ],
        "max_output_default": None,
    },
}


def read_token(provider: dict) -> str | None:
    tok = os.environ.get(provider["auth_env"])
    if tok:
        return tok
    # 1. the secrets env file (where ccp render now stores keys)
    env_file = Path(os.path.expanduser("~/.config/litellm/proxy.env"))
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            if line.startswith(provider["auth_env"] + "="):
                return line.split("=", 1)[1].strip().strip("'\"")
    # 1b. the deployed native profile (keys live here for native providers)
    if provider.get("profile_file"):
        pf = Path(os.path.expanduser(provider["profile_file"]))
        if pf.is_file():
            try:
                import json as _j

                doc = _j.loads(pf.read_text())
                t = (doc.get("env") or {}).get("ANTHROPIC_AUTH_TOKEN")
                if t and "YOUR_" not in t:
                    return t
            except Exception:
                pass
    # 2. a legacy hand-edited config.yaml with a literal api_key
    fp = Path(os.path.expanduser(provider.get("auth_file", "")))
    if not fp.is_file():
        return None
    try:
        text = fp.read_text()
    except Exception:
        return None
    marker = provider["auth_grep"]
    idx = text.find(marker)
    if idx == -1:
        return None
    tail = text[idx:]
    for line in tail.splitlines()[1:]:
        s = line.strip()
        if s.startswith("api_key:"):
            val = s.split("api_key:", 1)[1].strip().strip("'\"")
            if not val.startswith("os.environ/"):
                return val
        if s.startswith("- model_name:") or s.startswith("api_base:"):
            if "api_key:" in s:
                val = s.split("api_key:", 1)[1].strip().strip("'\"")
                if not val.startswith("os.environ/"):
                    return val
    return None


def fetch_catalogue(command: list[str]) -> list[dict]:
    """Ask a provider's own adapter for its model list.

    Used by providers with no HTTP /models endpoint (Trae). The adapter owns the
    auth handshake, so discovery never needs the credential itself — it just
    runs the sibling script and reads JSON from stdout.
    """
    import subprocess

    script = BRIDGE_DIR / command[0]
    proc = subprocess.run(
        [sys.executable, str(script), *command[1:]],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "no output").strip()[:300])
    return json.loads(proc.stdout)


def fetch_models(url: str, token: str | None, json_path: tuple[str, ...] | None = None) -> list[dict]:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    # Some gateways (CommandCode) sit behind Cloudflare and 403 a bare
    # urllib agent; present a normal client UA.
    req.add_header("User-Agent", "ccp-discover/1.0 (+https://github.com/caixuf/claude-code-providers)")
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if json_path:
        # REST-envelope providers (CodeBuddy): {"data": {"models": [...]}}
        cur = data
        for key in json_path:
            if not isinstance(cur, dict) or key not in cur:
                return []
            cur = cur[key]
        return cur if isinstance(cur, list) else []
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    if isinstance(data, list):
        return data
    return []


def litellm_cost_map() -> dict:
    """Load LiteLLM's bundled capability map (best-effort, may be absent)."""
    try:
        sys.path.insert(0, str(Path.home() / ".local/share/litellm-venv/lib/python3.12/site-packages"))
        import litellm  # type: ignore

        return litellm.model_cost or {}
    except Exception:
        return {}


def build_suffix_index(cmap: dict) -> dict:
    """Map each trailing 1-2 segment path to a cost entry (first wins)."""
    idx: dict[str, dict] = {}
    for k, e in cmap.items():
        segs = k.split("/")
        for n in (1, 2):
            if len(segs) >= n:
                tail = "/".join(segs[-n:])
                idx.setdefault(tail, e)
    return idx


def cost_lookup(cmap: dict, candidates: list[str], suffix_idx: dict | None = None) -> dict:
    """Try several key spellings against the cost map, then fall back to a
    suffix match so e.g. ``deepseek/deepseek-v4.1-flash`` is found under
    LiteLLM's ``openrouter/deepseek/deepseek-v4.1-flash`` entry."""
    for key in candidates:
        e = cmap.get(key)
        if e:
            return _cost_fields(e)
    idx = suffix_idx if suffix_idx is not None else build_suffix_index(cmap)
    for cand in candidates:
        segs = cand.split("/")
        for n in (2, 1):
            if len(segs) >= n:
                e = idx.get("/".join(segs[-n:]))
                if e:
                    return _cost_fields(e)
    return {}


def _cost_fields(e: dict) -> dict:
    return {
        "ctx": e.get("max_input_tokens") or e.get("max_tokens"),
        "max_output": e.get("max_output_tokens") or e.get("max_tokens"),
        "vision": e.get("supports_vision"),
    }


def norm_deepseek(entry: dict, provider: dict, cmap: dict, idx: dict | None = None) -> dict:
    """Official DeepSeek exposes context_window / max_output_tokens /
    input_modalities (["text","image"] -> vision)."""
    mid = entry.get("id", "")
    mods = entry.get("input_modalities") or []
    info = cost_lookup(cmap, [mid, f"deepseek/{mid}"], idx)
    vision = "image" in mods if mods else info.get("vision")
    return {
        "name": mid,
        "ctx": entry.get("context_window") or info.get("ctx"),
        "max_output": entry.get("max_output_tokens") or provider["max_output_default"] or info.get("max_output"),
        "vision": vision,
        "endpoint_kind": "anthropic",
    }


def norm_generic(entry: dict, provider: dict, cmap: dict, idx: dict | None = None) -> dict:
    mid = entry.get("id", "")
    info = cost_lookup(cmap, [mid, f"openai/{mid}", mid.split("/", 1)[-1]], idx)
    return {
        "name": mid,
        "ctx": entry.get("context_length") or entry.get("max_input_tokens") or info.get("ctx"),
        "max_output": entry.get("max_output_tokens") or provider["max_output_default"] or info.get("max_output"),
        "vision": info.get("vision"),
        "endpoint_kind": provider["endpoint_kind"],
    }


def norm_codex(entry: dict, provider: dict, cmap: dict, idx: dict | None = None) -> dict:
    """The local Codex gateway publishes the catalogue's own facts.

    ``context_length`` is the window and ``model_info.input_modalities`` the
    modality list, so neither has to come from LiteLLM's cost map — which
    knows none of these ids and would leave all three null. Vision stays
    False when nothing is declared, because the field is always present here.
    """
    mid = entry.get("id", "")
    mods = (entry.get("model_info") or {}).get("input_modalities") or []
    return {
        "name": mid,
        "ctx": entry.get("context_length"),
        # Deliberately not from the cost map: it recognises some of these ids
        # as OpenAI models and answers 128000, while the gateway happily took a
        # max_tokens=300000 request. Publishing either number would be a
        # guess dressed as a fact.
        "max_output": provider["max_output_default"],
        "vision": "image" in mods,
        "endpoint_kind": provider["endpoint_kind"],
    }


def norm_qianfan(entry: dict, provider: dict, cmap: dict, idx: dict | None = None) -> dict:
    """Qianfan's /v2/models carries real capability metadata per model.

    Unlike most OpenAI-shaped catalogues this one publishes
    ``context_length`` / ``max_completions_tokens`` and
    ``architecture.input_modalities``, so nothing has to be guessed or
    cross-checked against LiteLLM's cost map. ``type: multimodal`` plus an
    ``image`` input modality is the vision signal.
    """
    arch = entry.get("architecture") or {}
    mods = arch.get("input_modalities") or []
    vision = "image" in mods if mods else None
    return {
        "name": entry.get("id", ""),
        "ctx": entry.get("context_length") or entry.get("prompt_tokens"),
        "max_output": (entry.get("max_completions_tokens")
                       or entry.get("max_tokens")
                       or provider.get("max_output_default")),
        "vision": vision,
        "endpoint_kind": provider["endpoint_kind"],
    }


def norm_opendesign(entry: dict, provider: dict, cmap: dict, idx: dict | None = None) -> dict:
    """OpenDesign exposes everything the registry needs inside the model entry.

    ``metadata.context_limit`` is the input window, ``metadata.context_budget``
    the output cap (verified by probe: asking for more than the budget is
    rejected), and ``architecture.input_modalities`` lists image/audio/video.
    Because the vendor states these itself there is no cost-map fallback here —
    an absent field stays absent instead of being filled with someone else's
    number.
    """
    meta = entry.get("metadata") or {}
    arch = entry.get("architecture") or {}
    mods = arch.get("input_modalities") or []
    return {
        "name": entry.get("id", ""),
        "ctx": meta.get("context_limit"),
        "max_output": meta.get("context_budget"),
        "vision": "image" in mods if mods else None,
        "endpoint_kind": "anthropic",
    }


def _guess_vision(mid: str) -> bool | None:
    m = mid.lower()
    if any(w in m for w in ("vision", "-vl", "omni", "gemini", "gpt-4o", "gpt-5", "claude-", "grok")):
        return True
    return None


def norm_trae(entry: dict, provider: dict, cmap: dict, idx: dict | None = None) -> dict:
    """TraeCLI's subscription catalogue.

    Trae publishes no /models endpoint; ``trae_bridge.py`` calls its private
    ``get_config_list`` RPC through the PAT and hands us one dict per model
    with ctx / max_output / vision already resolved from the catalogue's own
    ``prompt_max_tokens`` / ``max_tokens`` / ``multimodal`` fields.

    ``prompt_max_tokens`` is NOT the model's window — it is what Trae's own CLI
    is willing to put in one request. Every model reports 96k–184k that way,
    and taking it at face value cost us 5/6 of the real window: the same
    DeepSeek-V4.1-Flash answers a 333k-token prompt and finds a needle placed at
    its very end, and still answers at 888k tokens, while 1.25M returns HTTP 200
    with empty content and zero usage — a silent failure, not an error.

    So ctx comes from ``ctx_default`` below, and ``prompt_max_tokens`` is kept
    out of the registry entirely. Re-probe by moving the needle: a request that
    succeeds is not proof, only one that answers correctly from the far end of
    the context is.
    """
    return {
        "name": entry.get("config_name") or entry.get("id"),
        "ctx": provider.get("ctx_default") or entry.get("ctx"),
        "max_output": entry.get("max_output"),
        "vision": entry.get("vision"),
        "endpoint_kind": "anthropic",
    }


def norm_qianfan(entry: dict, provider: dict, cmap: dict, idx: dict | None = None) -> dict:
    """Qianfan's /v2/models carries real capability metadata per model.

    Unlike most OpenAI-shaped catalogues this one publishes
    ``context_length`` / ``max_completions_tokens`` and
    ``architecture.input_modalities``, so nothing has to be guessed or
    cross-checked against LiteLLM's cost map. ``type: multimodal`` plus an
    ``image`` input modality is the vision signal.
    """
    arch = entry.get("architecture") or {}
    mods = arch.get("input_modalities") or []
    vision = "image" in mods if mods else None
    return {
        "name": entry.get("id", ""),
        "ctx": entry.get("context_length") or entry.get("prompt_tokens"),
        "max_output": (entry.get("max_completions_tokens")
                       or entry.get("max_tokens")
                       or provider.get("max_output_default")),
        "vision": vision,
        "endpoint_kind": provider["endpoint_kind"],
    }


NORMALIZERS = {
    "cline": norm_generic,
    "deepseek": norm_deepseek,
    "minimax": norm_generic,
    "opendesign": norm_opendesign,
    "qianfan": norm_qianfan,
    "trae": norm_trae,
    "codex": norm_codex,
}

# Speed-tier variants that share every fact with their base model. Neither the
# provider endpoint nor LiteLLM's cost map names these, so a bare normalizer
# leaves ctx/max_output/vision null (e.g. MiniMax's "-highspeed" SKUs). We
# inherit from the base model instead of shipping — and testing — a null tail.
VARIANT_SUFFIXES = ("-highspeed",)


def fill_variant_siblings(models: list[dict]) -> None:
    """Mutate ``models`` in place: a ``<base><suffix>`` variant with no ctx
    inherits ctx/max_output/vision from ``<base>`` in the same provider."""
    by_name = {m["name"]: m for m in models}
    for m in models:
        if m.get("ctx"):
            continue
        for suffix in VARIANT_SUFFIXES:
            if not m["name"].endswith(suffix):
                continue
            base = by_name.get(m["name"][: -len(suffix)])
            if base and base.get("ctx"):
                for field in ("ctx", "max_output", "vision"):
                    if m.get(field) is None:
                        m[field] = base.get(field)
            break


def load_existing() -> dict:
    """Load current registry (manual aliases/tiers preserved across refreshes)."""
    if not REGISTRY_PATH.is_file():
        return {}
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    try:
        doc = yaml.safe_load(REGISTRY_PATH.read_text()) or {}
    except Exception:
        return {}
    out = {}
    for prov in doc.get("providers", []):
        for m in (prov.get("models") or []):
            out[(prov["name"], m["name"])] = m
    return out


def load_existing_by_provider() -> dict:
    """provider name -> its model list from the current registry file."""
    if not REGISTRY_PATH.is_file():
        return {}
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    try:
        doc = yaml.safe_load(REGISTRY_PATH.read_text()) or {}
    except Exception:
        return {}
    return {p["name"]: (p.get("models") or []) for p in doc.get("providers", [])}


def build(only: str | None) -> dict:
    cmap = litellm_cost_map()
    cidx = build_suffix_index(cmap)
    existing = load_existing()
    existing_by_prov = load_existing_by_provider()

    def provider_block(pname: str, pdef: dict, models: list) -> dict:
        return {
            "name": pname,
            "api_base": pdef["api_base"],
            "endpoint_kind": pdef["endpoint_kind"],
            "auth_env": pdef["auth_env"],
            "models": models,
        }

    # Seed from the current registry so `--provider X` never drops the others.
    #
    # Only providers this file still registers are seeded. A provider removed
    # from PROVIDERS (an archived one) must disappear from the registry on the
    # next refresh, not live on as an unreachable block with an empty api_base
    # and auth_env — that stale block is what a profile would then try to use.
    by_prov: dict[str, dict] = {}
    for pname, models in existing_by_prov.items():
        pdef = PROVIDERS.get(pname)
        if pdef is None:
            print(
                f"  - {pname}: in the registry but no longer in PROVIDERS; dropping it",
                file=sys.stderr,
            )
            continue
        by_prov[pname] = provider_block(pname, pdef, models)

    for pname, pdef in PROVIDERS.items():
        if only and pname != only:
            continue
        token = read_token(pdef)
        raw = None
        try:
            if pdef.get("catalogue_command"):
                raw = fetch_catalogue(pdef["catalogue_command"])
            else:
                raw = fetch_models(pdef["models_url"], token, pdef.get("models_json_path"))
        except Exception as e:
            print(f"  ! {pname}: fetch failed ({e}); keeping existing entries", file=sys.stderr)
        if raw is None:
            # keep the previously discovered models verbatim (don't lose data
            # just because a key is temporarily missing/rate-limited)
            prior_models = existing_by_prov.get(pname) or []
            by_prov[pname] = provider_block(pname, pdef, prior_models)
            continue
        # ids the gateway routes but this account's catalogue omits
        raw = list(raw) + list(pdef.get("extra_models") or [])
        # Some providers publish a catalogue wider than what is current or
        # reachable: ClinePass advertises 469 ids and serves 6, and MiniMax's
        # /v1/models still lists the whole legacy M2 family. ``only_models``
        # narrows the registry to the curated set, so an unusable or retired id
        # never reaches a profile where it would fail at request time. It is
        # also what keeps a refresh from re-introducing a rung that was dropped
        # on purpose.
        if pdef.get("only_models"):
            wanted = set(pdef["only_models"])
            # `name` covers the already-normalized ``extra_models`` entries,
            # which carry no `id`/`config_name`; without it an allow-listed id
            # supplied there would be silently dropped by the narrow step.
            raw = [e for e in raw
                   if (e.get("id") or e.get("config_name") or e.get("name")) in wanted]
        models = []
        seen: set[str] = set()
        for entry in raw:
            norm = NORMALIZERS[pname](entry, pdef, cmap, cidx)
            if not norm or not norm.get("name") or norm["name"] in seen:
                continue
            seen.add(norm["name"])
            prior = existing.get((pname, norm["name"]), {})
            # preserve human edits
            norm["aliases"] = prior.get("aliases", [])
            norm["tiers"] = prior.get("tiers", [])
            # A catalogue can omit a fact entirely, and the fallbacks can hold
            # a number that is simply wrong: MiniMax's /v1/models publishes no
            # window, no output cap and no modality, while LiteLLM's cost map
            # knows M3's 1M window but insists its output cap is 128000 — the
            # endpoint rejects anything over 524288. So ``hints`` is not just a
            # "don't say I don't know" fallback: for the fields it names it is
            # the authority, because those numbers were measured against the
            # live endpoint instead of read out of someone else's table.
            #
            # Two sources, one precedence: a hint declared on the provider in
            # this file (code, reviewable) wins over one carried in the
            # registry (data, hand-editable), and both win over discovery.
            hints = dict(prior.get("hints") or {})
            hints.update((pdef.get("hints") or {}).get(norm["name"]) or {})
            for field in ("ctx", "max_output", "vision"):
                if hints.get(field) is not None:
                    norm[field] = hints[field]
            if hints:
                norm["hints"] = hints
            models.append(norm)
        models.sort(key=lambda m: m["name"])
        fill_variant_siblings(models)
        by_prov[pname] = provider_block(pname, pdef, models)

    # stable provider order: as declared in PROVIDERS
    doc = {"version": 1, "providers": [by_prov[p] for p in PROVIDERS if p in by_prov]}
    return doc


def _yaml_scalar(v) -> str:
    if v is True:
        return "true"
    if v is False:
        return "false"
    return str(v)


def to_yaml(doc: dict) -> str:
    """Emit YAML without requiring pyyaml (deterministic ordering)."""
    lines = [f"version: {doc['version']}", "providers:"]
    for p in doc["providers"]:
        lines.append(f"  - name: {p['name']}")
        lines.append(f"    api_base: {p['api_base']}")
        lines.append(f"    endpoint_kind: {p['endpoint_kind']}")
        lines.append(f"    auth_env: {p['auth_env']}")
        lines.append("    models:")
        if not p["models"]:
            lines[-1] = "    models: []"
            continue
        for m in p["models"]:
            lines.append(f"      - name: {m['name']}")
            lines.append(f"        ctx: {m.get('ctx') if m.get('ctx') is not None else 'null'}")
            lines.append(
                f"        max_output: {m.get('max_output') if m.get('max_output') is not None else 'null'}"
            )
            v = m.get("vision")
            lines.append(f"        vision: {'true' if v is True else ('false' if v is False else 'null')}")
            lines.append(f"        endpoint_kind: {m.get('endpoint_kind', p['endpoint_kind'])}")
            al = m.get("aliases") or []
            lines.append("        aliases: [" + ", ".join(al) + "]")
            ti = m.get("tiers") or []
            lines.append("        tiers: [" + ", ".join(ti) + "]")
            # Emit the hints so they survive a round trip. Dropping them here
            # would make every refresh re-derive the values from a catalogue
            # that does not carry them, i.e. silently undo the pin.
            hints = m.get("hints") or {}
            if hints:
                lines.append("        hints: {" + ", ".join(
                    f"{k}: {_yaml_scalar(hints[k])}" for k in sorted(hints)
                ) + "}")
    return "\n".join(lines) + "\n"


def main() -> None:
    # One provider's api_base is the loopback Trae bridge, and a proxy
    # answering for 127.0.0.1 replies 502 with an empty body — an empty
    # catalogue, silently. Keep the loopback out of the proxy.
    ensure_loopback_bypass()

    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--diff", action="store_true")
    args = ap.parse_args()

    doc = build(args.provider)
    text = to_yaml(doc)
    total = sum(len(p["models"]) for p in doc["providers"])
    print(f"Discovered {total} models across {len(doc['providers'])} provider(s).")

    if args.dry_run or args.diff:
        if args.diff and REGISTRY_PATH.is_file():
            old = REGISTRY_PATH.read_text().splitlines()
            new = text.splitlines()
            import difflib

            for line in difflib.unified_diff(old, new, "current", "discovered", lineterm=""):
                print(line)
        else:
            print(text)
        return

    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(text)
    print(f"Wrote {REGISTRY_PATH}")


if __name__ == "__main__":
    main()
