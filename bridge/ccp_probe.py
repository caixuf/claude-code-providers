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

REPO_DIR = pathlib.Path(__file__).resolve().parent.parent
PROFILES_PATH = REPO_DIR / "registry" / "profiles.yaml"
MODELS_PATH = REPO_DIR / "registry" / "models.yaml"

DEFAULT_BRIDGE_URL = "http://127.0.0.1:4000"
DEFAULT_MASTER_KEY = "sk-cmdc-litellm-local"

DEFAULT_PROMPT = "Explain what an API is in 2 sentences."
QUICK_PROMPT = "Count from 1 to 5."
PROBE_PROMPT = DEFAULT_PROMPT

DEFAULT_ROUNDS = 2
DEFAULT_TOKENS = 60
QUICK_TOKENS = 20
BENCH_ROUNDS = 3
BENCH_TOKENS = 100
MAX_TOKENS = DEFAULT_TOKENS

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
    `qianfan-deepseek-v4.1-flash.json`), and a rename in the registry renames
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

    # 3. Target that names a specific model (or a raw upstream id / alias)
    if target and not known_provider and not known_group:
        matched: List[Dict[str, Any]] = []
        for name, prov in providers.items():
            for sel_name, upstream in prov.models:
                if target_lower in (
                    sel_name.lower(),
                    upstream.lower(),
                    provider_wire_name(prov, sel_name, upstream).lower(),
                    f"{name}-{sel_name}".lower(),
                ):
                    matched.append(_spec(name, sel_name, upstream, prov, p_doc))
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
        return str(err.get("message") or "")[:120]
    if isinstance(err, str):
        return err[:120]
    return str(doc.get("message") or "")[:120]


