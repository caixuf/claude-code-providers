#!/usr/bin/env python3
"""Resolve which models a provider offers, and what to write for a chosen one.

`ccp` presents one folder per provider; the models are its contents. Two shapes
exist and they must not be confused:

* **native** — the profile points straight at the vendor's Anthropic endpoint,
  so the selectable model IS the upstream id from ``registry/models.yaml``
  (``qwen3.8-max``, ``DeepSeek-V4.1-Flash``).
* **bridged** — the request goes through LiteLLM, which only accepts
  ``model_name`` entries. Those come from the alias table, so the selectable
  name is the alias with its ``<provider>-`` prefix removed
  (``cline-kimi-k3`` -> ``kimi-k3``).

Why this is its own file: bin/ccp is bash, and deriving the candidate list needs
the registry, the alias table and the native/bridged distinction. Doing it once
here keeps the shell side down to reading two lines.

Commands
--------
  folders                    one line per provider: name|kind|count|default|ctx
  list <provider>            human-readable model list for one provider
  resolve <provider> [model] print `MODEL=` and `CTX=` for the chosen model

`resolve` with no model (or the literal ``default``) yields the profile's
default tier. Model names match case-insensitively; the registry's own
spelling is what gets written, so a vendor's odd capitalisation is preserved
instead of being normalised into a name the upstream does not accept.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

import yaml

ONE_M = 1_000_000


def _repo() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def _load() -> tuple[dict, dict]:
    root = _repo()
    models_doc = yaml.safe_load((root / "registry/models.yaml").read_text()) or {}
    profiles_doc = yaml.safe_load((root / "registry/profiles.yaml").read_text()) or {}
    return models_doc, profiles_doc


def _fmt_ctx(ctx: int | None) -> str:
    if not ctx:
        return "?"
    if ctx < ONE_M:
        return f"{ctx // 1000}k"
    return f"{ctx / ONE_M:.2f}".rstrip("0").rstrip(".") + "M"


class Provider:
    """One folder: a provider plus the models selectable inside it."""

    def __init__(self, name: str, profiles_doc: dict, models_doc: dict):
        self.name = name
        self.profile = (profiles_doc.get("profiles") or {}).get(name) or {}
        self.native = bool(self.profile.get("native"))
        provider = self.profile.get("provider", name)

        # (provider, model) -> alias, honouring last-wins like render_config
        self._alias_for: dict[str, str] = {}
        for alias, spec in (profiles_doc.get("aliases") or {}).items():
            if spec.get("provider") == provider:
                self._alias_for[spec["model"]] = alias

        models = [
            p for p in (models_doc.get("providers") or []) if p["name"] == provider
        ]
        self._meta = {m["name"]: m for m in (models[0].get("models") if models else [])}

        # selectable name -> upstream registry model
        self._choices: dict[str, str] = {}
        if self.native:
            for m in self._meta:
                self._choices[m] = m
        else:
            for upstream, alias in self._alias_for.items():
                if upstream in self._meta:
                    self._choices[_strip(alias, provider)] = upstream

    # ── queries ──────────────────────────────────────────────────────────
    @property
    def models(self) -> list[tuple[str, str]]:
        """(selectable name, upstream registry model), sorted by name."""
        return sorted((name, up) for name, up in self._choices.items())

    def default(self) -> tuple[str, str] | None:
        """The profile's default tier, as (selectable name, upstream)."""
        spec = (self.profile.get("tiers") or {}).get(
            self.profile.get("primary_tier", "default")
        ) or {}
        upstream = spec.get("model")
        if not upstream:
            return None
        name = upstream if self.native else _strip(
            self._alias_for.get(upstream, f"{self.name}-{upstream}"), self.name
        )
        return name, upstream

    def ctx(self, upstream: str) -> int | None:
        return self._meta.get(upstream, {}).get("ctx")

    def resolve(self, wanted: str) -> tuple[str, str]:
        """(selectable name, upstream) for `wanted`, case-insensitively."""
        if not wanted or wanted == "default":
            got = self.default()
            if got:
                return got
            if self._choices:
                first = sorted(self._choices)[0]
                return first, self._choices[first]
            raise KeyError(f"{self.name} has no models")
        lowered = wanted.lower()
        for name, upstream in self._choices.items():
            if name.lower() == lowered:
                return name, upstream
        # tolerate the upstream id and the full alias too, so `ccp trae qwen3.8-max`
        # and `ccp cline cline-kimi-k3` both work
        for name, upstream in self._choices.items():
            if upstream.lower() == lowered:
                return name, upstream
            if f"{self.name}-{name}".lower() == lowered:
                return name, upstream
        raise KeyError(wanted)

    def wire_name(self, selectable: str, upstream: str) -> str:
        """What actually goes into ANTHROPIC_MODEL."""
        if self.native:
            return upstream  # the vendor endpoint wants its own id
        alias = self._alias_for.get(upstream, f"{self.name}-{selectable}")
        ctx = self.ctx(upstream) or 0
        return f"{alias}[1m]" if ctx >= ONE_M else alias


