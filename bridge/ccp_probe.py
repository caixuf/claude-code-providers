#!/usr/bin/env python3
"""ccp_probe.py — Probe availability and speed (TTFT, TPS) for CCP models.

Tests model responsiveness through the Universal Bridge (:4000) by measuring:
  * Availability: HTTP status / error diagnostics (200 OK, 401 Auth, 429 Quota, Timeout)
  * TTFT: Time to First Token (首字延迟) via streaming chunk detection
  * TPS: Tokens per second (生成速度)
  * Total: End-to-end turnaround latency

Usage:
  python3 ccp_probe.py                 # probe primary models of each provider
  python3 ccp_probe.py trae            # probe all models under 'trae'
  python3 ccp_probe.py main            # probe a single model or alias
  python3 ccp_probe.py --all           # probe all available models
  python3 ccp_probe.py --sort speed    # sort by generation speed (tokens/sec)
  python3 ccp_probe.py --sort ttft     # sort by first token latency
  python3 ccp_probe.py --switch        # recommend & output best model to switch
  python3 ccp_probe.py --json          # emit JSON report
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from ccp_pick import _providers  # noqa: E402  (same derivation as `ccp`/switch)
from loopback import ensure_loopback_bypass, is_loopback_url, loopback_mounts  # noqa: E402
from bridge_auth import MASTER_KEY as DEFAULT_MASTER_KEY  # noqa: E402

REPO_DIR = pathlib.Path(__file__).resolve().parent.parent
PROFILES_PATH = REPO_DIR / "registry" / "profiles.yaml"
MODELS_PATH = REPO_DIR / "registry" / "models.yaml"

DEFAULT_BRIDGE_URL = "http://127.0.0.1:4000"

def _bulk_prompt(max_tokens: int) -> str:
    """A prompt whose answer actually FILLS the token budget.

    `max_tokens` is a ceiling, not a target. The old prompt ("Explain what an
    API is in 2 sentences") made the model answer ~30 tokens and stop, so the
    tok/s column was computed on a sample small enough that the fixed cost
    dominated — a provider measured 501 tok/s on one run and 194 on the next,
    and a real gap of under 10x looked like 35x. Asking for a numbered list
    whose length scales with the budget makes the model produce roughly the
    tokens we intend to time; asking for prose does not, because prose has its
    own idea of when it is finished.

    The list is sized at 40% of the budget, not 80%. Each number costs two
    or three tokens once the separators are counted, and a model that reasons
    before it answers spends part of the same budget on the reasoning: at
    160 items and 200 tokens the answer alone did not fit, the model was cut
    off mid-thought, and ClinePass answered `500 empty response content`
    about 65% of the time. The probe then reported a healthy speed for a
    model that had produced no answer at all. Half the budget for the answer
    leaves room for the reasoning, and the answer is still long enough to
    time.
    """
    n = max(20, int(max_tokens * 0.4))
    return ("Output the integers from 1 to %d, in order, separated by commas. "
            "Output the numbers only — no prose, no summary, no code fence." % n)


QUICK_PROMPT = "Count from 1 to 5."
# Kept for callers that pass an explicit prompt; the timed default is
# _bulk_prompt(max_tokens) so the answer fills the budget.
DEFAULT_PROMPT = _bulk_prompt(200)
PROBE_PROMPT = DEFAULT_PROMPT

# ── agent readiness ──────────────────────────────────────────────────────────
# For agent work TTFT is the wrong metric. A model can be fast to emit a token
# and still never call a tool, which is the failure that actually wastes a turn:
# reasoning does not advance the task, an action does. So this mode sends a
# prompt with one obvious action and measures time-to-first-tool-call, plus
# whether a tool call happens at all.
AGENT_TOOLS = [{
    "name": "read_file",
    "description": "Read a file from disk and return its contents.",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File to read"}},
        "required": ["path"],
    },
}]
AGENT_PROMPT = ("Read the file /etc/hostname so I can see the machine name. "
                "Call the tool now — do not explain first.")

DEFAULT_ROUNDS = 2
# A short generation makes the tok/s column mostly noise: at ~20-60 output
# tokens the fixed cost (TTFT plus session setup) dominates, and the same
# provider then reads 501 tok/s on one run and 194 on the next — measured on
# one provider, same settings, minutes apart. That is enough to make a provider look
# 35x slower than another when the real gap is under 10x, which is exactly the
# mistake this default used to invite. A longer sample costs a couple of
# seconds and buys a number worth ranking on. `-q` keeps the old cheap probe
# for a liveness check, where only reachability matters.
DEFAULT_TOKENS = 200
# The quick check used 20 tokens, which no reasoning model can answer in: the
# reasoning alone runs 19-45 tokens on this fleet, so the round was cut off
# before a single word came out and the provider was reported as down. 64
# leaves room for the reasoning and still answers in a couple of seconds.
QUICK_TOKENS = 64
BENCH_ROUNDS = 3
BENCH_TOKENS = 1000
MAX_TOKENS = DEFAULT_TOKENS


def default_timeout(tokens: int) -> float:
    """Per-round timeout that fits the work the probe asked for.

    The probe's prompt scales with the token budget (160 integers at the
    default 200), and the slower adapters need a long first byte before
    they say anything at all. Measured on this machine, Trae's first byte
    ran 6-23s at 200 tokens and 76s at 1000, with 117s in the round. A
    flat 10s cut them off mid-answer, so the row read as a failure when
    the model was simply slow — and "trae is down" is a conclusion a user
    acts on.

    The deadline therefore follows the budget: five seconds of headroom per
    token, floored at 30s and capped at 240s. The floor is what the reasoning
    models need — MiniMax-M3.1-Flash spent 15.1 s before its first token in
    one run, and a 15 s deadline cut it off and called it dead. The cap keeps
    a mistyped `--tokens` from hanging. The timeout is the gap between bytes,
    not the whole round, so a slow but steady stream is never cut.
    """
    return max(30.0, min(240.0, tokens / 5.0))


# Terminal color codes (safe fallback when not a tty)
USE_COLOR = sys.stdout.isatty()
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def pad_right(s: str, width: int) -> str:
    vlen = len(strip_ansi(s))
    return s + (" " * max(0, width - vlen))


def pad_left(s: str, width: int) -> str:
    vlen = len(strip_ansi(s))
    return (" " * max(0, width - vlen)) + s


def _color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def green(t: str) -> str:
    return _color(t, "32")


def yellow(t: str) -> str:
    return _color(t, "33")


def red(t: str) -> str:
    return _color(t, "31")


def cyan(t: str) -> str:
    return _color(t, "36")


def bold(t: str) -> str:
    return _color(t, "1")


def dim(t: str) -> str:
    return _color(t, "2")


def load_registry() -> Tuple[dict, dict]:
    try:
        p_doc = yaml.safe_load(PROFILES_PATH.read_text()) or {}
        m_doc = yaml.safe_load(MODELS_PATH.read_text()) or {}
        return p_doc, m_doc
    except Exception as e:
        print(f"Error loading registry: {e}", file=sys.stderr)
        return {}, {}


def _installed_endpoint(provider: str) -> Tuple[str, str]:
    """(base_url, token) for a provider, as the user's own session would use it.

    Read from the installed profile rather than the registry: that file is what
    Claude Code is actually pointed at, and for the native providers it is the
    only place the real credential lives (the template ships a placeholder).

    The file is not necessarily named after the provider. Profiles are named
    after the model they run (`opendesign-deepseek-v4.1-flash.json`,
    `cline-deepseek-v4.1-flash.json`), and a rename in the registry renames
    the installed file with it. Looking for `opendesign.json`, finding nothing
    and returning "" demoted every renamed provider to the LiteLLM bridge —
    and LiteLLM carries only the aliases, not the bare upstream ids, so the
    probe answered `Invalid model name passed in model=deepseek-v4.1-flash`
    for a provider that returns 200 when called directly. So: exact name
    first, then the longest matching `<provider>-*.json`, then give up.
    """
    root = pathlib.Path.home() / ".claude" / "providers"
    candidates = []
    exact = root / f"{provider}.json"
    if exact.exists():
        candidates.append(exact)
    try:
        candidates += sorted(root.glob(f"{provider}-*.json"),
                             key=lambda p: len(p.name), reverse=True)
    except Exception:
        pass
    for path in candidates:
        try:
            env = (json.loads(path.read_text()).get("env") or {})
        except Exception:
            continue
        base_url = env.get("ANTHROPIC_BASE_URL") or ""
        if not base_url:
            continue
        token = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or ""
        return base_url, token
    return "", ""


def _spec(provider: str, sel_name: str, upstream: str, prov, p_doc: dict) -> Dict[str, Any]:
    """One probe candidate, carrying its own endpoint and credential."""
    base_url, token = _installed_endpoint(provider)
    meta = prov._meta.get(upstream, {})
    # Prefer the extra metadata the registry may carry on the model row.
    return {
        "wire_name": provider_wire_name(prov, sel_name, upstream),
        "display_name": sel_name,
        "provider": provider,
        "ctx": _fmt_ctx(prov.ctx(upstream)),
        "vision": bool(meta.get("vision")),
        "is_default": bool(prov.default() and sel_name == prov.default()[0]),
        "endpoint": base_url or DEFAULT_BRIDGE_URL,
        "token": token,
        "native": bool(prov.native),
    }


def provider_wire_name(prov, sel_name: str, upstream: str) -> str:
    """What actually goes on the wire for this model.

    Native endpoints want the vendor's own id; bridged ones go through LiteLLM,
    which only accepts a model_name the alias table produced (the `[1m]` marker
    included when the window is 1M — that is what the profile sends).
    """
    try:
        return prov.wire_name(sel_name, upstream)
    except Exception:
        return upstream




def _norm(text: str) -> str:
    """Fold a model name to comparable form: lowercase, separators unified.

    `DeepSeek-V4.1-Flash`, `deepseek_v4_1_flash` and `v4.1-flash` collapse to the
    same string. Separators become a single '-', not nothing: `kimi-k3` must
    stay two tokens so a search for `kimi` finds it, while `flashx` stays one so
    a search for `flash` does not. Deleting the separator destroys exactly the
    information needed to tell those apart.
    """
    return re.sub(r"[^a-z0-9.]+", "-", (text or "").lower()).strip("-")


def _has(haystack: str, term: str) -> bool:
    """Token-boundary aware match: `flash` must not select `glm-5.3-flashx`.

    Separators are single '-' after normalising, so a boundary is either the
    end of the haystack, a '-', or a '.' beginning a new token. The term itself
    may contain '-', and may match with or without one on the haystack side:
    people type `v4.1flash` for `V4.1-Flash`, so a term is also compared against
    a separator-stripped copy of the haystack.
    """
    if not term:
        return False
    term_loose = term.replace("-", "")
    hay_loose = haystack.replace("-", "")
    haystacks = (haystack, hay_loose) if term_loose != term else (haystack,)
    for hay in haystacks:
        start = 0
        while True:
            i = hay.find(term, start)
            if i < 0:
                break
            tail = hay[i + len(term):]
            # a boundary is: nothing, a separator, or a '.' — anything else
            # continues the same token
            if tail == "" or tail[0] in "-.":
                return True
            start = i + 1
    # last resort: the term with its own separators removed, against the
    # separator-free haystack (handles `v4.1flash` vs `V4.1-Flash`)
    if term_loose != term and term_loose in hay_loose:
        i = hay_loose.find(term_loose)
        tail = hay_loose[i + len(term_loose):]
        if tail == "" or tail[0] in "-.":
            return True
        # `flash` vs `flashx` in the loose form: a trailing letter means the
        # word continues, a digit means a new segment
        if tail[0].isdigit() or tail[0] in "-.":
            return True
    return False


def _match_across_providers(target: str, providers: dict, ignore_provider_match: bool = False) -> List[Dict[str, Any]]:
    """Every model, at every provider, that the target names.

    Split on whitespace first so `deepseek v4.1 flash` is three required terms
    rather than one literal. Requiring all of them keeps `flash` from matching
    every Flash model on earth, and dropping the separators keeps `v4.1flash`
    from failing to match `V4.1-Flash`.
    """
    # Try the whole string first: `v4.1flash` normalises to `v4.1flash`, which
    # is exactly how `V4.1-Flash` normalises. Only if that finds nothing, fall
    # back to requiring each word — which is what makes `deepseek v4.1` select
    # the v4.1 line across vendors instead of one exact string.
    whole = _norm(target)
    words = [_norm(t) for t in target.split() if _norm(t)]
    terms = [whole] if whole else words
    if not terms:
        return []
    matched: List[Dict[str, Any]] = []
    for pname, prov in providers.items():
        for sel_name, upstream in prov.models:
            haystacks = [
                _norm(sel_name),
                _norm(upstream),
                _norm(provider_wire_name(prov, sel_name, upstream)),
                _norm(f"{pname}-{sel_name}"),
            ]
            hit = all(any(_has(h, term) for h in haystacks) for term in terms)
            if not hit and terms != words:
                # whole-string miss: retry with the individual words
                hit = all(any(_has(h, term) for h in haystacks) for term in words)
            if not hit:
                # `v4.1flash` spells `v4.1-flash` without the separator. Compare
                # separator-free forms so the way people actually type it works,
                # while the boundary rules above keep `flash` off `flashx`.
                loose_terms = [_norm(t).replace("-", "") for t in (terms if len(terms) == 1 else words)]
                loose_hays = [h.replace("-", "") for h in haystacks]
                hit = all(any(t in h for h in loose_hays) for t in loose_terms if t)
            if not hit:
                continue
            if not ignore_provider_match and terms == [_norm(pname)]:
                # the bare vendor name is a folder, not a model request
                continue
            matched.append(_spec(pname, sel_name, upstream, prov, {}))
    return matched

def get_configured_models(target: Optional[str] = None, all_models: bool = False) -> List[Dict[str, Any]]:
    """Return list of candidate models with metadata to probe.

    Scope rules:
      no target          primary model of every provider, plus the routing groups
      <provider>         every model that provider offers
      <model>            the matching model(s), searched across providers
      <group>            that routing group
      --all              every model of every provider
    """
    target_lower = target.lower().strip() if target else ""
    p_doc, m_doc = load_registry()
    providers = {p.name: p for p in _providers(p_doc, m_doc)}
    groups = p_doc.get("groups") or {}

    candidates: List[Dict[str, Any]] = []

    # 1. Routing groups
    if not target or target_lower in ("group", "groups", "all") or any(target_lower == g.lower() for g in groups):
        for gname, gspec in groups.items():
            if target_lower and target_lower not in ("group", "groups", "all") and target_lower != gname.lower():
                continue
            members = gspec.get("members") or []
            candidates.append({
                # A group is a LiteLLM model_name of its own; probe it through
                # the bridge like any other bridged model.
                "wire_name": gname,
                "display_name": f"group/{gname}",
                "provider": "group",
                "ctx": "1M",
                "vision": True,
                "is_default": True,
                "description": f"pool ({', '.join(members[:2])}...)",
                "endpoint": DEFAULT_BRIDGE_URL,
                "token": DEFAULT_MASTER_KEY,
                "native": False,
            })

    # 2. Providers. A provider name means "everything it offers"; no target means
    #    the primary of each; an unmatched name is treated as a model below.
    known_provider = target_lower in {n.lower() for n in providers}
    known_group = target_lower in {g.lower() for g in groups} or target_lower in ("group", "groups")
    for name, prov in providers.items():
        if target and not known_provider and not all_models:
            continue  # the target names a model, handled in step 3
        if known_provider and target_lower != name.lower():
            continue
        if target or all_models:
            picks = prov.models
        else:
            default = prov.default()
            picks = [default] if default else []
        for sel_name, upstream in picks:
            candidates.append(_spec(name, sel_name, upstream, prov, p_doc))

    # 3. Target that names specific model(s), searched across every provider.
    #
    #    This is the "which vendor is fastest for deepseek-v4.1-flash" question,
    #    so matching has to survive how people actually type it: `deepseek v4.1
    #    flash`, `v4.1flash`, `deepseek-v4.1-flash`. Exact matching found none
    #    of those. Normalise both sides — drop separators, lowercase — and
    #    require every word to appear, so `deepseek v4.1` collects the v4.1 line
    #    across all vendors instead of one exact string.
    if target and not known_provider and not known_group:
        matched = _match_across_providers(target, providers)

        # A name that is also a vendor but is not one (`deepseek` is both a
        # provider and the head of five model ids) should still find models.
        if not matched:
            matched = _match_across_providers(target, providers, ignore_provider_match=True)
        candidates = matched

    # Deduplicate by (wire_name, endpoint) — the same name can legitimately
    # exist on two providers, and they are different probes.
    seen = set()
    unique = []
    for c in candidates:
        key = (c["wire_name"], c.get("endpoint", ""))
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def _fmt_ctx(ctx: Optional[int]) -> str:
    if not ctx:
        return "-"
    if ctx >= 1_000_000:
        return f"{ctx / 1_000_000:.1f}M".replace(".0M", "M")
    return f"{ctx // 1000}k"


def _err_detail(body: bytes) -> str:
    """Pull a readable message out of either error shape.

    Anthropic answers {"type":"error","error":{"message":...}}; LiteLLM wraps
    some upstreams as {"error":{"message":...}} and others as a bare string.
    """
    try:
        doc = json.loads(body)
    except Exception:
        return body.decode("utf-8", "replace")[:120]
    err = doc.get("error") if isinstance(doc, dict) else None
    if isinstance(err, dict):
        return _clean_message(str(err.get("message") or ""))
    if isinstance(err, str):
        return _clean_message(err)
    return _clean_message(str(doc.get("message") or ""))


# LiteLLM prefixes its exceptions with the exception class, twice, before
# the part that says anything: "litellm.InternalServerError:
# InternalServerError: OpenAIException - Error code: 500 - {'error':
# 'empty response content'}". The first 60 characters of that are all
# noise, and the column was cut there.
_LITELLM_PREFIX = re.compile(r"^(?:litellm\.[A-Za-z]+(?:Error|Exception):\s*)+")


def _clean_message(msg: str) -> str:
    """Drop the repeated exception-class prefix, keep the cause."""
    return _LITELLM_PREFIX.sub("", msg or "").strip()[:120]


def _blank_body_hint(status: int, base_url: str) -> str:
    """Name the failure when the response body is empty.

    An empty body is the signature of a proxy answering on the upstream's
    behalf: a proxy cannot route a loopback destination, so it replies 502
    with nothing in it, and the client sees a gateway error with no
    explanation. "empty body" alone would leave the reader guessing, so
    say where the answer came from.
    """
    if status in (502, 504):
        if is_loopback_url(base_url):
            return "empty body: proxy answered for a loopback URL"
        return "empty body: gateway/proxy error"
    return f"empty body (HTTP {status})"


async def _probe_single_round(
    client: httpx.AsyncClient,
    wire_name: str,
    base_url: str,
    master_key: str,
    prompt: str,
    max_tokens: int,
    timeout_s: float,
    native: bool = False,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """One streaming round over Anthropic Messages, measuring TTFT / TPS.

    Anthropic Messages rather than OpenAI chat-completions on purpose: it is the
    one protocol every provider here speaks — the native ones point straight at
    an /anthropic endpoint, and the bridged ones reach it through LiteLLM. A
    chat-completions probe would test a path the user's session never takes, and
    would 400 on every native catalogue model for lack of a LiteLLM entry.
    """
    # When the caller did not hand us a prompt of their own, derive one that
    # scales with the budget — otherwise the model stops after two sentences
    # and we time a rounding error.
    if prompt in (DEFAULT_PROMPT, QUICK_PROMPT) and prompt != QUICK_PROMPT:
        prompt = _bulk_prompt(max_tokens)
    payload = {
        "model": wire_name,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if master_key:
        # Both spellings: the bridge wants x-api-key, several vendor endpoints
        # accept either, and sending both is accepted everywhere here.
        headers["x-api-key"] = master_key
        headers["Authorization"] = f"Bearer {master_key}"
    # The base is used as the caller wrote it: an endpoint that already ends in
    # /v1 (the native Codex gateway does) would otherwise be asked for
    # /v1/v1/messages, and that 404 comes back as an opaque `unsupported_endpoint`
    # from the gateway — a probe that says "not responsive" about a model that
    # answers in 2 s.
    root = base_url.rstrip("/")
    url = f"{root}/messages" if root.endswith("/v1") else f"{root}/v1/messages"

    t0 = time.perf_counter()
    t_first = None
    t_think = None      # first reasoning delta
    t_answer = None     # first text delta
    t_tool = None       # first tool call — the number that matters for agents
    tool_name = ""
    think_chars = 0
    answer_chars = 0
    completion_tokens = 0
    chunks = 0
    cached = 0

    try:
        async with client.stream("POST", url, json=payload, headers=headers, timeout=timeout_s) as resp:
            if resp.status_code != 200:
                detail = _err_detail(await resp.aread())
                if not detail:
                    detail = _blank_body_hint(resp.status_code, base_url)
                return {
                    "ok": False,
                    "status": f"HTTP {resp.status_code}",
                    "error_msg": detail.split("\n")[0],
                    "ttft_ms": 0.0,
                    "tps": 0.0,
                    "total_ms": 0.0,
                    "tokens": 0,
                    "cached_tokens": 0,
                }

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data_str)
                except Exception:
                    continue

                kind = chunk.get("type")

                if kind == "content_block_start":
                    blk = chunk.get("content_block") or {}
                    if blk.get("type") == "tool_use" and t_tool is None:
                        t_tool = time.perf_counter()
                        tool_name = blk.get("name") or ""
                elif kind == "content_block_delta":
                    delta = chunk.get("delta") or {}
                    # Time the two phases apart. On a Flash-class model the first
                    # token is usually `thinking`, so a single TTFT number cannot
                    # tell a model that answers immediately from one that spends
                    # 40s reasoning before writing anything — and that difference
                    # is the whole "thunderous overthinking" complaint. `t_first`
                    # still tracks whichever came first, so TTFT and TPS keep
                    # their existing meaning.
                    if delta.get("thinking"):
                        if t_first is None:
                            t_first = time.perf_counter()
                        if t_think is None:
                            t_think = time.perf_counter()
                        think_chars += len(delta["thinking"] or "")
                        chunks += 1
                    elif delta.get("text"):
                        if t_first is None:
                            t_first = time.perf_counter()
                        if t_answer is None:
                            t_answer = time.perf_counter()
                        answer_chars += len(delta["text"] or "")
                        chunks += 1
                elif kind == "message_delta":
                    usage = chunk.get("usage") or {}
                    completion_tokens = usage.get("output_tokens", completion_tokens)
                    cached = usage.get("cache_read_input_tokens", cached) or cached
                elif kind == "error":
                    err = chunk.get("error") or {}
                    return {
                        "ok": False,
                        "status": "Stream Error",
                        "error_msg": _clean_message(str(err.get("message") or err)),
                        "ttft_ms": 0.0,
                        "tps": 0.0,
                        "total_ms": 0.0,
                        "tokens": 0,
                        "cached_tokens": 0,
                    }

            t_end = time.perf_counter()
            total_duration = max(t_end - t0, 0.001)
            ttft_duration = max(t_first - t0, 0.001) if t_first else total_duration

            # A round that produced reasoning but no answer is not a success.
            # The stream was well formed, the upstream was happy, and the
            # model generated a plausible number of tokens — but it never
            # said anything, and the user gets nothing. Counting the
            # thinking deltas as an answer reported a healthy speed for a
            # model that had not answered: 18 of 20 probe rounds against
            # ClinePass ended this way, and the row said "✔ 2/2". The
            # budget is the usual reason (the model reasoned until it was
            # cut off), so the round is reported as what it is rather than
            # folded into the success rate.
            if not answer_chars and not tool_name:
                return {
                    "ok": False,
                    "status": "No Answer",
                    "error_msg": "budget spent on thinking; no answer was produced",
                    "tool_ms": round((t_tool - t0) * 1000, 1) if t_tool else None,
                    "tool_name": tool_name,
                    "think_ms": round((t_think - t0) * 1000, 1) if t_think else None,
                    "answer_ms": None,
                    "think_chars": think_chars,
                    "answer_chars": 0,
                    "ttft_ms": round(ttft_duration * 1000, 1),
                    "tps": 0.0,
                    "total_ms": round(total_duration * 1000, 1),
                    "tokens": 0,
                    "cached_tokens": cached,
                }

            tokens = completion_tokens or chunks or 1
            stream_interval = (t_end - t_first) if t_first else 0.0

            # Guard against server-side buffering: a server that waits seconds
            # then dumps every token at once makes stream_interval tiny while the
            # real generation spanned total_duration.
            #
            # Two different things end up here. A server that *stalled* and
            # then dumped has no decode window at all, and its speed is not
            # comparable with a streaming one — that is worth telling the
            # reader about. A round that simply produced three chunks or
            # fewer (the quick check asks for a five-word answer) has too
            # little output to time, which is a different remark: the speed
            # is meaningless, but nothing is broken. Both fall back to the
            # whole request for the arithmetic; only the first is a note
            # about the provider.
            stalled = (
                (total_duration > 1.2 and stream_interval < 0.25)
                or (total_duration > 2.0 and (ttft_duration / total_duration) > 0.85)
            )
            is_buffered = chunks <= 3 or stalled

            # Two clocks, two meanings. When the server streams, the honest
            # denominator is the decode window itself — first token to last —
            # which is what the harness measures for its own "tok/s" readout.
            # A buffering server gives us no usable window (it stalls, then
            # dumps), so there the only defensible denominator is the whole
            # request; that number is not comparable with a streaming one, and
            # `buffered` records which flavour this round produced.
            if chunks > 1 and stream_interval >= 0.05 and not is_buffered:
                decode_seconds = stream_interval
                decode_tokens = max(tokens - 1, 1)
            else:
                decode_seconds = total_duration
                decode_tokens = tokens
            tps = decode_tokens / max(decode_seconds, 0.05)

            return {
                "ok": True,
                "status": "OK",
                "error_msg": "",
                "tool_ms": round((t_tool - t0) * 1000, 1) if t_tool else None,
                "tool_name": tool_name,
                "think_ms": round((t_think - t0) * 1000, 1) if t_think else None,
                "answer_ms": round((t_answer - t0) * 1000, 1) if t_answer else None,
                "think_chars": think_chars,
                "answer_chars": answer_chars,
                "ttft_ms": round(ttft_duration * 1000, 1),
                "tps": round(tps, 1),
                "decode_ms": round(decode_seconds * 1000, 1),
                "decode_tokens": decode_tokens,
                "buffered": is_buffered,
                "stalled": stalled,
                "total_ms": round(total_duration * 1000, 1),
                "tokens": tokens,
                "cached_tokens": cached,
            }

    except httpx.ConnectError:
        where = base_url if native else f"{base_url} (bridge)"
        return {
            "ok": False,
            "status": "Unreachable",
            "error_msg": f"Cannot reach {where}",
            "ttft_ms": 0.0,
            "tps": 0.0,
            "total_ms": 0.0,
            "tokens": 0,
            "cached_tokens": 0,
        }
    except httpx.TimeoutException:
        return {
            "ok": False,
            "status": "Timeout",
            "error_msg": f"No response within {timeout_s:.0f}s",
            "ttft_ms": 0.0,
            "tps": 0.0,
            "total_ms": 0.0,
            "tokens": 0,
            "cached_tokens": 0,
        }
    except Exception as e:
        return {
            "ok": False,
            "status": "Error",
            "error_msg": str(e)[:60],
            "ttft_ms": 0.0,
            "tps": 0.0,
            "total_ms": 0.0,
            "tokens": 0,
            "cached_tokens": 0,
        }


async def probe_single_model(
    client: httpx.AsyncClient,
    model_spec: Dict[str, Any],
    base_url: str = DEFAULT_BRIDGE_URL,
    master_key: str = DEFAULT_MASTER_KEY,
    timeout_s: float = 10.0,
    rounds: int = DEFAULT_ROUNDS,
    max_tokens: int = DEFAULT_TOKENS,
    prompt: str = DEFAULT_PROMPT,
    semaphore: Optional[asyncio.Semaphore] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Multi-round streaming probe; statistical TTFT, TPS and stability.

    The candidate carries its own endpoint and credential, because the providers
    do not share one: bridged ones go through the local bridge, native ones
    straight to the vendor. base_url/master_key are the fallback for a spec that
    carries neither (and for tests).
    """
    wire_name = model_spec["wire_name"]
    spec_base = model_spec.get("endpoint") or base_url
    spec_key = model_spec.get("token") or master_key
    native = bool(model_spec.get("native"))

    async def _do():
        round_results: List[Dict[str, Any]] = []
        for r_idx in range(rounds):
            res = await _probe_single_round(
                client=client,
                wire_name=wire_name,
                base_url=spec_base,
                master_key=spec_key,
                prompt=prompt,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
                native=native,
                tools=tools,
            )
            round_results.append(res)
            # Short-circuit on permanent fatal errors
            if not res["ok"] and res["status"] in ("Unreachable", "HTTP 400", "HTTP 401", "HTTP 404"):
                break
            # A gateway error is the proxy's answer, not the model's: the same
            # request will bounce the same way 150ms later, and waiting for a
            # second round only delays the report the user is waiting for.
            if not res["ok"] and res["status"] in ("HTTP 502", "HTTP 504"):
                break
            if r_idx < rounds - 1:
                await asyncio.sleep(0.15)

        ok_rounds = [r for r in round_results if r["ok"]]
        successes = len(ok_rounds)

        if successes == rounds:
            status_str = "OK" if rounds == 1 else f"{rounds}/{rounds}"
        elif successes > 0:
            status_str = f"{successes}/{rounds}"
        else:
            status_str = round_results[-1]["status"] if round_results else "Fail"

        last_err = ""
        for r in reversed(round_results):
            if r.get("error_msg"):
                last_err = r["error_msg"]
                break

        avg_ttft = round(sum(r["ttft_ms"] for r in ok_rounds) / len(ok_rounds), 1) if ok_rounds else 0.0
        # `is not None`, not truthiness: a model that answers inside the same
        # millisecond records 0.0, and `if r.get(...)` would drop it and report
        # "-" for the fastest case in the table.
        _th = [r["think_ms"] for r in ok_rounds if r.get("think_ms") is not None]
        _an = [r["answer_ms"] for r in ok_rounds if r.get("answer_ms") is not None]
        avg_think = round(sum(_th) / len(_th), 1) if _th else None
        avg_answer = round(sum(_an) / len(_an), 1) if _an else None
        _tl = [r["tool_ms"] for r in ok_rounds if r.get("tool_ms") is not None]
        avg_tool = round(sum(_tl) / len(_tl), 1) if _tl else None
        acted = any(r.get("tool_name") for r in ok_rounds)
        # Aggregate the way the harness does: total tokens over total decode
        # time, not the mean of per-round rates. Averaging rates lets one
        # unrepresentative round pull the figure as hard as a long one, which
        # is how the same provider read 501 tok/s and then 194 minutes apart.
        decode_ms_sum = sum(r.get("decode_ms", 0.0) for r in ok_rounds)
        decode_tok_sum = sum(r.get("decode_tokens", 0) for r in ok_rounds)
        if decode_ms_sum > 0 and decode_tok_sum > 0:
            avg_tps = round(decode_tok_sum / (decode_ms_sum / 1000.0), 1)
        else:
            avg_tps = round(sum(r["tps"] for r in ok_rounds) / len(ok_rounds), 1) if ok_rounds else 0.0
        avg_total = round(sum(r["total_ms"] for r in ok_rounds) / len(ok_rounds), 1) if ok_rounds else 0.0
        avg_tokens = round(sum(r["tokens"] for r in ok_rounds) / len(ok_rounds)) if ok_rounds else 0
        cached = max((r["cached_tokens"] for r in ok_rounds), default=0)
        # A round that buffered is scored against the whole request, because
        # there was no decode window to measure. That number is not
        # comparable with a streaming one, and the flag has to reach the
        # reader: a footnote in a document is not a flag. Only a round that
        # actually stalled is worth the reader's attention — a round with
        # three chunks simply had too little output to time.
        buffered = any(r.get("stalled") for r in ok_rounds)

        return {
            **model_spec,
            "available": successes > 0,
            "status": status_str,
            "rounds_passed": successes,
            "rounds_total": rounds,
            "no_answer_rounds": sum(1 for r in round_results if r.get("status") == "No Answer"),
            "ttft_ms": avg_ttft,
            "think_ms": avg_think,
            "answer_ms": avg_answer,
            "tool_ms": avg_tool,
            "acted": acted,
            "tps": avg_tps,
            "total_ms": avg_total,
            "tokens": avg_tokens,
            "cached_tokens": cached,
            "buffered": buffered,
            "error_msg": last_err,
        }

    if semaphore:
        async with semaphore:
            return await _do()
    return await _do()