async def _probe_single_round(
    client: httpx.AsyncClient,
    wire_name: str,
    base_url: str,
    master_key: str,
    prompt: str,
    max_tokens: int,
    timeout_s: float,
    native: bool = False,
) -> Dict[str, Any]:
    """One streaming round over Anthropic Messages, measuring TTFT / TPS.

    Anthropic Messages rather than OpenAI chat-completions on purpose: it is the
    one protocol every provider here speaks — the native ones point straight at
    an /anthropic endpoint, and the bridged ones reach it through LiteLLM. A
    chat-completions probe would test a path the user's session never takes, and
    would 400 on every native catalogue model for lack of a LiteLLM entry.
    """
    payload = {
        "model": wire_name,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": True,
    }
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if master_key:
        # Both spellings: the bridge wants x-api-key, several vendor endpoints
        # accept either, and sending both is accepted everywhere here.
        headers["x-api-key"] = master_key
        headers["Authorization"] = f"Bearer {master_key}"
    url = f"{base_url.rstrip('/')}/v1/messages"

    t0 = time.perf_counter()
    t_first = None
    completion_tokens = 0
    chunks = 0
    cached = 0

    try:
        async with client.stream("POST", url, json=payload, headers=headers, timeout=timeout_s) as resp:
            if resp.status_code != 200:
                detail = _err_detail(await resp.aread())
                return {
                    "ok": False,
                    "status": f"HTTP {resp.status_code}",
                    "error_msg": detail.split("\n")[0][:60],
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

                if kind == "content_block_delta":
                    delta = chunk.get("delta") or {}
                    # `thinking` counts: it is real generation, and on these
                    # models it is what arrives first.
                    piece = delta.get("text") or delta.get("thinking") or ""
                    if piece:
                        if t_first is None:
                            t_first = time.perf_counter()
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
                        "error_msg": str(err.get("message") or err)[:60],
                        "ttft_ms": 0.0,
                        "tps": 0.0,
                        "total_ms": 0.0,
                        "tokens": 0,
                        "cached_tokens": 0,
                    }

            t_end = time.perf_counter()
            total_duration = max(t_end - t0, 0.001)
            ttft_duration = max(t_first - t0, 0.001) if t_first else total_duration

            tokens = completion_tokens or chunks or 1
            stream_interval = (t_end - t_first) if t_first else 0.0

            # Guard against server-side buffering: a server that waits seconds
            # then dumps every token at once makes stream_interval tiny while the
            # real generation spanned total_duration.
            is_buffered = (
                chunks <= 3
                or (total_duration > 1.2 and stream_interval < 0.25)
                or (total_duration > 2.0 and (ttft_duration / total_duration) > 0.85)
            )

            if chunks > 1 and stream_interval >= 0.05 and not is_buffered:
                tps = max(tokens - 1, 1) / stream_interval
            else:
                tps = tokens / max(total_duration, 0.05)

            return {
                "ok": True,
                "status": "OK",
                "error_msg": "",
                "ttft_ms": round(ttft_duration * 1000, 1),
                "tps": round(tps, 1),
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
            )
            round_results.append(res)
            # Short-circuit on permanent fatal errors
            if not res["ok"] and res["status"] in ("Unreachable", "HTTP 400", "HTTP 401", "HTTP 404"):
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
        avg_tps = round(sum(r["tps"] for r in ok_rounds) / len(ok_rounds), 1) if ok_rounds else 0.0
        avg_total = round(sum(r["total_ms"] for r in ok_rounds) / len(ok_rounds), 1) if ok_rounds else 0.0
        avg_tokens = round(sum(r["tokens"] for r in ok_rounds) / len(ok_rounds)) if ok_rounds else 0
        cached = max((r["cached_tokens"] for r in ok_rounds), default=0)

        return {
            **model_spec,
            "available": successes > 0,
            "status": status_str,
            "rounds_passed": successes,
            "rounds_total": rounds,
            "ttft_ms": avg_ttft,
            "tps": avg_tps,
            "total_ms": avg_total,
            "tokens": avg_tokens,
            "cached_tokens": cached,
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
    timeout_s: float = 10.0,
    rounds: int = DEFAULT_ROUNDS,
    max_tokens: int = DEFAULT_TOKENS,
    prompt: str = DEFAULT_PROMPT,
) -> List[Dict[str, Any]]:
    """Probe every candidate concurrently, each against its own endpoint."""
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_keepalive_connections=concurrency, max_connections=concurrency * 2)
    async with httpx.AsyncClient(limits=limits) as client:
        tasks = [
            probe_single_model(
                client=client,
                model_spec=m,
                base_url=base_url,
                master_key=master_key,
                timeout_s=timeout_s,
                rounds=rounds,
                max_tokens=max_tokens,
                prompt=prompt,
                semaphore=sem,
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
    col_stat = 10
    col_ttft = 10
    col_tps = 14
    col_tot = 10
    col_ctx = 8
    col_vis = 6

    header = (
        f"{pad_right(bold('Provider'), col_prov)} "
        f"{pad_right(bold('Model'), col_name)} "
        f"{pad_right(bold('Status'), col_stat)} "
        f"{pad_left(bold('TTFT'), col_ttft)} "
        f"{pad_left(bold('Speed'), col_tps)} "
        f"{pad_left(bold('Total'), col_tot)} "
        f"{pad_left(bold('Context'), col_ctx)} "
        f"{pad_left(bold('Vision'), col_vis)}"
    )

    sep_len = col_prov + col_name + col_stat + col_ttft + col_tps + col_tot + col_ctx + col_vis + 7
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
                stat = red(f"✖ {raw_stat[:7]}")

            err = r.get("error_msg") or ""
            max_err_width = col_ttft + col_tps + col_tot + col_ctx + col_vis + 4
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", default=None, help="Provider name (trae, qianfan), model alias, or omit for primary tiers")
    ap.add_argument("--all", action="store_true", help="Probe all known models across all providers")
    ap.add_argument("-n", "--rounds", type=int, default=None, help=f"Probe rounds per model (default: {DEFAULT_ROUNDS})")
    ap.add_argument("--tokens", type=int, default=None, help=f"Max tokens per probe (default: {DEFAULT_TOKENS})")
    ap.add_argument("-q", "--quick", action="store_true", help=f"Quick mode: 1 round, {QUICK_TOKENS} tokens (fast check)")
    ap.add_argument("-b", "--bench", action="store_true", help=f"Benchmark mode: {BENCH_ROUNDS} rounds, {BENCH_TOKENS} tokens (rigorous test)")
    ap.add_argument("--sort", choices=["speed", "ttft", "total", "default"], default="speed", help="Sort order (default: speed)")
    ap.add_argument("--timeout", type=float, default=10.0, help="Per-round timeout in seconds (default: 10.0)")
    ap.add_argument("--concurrency", type=int, default=8, help="Max concurrent model probes (default: 8)")
    ap.add_argument("--base-url", default=DEFAULT_BRIDGE_URL, help=f"Universal bridge base URL (default: {DEFAULT_BRIDGE_URL})")
    ap.add_argument("-s", "--switch", action="store_true", help="Automatically switch active provider/model to recommended winner")
    ap.add_argument("--json", action="store_true", help="Output machine-readable JSON")

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

    candidates = get_configured_models(target=args.target, all_models=args.all)
    if not candidates:
        print(f"No models found matching target '{args.target}'. Run 'ccp models' to see options.", file=sys.stderr)
        return 1

    if not args.json:
        label = args.target if args.target else ("all models" if args.all else "primary provider tiers")
        mode_label = f"{rounds} rounds, ~{tokens} tokens"
        print(f"Probing availability & speed for {bold(str(len(candidates)))} models ({label} | {mode_label})...")

    results = asyncio.run(
        probe_all(
            models=candidates,
            base_url=args.base_url,
            concurrency=args.concurrency,
            timeout_s=args.timeout,
            rounds=rounds,
            max_tokens=tokens,
            prompt=prompt,
        )
    )

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    best = print_table(results, sort_key=args.sort, rounds=rounds, tokens=tokens)

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