def _strip(alias: str, provider: str) -> str:
    prefix = f"{provider}-"
    return alias[len(prefix):] if alias.startswith(prefix) else alias


def _providers(profiles_doc: dict, models_doc: dict) -> list[Provider]:
    return [
        Provider(name, profiles_doc, models_doc)
        for name in (profiles_doc.get("profiles") or {})
    ]


def cmd_folders(args: argparse.Namespace) -> int:
    models_doc, profiles_doc = _load()
    for p in _providers(profiles_doc, models_doc):
        default = p.default()
        dname = default[0] if default else "?"
        dctx = p.ctx(default[1]) if default else None
        print(f"{p.name}|{'native' if p.native else 'bridged'}|{len(p.models)}|{dname}|{dctx or 0}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    models_doc, profiles_doc = _load()
    for p in _providers(profiles_doc, models_doc):
        if p.name != args.provider:
            continue
        default = p.default()
        print(
            f"\033[36m{p.name}\033[0m "
            f"({'native' if p.native else 'bridged'}) — {len(p.models)} models"
        )
        print(f"  {'model':38s} {'ctx':>7} {'out':>8}  vis")
        for name, upstream in p.models:
            meta = p._meta.get(upstream, {})
            mark = "\033[32m*\033[0m" if default and name == default[0] else " "
            vis = (
                "yes" if meta.get("vision") is True
                else "no" if meta.get("vision") is False else "-"
            )
            out = meta.get("max_output") or "?"
            print(f"{mark} {name:38s} {_fmt_ctx(p.ctx(upstream)):>7} {str(out):>8}  {vis}")
        return 0
    print(f"Error: no provider '{args.provider}'", file=sys.stderr)
    return 1


def cmd_resolve(args: argparse.Namespace) -> int:
    models_doc, profiles_doc = _load()
    for p in _providers(profiles_doc, models_doc):
        if p.name != args.provider:
            continue
        try:
            selectable, upstream = p.resolve(args.model or "")
        except KeyError:
            print(
                f"Error: '{args.model}' is not a {args.provider} model.",
                file=sys.stderr,
            )
            cmd_list(argparse.Namespace(provider=args.provider))
            return 1
        print(f"MODEL={p.wire_name(selectable, upstream)}")
        print(f"CTX={p.ctx(upstream) or 0}")
        return 0
    print(f"Error: no provider '{args.provider}'", file=sys.stderr)
    return 1


# ── verification: does the name we are about to write actually serve? ─────────
# A catalogue is a claim, not evidence. MiniMax answers a request for
# `MiniMax-M3.1` with HTTP 200 and a completion — from M3, which the body
# admits in its `model` field. ClinePass does the same for the 463 ids it does
# not serve. Trae at 1.25M tokens returns 200, an empty body and zero usage.
#
# So the only honest check is to make one real call and read what came back.
# Cached, because a probe costs a round trip and the answer does not change
# within a session.

VERIFY_CACHE = pathlib.Path.home() / ".cache" / "ccp" / "model-verified.json"
VERIFY_TTL = 12 * 3600  # a day; plans and quotas do not turn over faster


def _installed(profile_name: str) -> dict:
    """The env the user's session would actually use for this provider."""
    path = pathlib.Path.home() / ".claude" / "providers" / f"{profile_name}.json"
    try:
        return json.loads(path.read_text()).get("env") or {}
    except Exception:
        return {}


def _cache_load() -> dict:
    try:
        return json.loads(VERIFY_CACHE.read_text())
    except Exception:
        return {}


def _cache_store(key: str, served: str | None, note: str) -> None:
    try:
        VERIFY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        data = _cache_load()
        data[key] = {"served": served, "note": note, "at": time.time()}
        VERIFY_CACHE.write_text(json.dumps(data))
    except Exception:
        pass


def verify(provider_name: str, model: str, timeout: float = 45.0) -> tuple[str | None, str]:
    """Call the model once; return (name the upstream says it served, note).

    Returns (None, note) when the call cannot be made at all — no profile
    installed, no credential, endpoint down. That is not a failure of the model,
    and must not be reported as one.
    """
    env = _installed(provider_name)
    base = env.get("ANTHROPIC_BASE_URL") or ""
    token = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or ""
    if not base or not token:
        return None, "no profile installed for %s" % provider_name

    # The credential is part of the identity of the answer: the standard
    # MiniMax key serves M3 for this name, the M Plan key serves M3.1. A cache
    # keyed on provider+model alone would carry the old verdict across a key
    # change and report a downgrade that is no longer true — or hide a real one.
    key = "%s|%s|%s" % (provider_name, model, hashlib.sha256(token.encode()).hexdigest()[:12])
    cached = _cache_load().get(key)
    if cached and time.time() - cached.get("at", 0) < VERIFY_TTL:
        return cached.get("served"), cached.get("note", "")

    body = json.dumps({
        "model": model, "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    req = urllib.request.Request(base.rstrip("/") + "/v1/messages", data=body, method="POST")
    for k, v in (("content-type", "application/json"), ("x-api-key", token),
                 ("authorization", "Bearer " + token),
                 ("anthropic-version", "2023-06-01")):
        req.add_header(k, v)
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = str((json.loads(e.read()).get("error") or {}).get("message", ""))[:80]
        except Exception:
            pass
        note = "HTTP %s %s" % (e.code, detail)
        _cache_store(key, None, note)
        return None, note
    except Exception as e:
        note = "%s: %s" % (type(e).__name__, str(e)[:60])
        _cache_store(key, None, note)
        return None, note

    try:
        doc = json.loads(raw)
    except Exception:
        doc = {}
    if doc.get("error"):
        note = str((doc.get("error") or {}).get("message", ""))[:80]
        _cache_store(key, None, note)
        return None, note

    served = doc.get("model") or ""
    usage = doc.get("usage") or {}
    out_tok = usage.get("output_tokens") or 0
    blocks = doc.get("content") or []
    has_text = any(b.get("type") == "text" and (b.get("text") or "").strip() for b in blocks)
    # 200 with nothing in it is the silent-failure shape (Trae at 1.25M).
    if not has_text and not out_tok:
        note = "200 but empty (no text, zero usage)"
        _cache_store(key, None, note)
        return served, note

    note = "ok, %s output tokens" % out_tok if out_tok else "ok"
    _cache_store(key, served, note)
    return served, note


def cmd_verify(args: argparse.Namespace) -> int:
    """Report whether the requested model is the model that answers.

    Exit 0 when the response names the model that was asked for; 1 when it does
    not, or when the call could not be made.
    """
    models_doc, profiles_doc = _load()
    provider = None
    for p in _providers(profiles_doc, models_doc):
        if p.name == args.provider:
            provider = p
            break
    if provider is None:
        print("Error: no provider '%s'" % args.provider, file=sys.stderr)
        return 1

    try:
        selectable, upstream = provider.resolve(args.model or "")
    except KeyError:
        print("Error: '%s' is not a %s model." % (args.model, args.provider), file=sys.stderr)
        cmd_list(argparse.Namespace(provider=args.provider))
        return 1

    wire = provider.wire_name(selectable, upstream)
    served, note = verify(args.provider, wire, timeout=args.timeout)

    if served is None:
        print("UNVERIFIED\t%s\t%s" % (wire, note), file=sys.stderr)
        return 1
    if _same_model(served, wire):
        print("OK\t%s\t%s" % (wire, note))
        return 0
    print("MISMATCH\t%s\t%s" % (wire, served))
    print("", file=sys.stderr)
    print("  %s is not served here." % wire, file=sys.stderr)
    print("  The endpoint answered as \"%s\" instead (%s)." % (served, note), file=sys.stderr)
    print("  A 200 and a completion are not proof — this name is silently", file=sys.stderr)
    print("  downgraded, so using it would quietly cost you a different model.", file=sys.stderr)
    return 1


def _same_model(served: str, asked: str) -> bool:
    """Compare names, tolerating the [1m] marker and case.

    A gateway may echo the name back verbatim even when it is serving something
    else, so this is a cheap first check, not proof — it only rejects the
    obvious cases.
    """
    def norm(x: str) -> str:
        x = x.strip()
        if x.lower().endswith("[1m]"):
            x = x[:-4]
        return x.lower()
    a, b = norm(asked), norm(served)
    if a == b:
        return True
    # LiteLLM prefixes entries (`openai/cline-pass/deepseek-v4.1-flash`) and
    # the registry may prefix them too (`deepseek/deepseek-v4.1-flash`), while
    # the other side answers with the bare upstream id. Compare the last
    # segment of each, but only when one side actually has a prefix — otherwise
    # "deepseek-v4.1-flash" would match any provider's copy of it.
    a_tail, b_tail = a.rsplit("/", 1)[-1], b.rsplit("/", 1)[-1]
    if a_tail == b_tail and ("/" in a or "/" in b):
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(prog="ccp_pick")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("folders").set_defaults(func=cmd_folders)
    p_list = sub.add_parser("list")
    p_list.add_argument("provider")
    p_list.set_defaults(func=cmd_list)
    p_res = sub.add_parser("resolve")
    p_res.add_argument("provider")
    p_res.add_argument("model", nargs="?", default="")
    p_res.set_defaults(func=cmd_resolve)
    p_ver = sub.add_parser("verify")
    p_ver.add_argument("provider")
    p_ver.add_argument("model", nargs="?", default="")
    p_ver.add_argument("--timeout", type=float, default=45.0)
    p_ver.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())