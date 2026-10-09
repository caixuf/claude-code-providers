#!/usr/bin/env python3
"""Auto-discover model capabilities from providers and emit registry/models.yaml.

Instead of hand-filling context window / vision / output caps for every model,
this pulls them from, in priority order:

  1. The provider's own ``GET .../models`` endpoint (authoritative when present):
       - CommandCode : context_length, supported_endpoints   (~87 models)
       - StepFun     : max_input_tokens, enable_vision_input  (rich!)
       - Cline       : id/name only
       - MiniMax / DeepSeek : standard OpenAI /v1/models shape
  2. LiteLLM's bundled cost map (``litellm.model_cost``, ~4500 entries) for
     max_input_tokens / max_output_tokens / supports_vision.
  3. A tiny built-in defaults table for the few facts neither source knows
     (e.g. every CommandCode model caps output at 393216 — verified by probe).

Output is a *generated but committed* ``registry/models.yaml`` that a human can
still hand-edit; re-running merges (never clobbers manual ``aliases``/``tiers``).

Usage:
  python3 discover_models.py                 # refresh all providers into registry
  python3 discover_models.py --provider cmdc # just one
  python3 discover_models.py --dry-run       # print, don't write
  python3 discover_models.py --diff          # show what changed vs current file
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

# CommandCode's hard output cap — verified by direct probe
# (max_tokens=393216 -> 200, 393217 -> 400) across flash / flash-fast / pro.
CMDC_MAX_OUTPUT = 393216

# Provider definitions. ``auth_file`` points at the deployed litellm config or
# env so we can read the token without hard-coding secrets here.
PROVIDERS = {
    "cmdc": {
        "models_url": "https://api.commandcode.ai/provider/v1/models",
        "api_base": "https://api.commandcode.ai/provider/v1",
        "endpoint_kind": "openai",  # Chat Completions / Responses
        "litellm_prefix": "openai/",  # we keep upstream model id as-is
        "auth_env": "CMDC_API_KEY",
        "auth_file": "~/.config/litellm/config.yaml",
        "auth_grep": "api.commandcode.ai",
        "max_output_default": CMDC_MAX_OUTPUT,
    },
    "stepfun": {
        "models_url": "https://api.stepfun.com/step_plan/v1/models",
        "api_base": "https://api.stepfun.com/step_plan",
        "endpoint_kind": "anthropic",
        "litellm_prefix": "anthropic/",
        "auth_env": "STEPFUN_API_KEY",
        "auth_file": "~/.config/litellm/config.yaml",
        "auth_grep": "api.stepfun.com",
        "max_output_default": 128000,
    },
    "cline": {
        "models_url": "https://api.cline.bot/api/v1/models",
        "api_base": "https://api.cline.bot/api/v1",
        "endpoint_kind": "openai",
        "litellm_prefix": "openai/",
        "auth_env": "CLINE_API_KEY",
        "auth_file": "~/.config/litellm/config.yaml",
        "auth_grep": "api.cline.bot",
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
    "minimax": {
        "models_url": "https://api.minimaxi.com/v1/models",
        "api_base": "https://api.minimaxi.com/anthropic",
        "endpoint_kind": "anthropic",
        "auth_env": "MINIMAX_API_KEY",
        "profile_file": "~/.claude/providers/minimax.json",
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


def fetch_models(url: str, token: str | None) -> list[dict]:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    # Some gateways (CommandCode) sit behind Cloudflare and 403 a bare
    # urllib agent; present a normal client UA.
    req.add_header("User-Agent", "ccp-discover/1.0 (+https://github.com/caixuf/claude-code-providers)")
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
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


def norm_cmdc(entry: dict, provider: dict, cmap: dict, idx: dict | None = None) -> dict:
    mid = entry.get("id", "")
    ctx = entry.get("context_length")
    endpoints = entry.get("supported_endpoints") or []
    kind = "anthropic" if endpoints == ["/messages"] else "openai"
    # cross-check with cost map
    info = cost_lookup(cmap, [mid, f"openrouter/{mid}", mid.split("/", 1)[-1]], idx)
    return {
        "name": mid,
        "ctx": ctx or info.get("ctx"),
        "max_output": provider["max_output_default"],
        "vision": info.get("vision") if info.get("vision") is not None else _guess_vision(mid),
        "endpoint_kind": kind,
    }


def norm_stepfun(entry: dict, provider: dict, cmap: dict, idx: dict | None = None) -> dict:
    mid = entry.get("id", "")
    if entry.get("model_type") and entry["model_type"] not in ("大语言模型",):
        return {}  # skip tts/asr/audio
    info = cost_lookup(cmap, [mid, f"anthropic/{mid}"], idx)
    return {
        "name": mid,
        "ctx": entry.get("max_input_tokens") or info.get("ctx"),
        "max_output": provider["max_output_default"],
        "vision": bool(entry.get("enable_vision_input"))
        if "enable_vision_input" in entry
        else info.get("vision"),
        "endpoint_kind": "anthropic",
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


def _guess_vision(mid: str) -> bool | None:
    m = mid.lower()
    if any(w in m for w in ("vision", "-vl", "omni", "gemini", "gpt-4o", "gpt-5", "claude-", "grok")):
        return True
    return None


NORMALIZERS = {
    "cmdc": norm_cmdc,
    "stepfun": norm_stepfun,
    "cline": norm_generic,
    "deepseek": norm_deepseek,
    "minimax": norm_generic,
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
    by_prov: dict[str, dict] = {}
    for pname, models in existing_by_prov.items():
        pdef = PROVIDERS.get(pname, {})
        by_prov[pname] = provider_block(
            pname,
            {
                "api_base": pdef.get("api_base", ""),
                "endpoint_kind": pdef.get("endpoint_kind", "openai"),
                "auth_env": pdef.get("auth_env", ""),
            },
            models,
        )

    for pname, pdef in PROVIDERS.items():
        if only and pname != only:
            continue
        token = read_token(pdef)
        raw = None
        try:
            raw = fetch_models(pdef["models_url"], token)
        except Exception as e:
            print(f"  ! {pname}: fetch failed ({e}); keeping existing entries", file=sys.stderr)
        if raw is None:
            # keep the previously discovered models verbatim (don't lose data
            # just because a key is temporarily missing/rate-limited)
            prior_models = existing_by_prov.get(pname) or []
            by_prov[pname] = provider_block(pname, pdef, prior_models)
            continue
        models = []
        for entry in raw:
            norm = NORMALIZERS[pname](entry, pdef, cmap, cidx)
            if not norm or not norm.get("name"):
                continue
            prior = existing.get((pname, norm["name"]), {})
            # preserve human edits
            norm["aliases"] = prior.get("aliases", [])
            norm["tiers"] = prior.get("tiers", [])
            models.append(norm)
        models.sort(key=lambda m: m["name"])
        fill_variant_siblings(models)
        by_prov[pname] = provider_block(pname, pdef, models)

    # stable provider order: as declared, then any extras
    order = list(PROVIDERS) + [p for p in by_prov if p not in PROVIDERS]
    doc = {"version": 1, "providers": [by_prov[p] for p in order if p in by_prov]}
    return doc


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
    return "\n".join(lines) + "\n"


def main() -> None:
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