async def probe_all(
    models: List[Dict[str, Any]],
    base_url: str = DEFAULT_BRIDGE_URL,
    master_key: str = DEFAULT_MASTER_KEY,
    concurrency: int = 8,
    timeout_s: float = 0.0,
    rounds: int = DEFAULT_ROUNDS,
    max_tokens: int = DEFAULT_TOKENS,
    prompt: str = DEFAULT_PROMPT,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Probe every candidate concurrently, each against its own endpoint."""
    # The bridge and the Trae adapter live on this machine, and a request to
    # 127.0.0.1 must never be handed to the HTTP proxy: the proxy cannot
    # route a loopback destination and answers 502 with an empty body, which
    # reads exactly like a dead provider. Windows' inherited `no_proxy` uses
    # wildcard spellings that no HTTP stack understands, so the bypass is
    # enforced here rather than trusted from the environment.
    ensure_loopback_bypass()
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_keepalive_connections=concurrency, max_connections=concurrency * 2)
    async with httpx.AsyncClient(limits=limits, mounts=loopback_mounts()) as client:
        tasks = [
            probe_single_model(
                client=client,
                model_spec=m,
                base_url=base_url,
                master_key=master_key,
                timeout_s=timeout_s or default_timeout(max_tokens),
                rounds=rounds,
                max_tokens=max_tokens,
                prompt=prompt,
                semaphore=sem,
                tools=tools,
            )
            for m in models
        ]
        return await asyncio.gather(*tasks)


def sort_results(results: List[Dict[str, Any]], sort_key: str = "default") -> List[Dict[str, Any]]:
    """Sort probe results with available and 100% stable models on top."""
    def key_fn(r: Dict[str, Any]):
        avail = 0 if r.get("available") else 1
        passed = r.get("rounds_passed", 1 if r.get("available") else 0)
        total = r.get("rounds_total", 1)
        ratio = passed / total if total > 0 else 0.0
        stability_rank = -ratio  # 1.0 (100% passed) ranks before 0.5 before 0.0

        tps = r.get("tps", 0.0)
        ttft = r.get("ttft_ms", 999999.0)
        tot = r.get("total_ms", 999999.0)

        if sort_key == "speed":
            return (avail, stability_rank, -tps, ttft)
        elif sort_key == "ttft":
            return (avail, stability_rank, ttft, -tps)
        elif sort_key == "total":
            return (avail, stability_rank, tot)
        else:
            # default: balanced score of throughput penalised by high TTFT latency
            score = tps / (1.0 + (ttft / 1000.0)) if ttft < 999999 else 0.0
            return (avail, stability_rank, -score, ttft)

    return sorted(results, key=key_fn)


def _status_label(raw_stat: str) -> str:
    """A status short enough for the column, complete enough to read.

    The old code cut the status to seven characters, which turned "HTTP 502"
    into "HTTP 50" — a code that does not exist, on the one failure the user
    most needs to read. Codes are short; the label is what has to give.
    """
    text = (raw_stat or "Fail").strip()
    if text.startswith("HTTP "):
        code = text[5:].split(" ")[0].strip()
        return f"✖ HTTP {code}" if code else "✖ HTTP"
    if text == "Stream Error":
        return "✖ StreamErr"
    if text == "Unreachable":
        return "✖ Unreach"
    if len(text) > 10:
        text = text[:10]
    return f"✖ {text}"


def print_table(
    results: List[Dict[str, Any]],
    sort_key: str = "default",
    rounds: int = DEFAULT_ROUNDS,
    tokens: int = DEFAULT_TOKENS,
) -> Optional[Dict[str, Any]]:
    """Render terminal report with status, TTFT, TPS, and recommendation."""
    sorted_res = sort_results(results, sort_key)

    col_prov = 12
    col_name = 28
    col_stat = 12
    col_ttft = 10
    col_tps = 14
    col_tot = 10
    col_think = 9
    col_ctx = 8
    col_vis = 6

    header = (
        f"{pad_right(bold('Provider'), col_prov)} "
        f"{pad_right(bold('Model'), col_name)} "
        f"{pad_right(bold('Status'), col_stat)} "
        f"{pad_left(bold('TTFT'), col_ttft)} "
        f"{pad_left(bold('Speed'), col_tps)} "
        f"{pad_left(bold('Total'), col_tot)} "
        f"{pad_left(bold('Think'), col_think)} "
        f"{pad_left(bold('Context'), col_ctx)} "
        f"{pad_left(bold('Vision'), col_vis)}"
    )

    sep_len = (col_prov + col_name + col_stat + col_ttft + col_tps + col_tot
               + col_think + col_ctx + col_vis + 8)
    line = dim("─" * sep_len)

    print()
    print(header)
    print(line)

    best_model = None

    for r in sorted_res:
        prov = r.get("provider", "-")[:col_prov-1]
        name = r.get("display_name", r.get("wire_name", "-"))[:col_name-1]
        ctx = str(r.get("ctx", "-"))
        vis = "yes" if r.get("vision") else "no"

        passed = r.get("rounds_passed", 1 if r.get("available") else 0)
        tot_rounds = r.get("rounds_total", rounds)
        raw_stat = r.get("status", "")

        if r.get("available"):
            # Candidate for best model: prefer 100% stability
            if not best_model:
                best_model = r
            elif best_model.get("rounds_passed", 0) < best_model.get("rounds_total", 1) and passed == tot_rounds:
                best_model = r

            if tot_rounds > 1:
                stat = green(f"✔ {passed}/{tot_rounds}") if passed == tot_rounds else yellow(f"⚠ {passed}/{tot_rounds}")
            else:
                stat = green("✔ OK")

            ttft_val = r["ttft_ms"]
            tps_val = r["tps"]
            tot_val = r["total_ms"]

            # "Think" is the wait before the model writes its first useful
            # token. A model that reasons for 4s and then answers in 0.1s is
            # slow to be useful even though its throughput looks fine.
            think_val = r.get("think_ms")
            if think_val is None:
                think_str = "-"                  # the model did not reason at all
            elif think_val >= 1000:
                think_str = f"{think_val/1000:.1f}s"
            else:
                think_str = f"{think_val:.0f}ms"
            if think_val is not None and think_val > 5000:
                think_str = red(think_str)
            elif think_val is not None and think_val > 1500:
                think_str = yellow(think_str)

            # Color coding for TTFT
            if ttft_val < 600:
                ttft_str = green(f"{ttft_val:.0f}ms")
            elif ttft_val < 1500:
                ttft_str = yellow(f"{ttft_val:.0f}ms")
            else:
                ttft_str = cyan(f"{ttft_val/1000:.1f}s")

            # Color coding for Speed
            if tps_val >= 50:
                tps_str = green(f"{tps_val:.1f} tok/s")
            elif tps_val >= 25:
                tps_str = yellow(f"{tps_val:.1f} tok/s")
            else:
                tps_str = f"{tps_val:.1f} tok/s"

            tot_str = f"{tot_val:.0f}ms" if tot_val < 1000 else f"{tot_val/1000:.1f}s"

            print(
                f"{pad_right(cyan(prov), col_prov)} "
                f"{pad_right(name, col_name)} "
                f"{pad_right(stat, col_stat)} "
                f"{pad_left(ttft_str, col_ttft)} "
                f"{pad_left(tps_str, col_tps)} "
                f"{pad_left(tot_str, col_tot)} "
                f"{pad_left(think_str, col_think)} "
                f"{pad_left(ctx, col_ctx)} "
                f"{pad_left(vis, col_vis)}"
            )
        else:
            if tot_rounds > 1 and "HTTP" not in raw_stat and "Unreachable" not in raw_stat and "Timeout" not in raw_stat:
                stat = red(f"✖ 0/{tot_rounds}")
            elif "Unreachable" in raw_stat:
                # Endpoint refused the connection: bridge down for a bridged
                # provider, network/DNS for a native one.
                stat = yellow("✖ Unreach")
            elif "Timeout" in raw_stat:
                stat = red("✖ Timeout")
            else:
                stat = red(_status_label(raw_stat))

            err = r.get("error_msg") or ""
            max_err_width = col_ttft + col_tps + col_tot + col_think + col_ctx + col_vis + 5
            print(
                f"{pad_right(dim(prov), col_prov)} "
                f"{pad_right(dim(name), col_name)} "
                f"{pad_right(stat, col_stat)} "
                f"{pad_right(dim(err[:max_err_width]), max_err_width)}"
            )

    print(line)

    avail_count = sum(1 for r in results if r.get("available"))
    total_count = len(results)
    rounds_info = f"{rounds} rounds, ~{tokens} tok" if rounds > 1 else f"1 round, ~{tokens} tok"

    print(f"Summary: {bold(str(avail_count))}/{total_count} models responsive ({rounds_info}).")

    # A model that answered in one round and failed in the other still shows
    # as "⚠ 1/2" with a speed, and the speed alone does not say what went
    # wrong in the other round. Say it here.
    partial = [r for r in results
               if r.get("available") and r.get("rounds_passed", 0) < r.get("rounds_total", rounds)]
    if partial:
        parts = []
        for r in partial:
            name = r.get("display_name", "-")
            n = r.get("rounds_passed", 0)
            tot = r.get("rounds_total", rounds)
            if r.get("no_answer_rounds"):
                why = "no answer within the budget"
            else:
                why = (r.get("error_msg") or r.get("status") or "a round failed").split("\n")[0]
            parts.append(f"{name} ({n}/{tot} — {why})")
        print(yellow(f"  ⚠ rounds that failed: {', '.join(parts)}"))

    # A stalled round has no decode window: the server thinks for seconds and
    # then releases the whole answer at once, so the speed was scored against
    # the whole request and is not comparable with the streaming rows.
    buf = [r for r in results if r.get("available") and r.get("buffered")]
    if buf:
        names = ", ".join(f"{r.get('provider')}/{r.get('display_name', '-')}" for r in buf)
        print(yellow(f"  ⚠ speed measured against the whole request (no decode window to measure): {names}"))

    if best_model:
        passed = best_model.get("rounds_passed", 1)
        tot_r = best_model.get("rounds_total", 1)
        stab_str = f"Stability: {green(f'{passed}/{tot_r}')} | " if tot_r > 1 else ""
        print(
            f"🏆 Recommended: {green(bold(best_model['display_name']))} "
            f"({best_model.get('provider')} | "
            f"{stab_str}"
            f"TTFT: {green(str(best_model['ttft_ms']) + 'ms')} | "
            f"Speed: {green(str(best_model['tps']) + ' tok/s')})"
        )
    print()
    return best_model


def print_agent_table(results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Agent readiness: did it act, and how long until it did.

    TTFT says nothing here — reasoning tokens do not advance the task, so a model
    can emit a token instantly and still never call a tool. `1st tool: NEVER`
    means it answered the request with prose, which wastes the turn outright.
    """
    def key(r):
        return (0 if r.get("acted") else 1,
                r.get("tool_ms") if r.get("tool_ms") is not None else 1e9)
    ordered = sorted(results, key=key)

    print()
    print(f"{pad_right(bold('Provider'), 12)} {pad_right(bold('Model'), 30)} "
          f"{pad_left(bold('Think'), 8)} {pad_left(bold('1st tool'), 9)} "
          f"{pad_left(bold('Total'), 8)}  {bold('verdict')}")
    print(dim("─" * 92))
    for r in ordered:
        if not r.get("available"):
            print(f"{pad_right(dim(r.get('provider', '-')), 12)} "
                  f"{pad_right(dim(r.get('display_name', '-')[:30]), 30)} "
                  f"{pad_left('', 8)} {pad_left(red('NEVER'), 9)} "
                  f"{pad_left('', 8)}  {red(str(r.get('status', 'fail'))[:40])}")
            continue
        th = r.get("think_ms")
        th_s = "-" if th is None else (f"{th/1000:.1f}s" if th >= 1000 else f"{th:.0f}ms")
        tl = r.get("tool_ms")
        if tl is None:
            tl_s, verdict = red("NEVER"), red("prose only — wastes the turn")
        else:
            tl_s = f"{tl/1000:.1f}s" if tl >= 1000 else f"{tl:.0f}ms"
            tl_s = green(tl_s) if tl <= 3000 else (yellow(tl_s) if tl <= 10000 else red(tl_s))
            verdict = (green("acts promptly") if tl <= 3000
                       else yellow("acts, but slowly") if tl <= 10000
                       else red("too slow to act"))
        tot = r.get("total_ms", 0) / 1000
        print(f"{pad_right(cyan(r.get('provider', '-')[:12]), 12)} "
              f"{pad_right(r.get('display_name', '-')[:30], 30)} "
              f"{pad_left(th_s, 8)} {pad_left(tl_s, 9)} "
              f"{pad_left(f'{tot:.1f}s', 8)}  {verdict}")

    acted = [r for r in results if r.get("acted")]
    stalled = [r for r in results if r.get("available") and not r.get("acted")]
    print(dim("─" * 92))
    print(f"Summary: {bold(str(len(acted)))}/{len(results)} called the tool.")
    if stalled:
        print(red(f"  never acted: {', '.join(r['provider'] for r in stalled)}"))
    if acted:
        best = min(acted, key=lambda r: r["tool_ms"] or 1e9)
        print(f"🏆 Fastest to act: {green(bold(best['display_name']))} "
              f"({best['provider']} | {best['tool_ms']/1000:.1f}s | {best.get('tool_name') or 'tool'})")
    print()
    return min(acted, key=lambda r: r["tool_ms"] or 1e9) if acted else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", default=None, help="Provider name (trae, minimax), model alias, or omit for primary tiers")
    ap.add_argument("--all", action="store_true", help="Probe all known models across all providers")
    ap.add_argument("-n", "--rounds", type=int, default=None, help=f"Probe rounds per model (default: {DEFAULT_ROUNDS})")
    ap.add_argument("--tokens", type=int, default=None, help=f"Max tokens per probe (default: {DEFAULT_TOKENS})")
    ap.add_argument("-q", "--quick", action="store_true", help=f"Quick mode: 1 round, {QUICK_TOKENS} tokens (fast check)")
    ap.add_argument("-b", "--bench", action="store_true", help=f"Benchmark mode: {BENCH_ROUNDS} rounds, {BENCH_TOKENS} tokens (rigorous test)")
    ap.add_argument("--sort", choices=["speed", "ttft", "total", "default"], default="speed", help="Sort order (default: speed)")
    ap.add_argument("--timeout", type=float, default=None,
                    help="Per-round timeout in seconds (default: scales with --tokens)")
    ap.add_argument("--concurrency", type=int, default=8, help="Max concurrent model probes (default: 8)")
    ap.add_argument("--base-url", default=DEFAULT_BRIDGE_URL, help=f"Universal bridge base URL (default: {DEFAULT_BRIDGE_URL})")
    ap.add_argument("-s", "--switch", action="store_true", help="Automatically switch active provider/model to recommended winner")
    ap.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    ap.add_argument("--agent", action="store_true",
                    help="Agent readiness: time to first tool call, and whether one comes at all")

    args = ap.parse_args()

    # Resolve mode presets
    if args.quick:
        rounds = 1
        tokens = QUICK_TOKENS
        prompt = QUICK_PROMPT
    elif args.bench:
        rounds = BENCH_ROUNDS
        tokens = BENCH_TOKENS
        prompt = DEFAULT_PROMPT
    else:
        rounds = args.rounds if args.rounds is not None else DEFAULT_ROUNDS
        tokens = args.tokens if args.tokens is not None else DEFAULT_TOKENS
        prompt = DEFAULT_PROMPT

    # Agent mode asks a different question, so it brings its own prompt and a
    # tool schema. One round by default: the point is whether and how fast the
    # model acts, not how stable that is across repeats.
    tools = None
    if args.agent:
        tools = AGENT_TOOLS
        prompt = AGENT_PROMPT
        if args.rounds is None:
            rounds = 1
        if args.tokens is None:
            tokens = 400

    candidates = get_configured_models(target=args.target, all_models=args.all)
    if not candidates:
        print(f"No models found matching target '{args.target}'. Run 'ccp models' to see options.", file=sys.stderr)
        return 1

    # The deadline follows the work: a probe that asks for more tokens needs
    # a longer first byte, and cutting it short reports a slow model as a
    # dead one.
    timeout_s = args.timeout if args.timeout is not None else default_timeout(tokens)

    if not args.json:
        label = args.target if args.target else ("all models" if args.all else "primary provider tiers")
        mode_label = f"{rounds} rounds, ~{tokens} tokens"
        print(f"Probing availability & speed for {bold(str(len(candidates)))} models ({label} | {mode_label})...")

    results = asyncio.run(
        probe_all(
            models=candidates,
            base_url=args.base_url,
            concurrency=args.concurrency,
            timeout_s=timeout_s,
            rounds=rounds,
            max_tokens=tokens,
            prompt=prompt,
            tools=tools,
        )
    )

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    best = (print_agent_table(results) if args.agent
            else print_table(results, sort_key=args.sort, rounds=rounds, tokens=tokens))

    if args.switch and best and best.get("display_name"):
        prov = best.get("provider", "")
        model = best.get("display_name", "")
        if prov == "group":
            print(f"SWITCH_TO=MODEL {model}")
        else:
            print(f"SWITCH_TO={prov} {model}")

    return 0 if any(r.get("available") for r in results) else 2


if __name__ == "__main__":
    sys.exit(main())
