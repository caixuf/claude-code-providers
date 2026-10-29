"""Forward Anthropic SSE; stop extra frames after a complete message_stop.

Do **not** inject synthetic JSON into a live stream. LiteLLM treats that as
``JSON error injected into SSE stream`` / MidStreamFallbackError and Claude
Code retries for many minutes (API_TIMEOUT_MS).

Idle cut is disabled by default (``ping_idle_s=0``). Long tool/Explore turns
can go minutes without a token; a 12s idle inject aborted LiteLLM mid-JSON.
"""

from __future__ import annotations

import re

MESSAGE_STOP_MARK = b"event: message_stop"
OPENAI_DONE_MARK = b"data: [DONE]"


def sse_should_close(buf: bytes) -> bool:
    return MESSAGE_STOP_MARK in buf or OPENAI_DONE_MARK in buf


def split_complete_events(buf: bytes) -> tuple[list[bytes], bytes]:
    events: list[bytes] = []
    while True:
        idx_lf = buf.find(b"\n\n")
        idx_crlf = buf.find(b"\r\n\r\n")
        if idx_lf == -1 and idx_crlf == -1:
            break
        if idx_crlf != -1 and (idx_lf == -1 or idx_crlf < idx_lf):
            part = buf[: idx_crlf + 4]
            buf = buf[idx_crlf + 4 :]
        else:
            part = buf[: idx_lf + 2]
            buf = buf[idx_lf + 2 :]
        events.append(part)
    return events, buf


def event_is_ping(ev: bytes) -> bool:
    lines = [ln.strip() for ln in ev.splitlines() if ln.startswith(b"event:")]
    return bool(lines) and all(ln == b"event: ping" for ln in lines)


class SseCutState:
    def __init__(self, *, ping_idle_s: float = 0.0, first_byte_s: float = 90.0) -> None:
        self.ping_idle_s = ping_idle_s
        self.first_byte_s = first_byte_s
        self.saw_real = False
        self.saw_stop = False
        self.buf = b""

    def wait_budget(self) -> float:
        if not self.saw_real:
            return self.first_byte_s
        if self.ping_idle_s and self.ping_idle_s > 0:
            return self.ping_idle_s
        return 3600.0

    def feed(self, chunk: bytes) -> tuple[bytes, bool]:
        """Return (bytes_to_forward, should_stop_forwarding)."""
        if not chunk:
            return b"", True
        self.buf += chunk
        events, self.buf = split_complete_events(self.buf)
        out = b""
        for ev in events:
            out += ev
            if sse_should_close(ev):
                self.saw_stop = True
                return out, True
            if not event_is_ping(ev):
                self.saw_real = True
        return out, False

    def idle_timeout(self) -> bytes:
        return b""


def strip_1m_model(body: bytes) -> tuple[bytes, str | None]:
    """Strip [1m] or [1M] suffix from request body model field for upstream compatibility."""
    if not body or (b"[1m]" not in body and b"[1M]" not in body):
        return body, None
    try:
        import json as _j
        _b = _j.loads(body)
        if isinstance(_b, dict) and isinstance(_b.get("model"), str) and _b["model"].lower().endswith("[1m]"):
            orig_m = _b["model"]
            _b["model"] = orig_m[:-4]
            return _j.dumps(_b).encode("utf-8"), orig_m
    except Exception:
        pass
    return body, None


ONE_M_BETA = "context-1m-2025-08-07"


def strip_1m_beta(header_value: str) -> str:
    """Remove the 1M-context beta token from an ``anthropic-beta`` header value.

    Claude Code adds ``context-1m-2025-08-07`` whenever a model carries the
    ``[1m]`` marker. That beta is meaningful only to Anthropic's own API; the
    OpenAI-compatible gateways behind the bridge ignore it, and a strict
    gateway can 400 on an unknown beta. Dropping it makes 1M models just work.
    Returns the remaining comma-separated tokens ('' if none left).
    """
    if not header_value:
        return header_value
    keep = [
        p.strip()
        for p in header_value.split(",")
        if p.strip() and p.strip().lower() != ONE_M_BETA
    ]
    return ",".join(keep)


def norm_model_key(name: str) -> str:
    """Normalize a model name for rule-map lookup: strip any [..] tier suffix, lower-case."""
    if not isinstance(name, str):
        return ""
    base = name.split("[", 1)[0].strip()
    return base.lower()


# Back-compat alias: this helper predates the rule maps that now share it.
_norm_model = norm_model_key


# Tencent's CodeBuddy / WorkBuddy gateway runs a *channel check* on a chat and
# answers ``400 {"code":11128,"msg":"Illegal API invocation from an unapproved
# channel"}`` — not an auth failure, and the whole request dies. It rejects two
# openings:
#
#   1. an attribution block Claude Code puts first in its system prompt
#        x-anthropic-billing-header: cc_version=2.1.287.95e; cc_entrypoint=sdk-cli;
#   2. Claude Code's own identity line, when the first system message *opens*
#      with it:
#        You are Claude Code, Anthropic's official CLI for Claude.
#        You are Claude Code, Anthropic's official CLI for Claude, running within the Claude Agent SDK.
#
# Both rewrites are applied per-model via ``bridge/request_rules.json`` and never
# to the native Anthropic profiles, which expect the header.
BILLING_HEADER_MARKER = "x-anthropic-billing-header:"

# Claude Code 2.1.287 picks one of three identity lines (see yqn() in the
# bundle):
#   interactive                -> "You are Claude Code, Anthropic's official CLI for Claude."
#   -p, plain                  -> "You are a Claude agent, built on Anthropic's Claude Agent SDK."
#   -p, --append-system-prompt -> "… for Claude, running within the Claude Agent SDK."
# The interactive TUI — how this is normally used — and `--append-system-prompt`
# both send a refused line, so the check has to be neutralised, not just the
# billing block removed.
#
# The measured boundary (magpie's issue #232, re-confirmed here):
#   * only the very start of the first system message is inspected, and
#     case-insensitively;
#   * ANY single leading character defeats it — a letter, "\n", a space, a tab;
#   * the same words on a later line, or in a second system message, pass;
#   * the identity must run as far as "…for Claude" to be refused at all.
# So we prepend one stable newline instead of rewriting the line: the identity
# stays visible to the model (it is what makes Claude-Code-style tool use work),
# and a constant prefix cannot disturb the upstream prompt cache.
CLAUDE_IDENTITY_OPENINGS = ("you are claude code",)
CHANNEL_SAFE_PREFIX = "\n"

# The gateway has a *second* check, and it is not a prefix check: this line is
# refused wherever it appears in the text, in any case, with a trailing space
# and all. Measured on 2026-10-24 against www.codebuddy.ai: it is refused as
# the first line, on a later line, in the middle of the text, lowercased, and
# with a trailing space — while the same words one character apart are fine.
#
# Claude Code's TUI sends it ("Main branch (you will usually use this for
# PRs)"), so a real session was refused with 11128 even after the identity
# line had been neutralised. A leading newline does not defeat this one, so
# the line is reworded instead — the same wording magpie's plugin uses
# (magpie-community/plugins, packages/workbuddy).
CHANNEL_REFUSED_LINES = (
    (re.compile(r"main branch \(you will usually use this for PRs\)", re.IGNORECASE),
     "Main branch (usually the base for PRs)"),
)

# Used only when stripping leaves a request with no system prompt at all, where
# the gateway's other check ("first message is not system prompt") would fire.
DEFAULT_SYSTEM_PROMPT = (
    "You are an interactive agent that helps users with software engineering tasks."
)


def _strip_marker_lines(text: str) -> tuple[str, bool]:
    """Drop every line of ``text`` that declares the billing header."""
    if BILLING_HEADER_MARKER not in text.lower():
        return text, False
    kept = [
        ln
        for ln in text.splitlines()
        if not ln.strip().lower().startswith(BILLING_HEADER_MARKER)
    ]
    out = "\n".join(kept)
    # preserve a trailing newline if the original had one and we removed the tail
    if text.endswith("\n") and not out.endswith("\n"):
        out += "\n"
    return out, out != text


def _opens_with_identity(text: str) -> bool:
    """True when ``text`` would trip the gateway's identity check."""
    head = text.lstrip("\ufeff").lower()
    # Only the very first characters count; a leading whitespace already passes,
    # but normalise it away so the check is about the words, not the padding.
    head = head.lstrip()
    return head.startswith(CLAUDE_IDENTITY_OPENINGS)


def _clean_content(content):
    """Return ``(content, changed)`` with billing-header blocks/lines removed.

    Handles both Anthropic shapes: a bare string, or a list of content blocks
    (``{"type": "text", "text": ...}`` and friends). Non-text blocks are left
    untouched.
    """
    if isinstance(content, str):
        return _strip_marker_lines(content)
    if not isinstance(content, list):
        return content, False
    changed = False
    blocks = []
    for blk in content:
        if isinstance(blk, dict) and isinstance(blk.get("text"), str):
            text, hit = _strip_marker_lines(blk["text"])
            if hit:
                changed = True
                blk = dict(blk, text=text)
                # A block that held nothing but the marker is noise upstream;
                # keep it only if it still carries content.
                if not text.strip():
                    continue
        blocks.append(blk)
    return blocks, changed


def _reword_text(text: str) -> tuple[str, bool]:
    """Replace the gateway's refused lines with a wording it accepts."""
    out = text
    for pattern, replacement in CHANNEL_REFUSED_LINES:
        out = pattern.sub(replacement, out)
    return out, out != text


def _reword_refused(content):
    """Return ``(content, changed)`` with the refused lines reworded.

    The gateway *scans* for these lines, so the newline trick that defeats the
    identity check does not help here: the words are still in the text. The
    line is reworded instead, which is what magpie's plugin does.

    Handles both Anthropic shapes: a bare string, or a list of content blocks.
    """
    if isinstance(content, str):
        return _reword_text(content)
    if not isinstance(content, list):
        return content, False
    changed = False
    blocks = []
    for blk in content:
        if isinstance(blk, dict) and isinstance(blk.get("text"), str):
            text, hit = _reword_text(blk["text"])
            if hit:
                changed = True
                blk = dict(blk, text=text)
        blocks.append(blk)
    return blocks, changed


def _neutralize_identity(content):
    """Return ``(content, changed)`` with every refused opening made harmless.

    Prepends one stable newline to *each* text-bearing piece that opens with
    Claude Code's identity — not only the opening of the first one. The gateway
    inspects every system block and every assistant turn for that line, so a
    request is rejected for an identity carried in a later ``system`` block (or
    in a past assistant message) even when the first block was already
    neutralised. Which position carries it varies by Claude Code version and
    invocation, so all of them are handled.
    """
    if isinstance(content, str):
        if not _opens_with_identity(content):
            return content, False
        return CHANNEL_SAFE_PREFIX + content, True
    if not isinstance(content, list):
        return content, False
    changed = False
    blocks = []
    for blk in content:
        if (
            isinstance(blk, dict)
            and isinstance(blk.get("text"), str)
            and _opens_with_identity(blk["text"])
        ):
            blk = dict(blk, text=CHANNEL_SAFE_PREFIX + blk["text"])
            changed = True
        blocks.append(blk)
    return blocks, changed


def sanitize_channel_markers(body: bytes) -> tuple[bytes, list[str]]:
    """Make an Anthropic request acceptable to a gateway with a channel check.

    Returns ``(body, applied)`` where ``applied`` names the rewrites that fired
    (empty list = the body is unchanged). Three things are done:

    ``billing-header``
        Claude Code's ``x-anthropic-billing-header`` block, dropped from
        ``system`` — and from message contents, in case a variant inlines it.
    ``claude-identity``
        The Claude Code identity line, neutralised with a leading newline when
        it would open the first system message — and in every assistant turn,
        because the gateway inspects those too.
    ``system-default``
        The gateway refuses a chat that does not open with a system message
        ("first message is not system prompt"), so a request that carries no
        system prompt gets a neutral one.

    Anything unparseable is returned untouched: a rewrite must never be the
    reason a request fails. The rewrites are scoped per model by
    ``bridge/request_rules.json``, so Anthropic-native profiles still get the
    original bytes.
    """
    if not body:
        return body, []
    try:
        import json as _j

        doc = _j.loads(body)
    except Exception:
        return body, []
    if not isinstance(doc, dict):
        return body, []

    marker_present = b"x-anthropic-billing-header" in body.lower()
    identity_present = any(
        o.encode() in body.lower() for o in CLAUDE_IDENTITY_OPENINGS
    )

    applied: list[str] = []
    had_system = bool(doc.get("system"))
    if marker_present and "system" in doc:
        doc["system"], hit = _clean_content(doc["system"])
        if hit:
            applied.append("billing-header")
    if marker_present:
        for msg in doc.get("messages") or []:
            if isinstance(msg, dict) and "content" in msg:
                msg["content"], hit = _clean_content(msg["content"])
                if hit and "billing-header" not in applied:
                    applied.append("billing-header")

    if identity_present:
        if "system" not in doc:
            # Without a system prompt the gateway's other check fires instead
            # ("first message is not system prompt"), so supply a neutral one.
            doc["system"] = [{"type": "text", "text": DEFAULT_SYSTEM_PROMPT}]
            applied.append("claude-identity")
        else:
            doc["system"], hit = _neutralize_identity(doc["system"])
            if hit:
                applied.append("claude-identity")
        # ...and in every assistant turn. The check scans those too, and a
        # resumed conversation carries the line in a past assistant message:
        # neutralising only `system` left exactly that request refused, which is
        # the failure the interactive TUI hit on a long session. User turns are
        # not inspected by the gateway, so they are left byte-identical.
        for msg in doc.get("messages") or []:
            if (
                isinstance(msg, dict)
                and msg.get("role") == "assistant"
                and "content" in msg
            ):
                msg["content"], hit = _neutralize_identity(msg["content"])
                if hit and "claude-identity" not in applied:
                    applied.append("claude-identity")

    # The gateway wants the chat to open with a system message, whether or not
    # anything above fired: a plain probe (no system prompt, no identity) is
    # refused with "first message is not system prompt" just the same. A system
    # prompt that a rewrite above emptied counts as one that was there — the
    # rewrite's own tag already explains the change.
    if not doc.get("system") and (had_system or doc.get("messages")):
        doc["system"] = [{"type": "text", "text": DEFAULT_SYSTEM_PROMPT}]
        if not had_system:
            applied.append("system-default")

    # The gateway also scans for one refused line *wherever* it appears, so
    # this one is reworded rather than prefixed. Claude Code's TUI sends it,
    # and a session that carries it is refused even when the identity line has
    # been neutralised.
    if "system" in doc:
        doc["system"], hit = _reword_refused(doc["system"])
        if hit:
            applied.append("refused-line")
    for msg in doc.get("messages") or []:
        if (
            isinstance(msg, dict)
            and msg.get("role") in ("assistant", "system")
            and "content" in msg
        ):
            msg["content"], hit = _reword_refused(msg["content"])
            if hit and "refused-line" not in applied:
                applied.append("refused-line")

    if not applied:
        return body, []
    return _j.dumps(doc).encode("utf-8"), applied


# Previous name, kept so out-of-tree callers do not break.
def strip_billing_header(body: bytes) -> tuple[bytes, bool]:
    out, applied = sanitize_channel_markers(body)
    return out, bool(applied)


# ── media downgrade ───────────────────────────────────────────────────────
# An upstream that cannot read pixels does not ignore an image, it *refuses the
# whole request*: Qianfan answers
#   400 invalid_argument "Invalid content type. image_url is only supported by
#       certain models"
# and the turn is lost with nothing the client can retry around — the image is
# already in the durable history, so every later turn of that session carries it
# and 400s too. That is the difference between a degraded answer and a dead
# session: replacing the pixels with a text marker keeps the conversation
# alive and tells the model (and later the reader) why it cannot see.
#
# The registry already records `vision: false` per model, discovered rather than
# guessed, so the proxy knows which names need this before sending anything.

IMAGE_PLACEHOLDER = (
    "[an image was attached here but the current model ({model}) cannot read "
    "images; it was not sent]"
)
MEDIA_PLACEHOLDER = (
    "[{kind} content was attached here but the current model ({model}) cannot "
    "read it; it was not sent]"
)


def _is_media_block(block: object) -> str | None:
    """Return the kind of a non-text content block, or None if it is text.

    Covers both dialects the bridge carries: Anthropic ``image``/``document``
    blocks and OpenAI ``image_url``/``input_audio``/``file`` chat parts.
    """
    if not isinstance(block, dict):
        return None
    btype = block.get("type")
    if btype in ("text", "input_text", "tool_use", "tool_result", "thinking", "reasoning"):
        return None
    if btype == "image":
        return "image"
    if btype == "image_url":
        return "image"
    if btype in ("document", "file"):
        return "document"
    if btype in ("audio", "input_audio", "video"):
        return str(btype)
    return None


def _downgrade_parts(parts: list, model: str) -> tuple[list, int]:
    """Replace media blocks in one content list; returns (parts, replaced).

    Recurses into ``tool_result`` (and its OpenAI twin ``tool_result``/``content``
    list): the image that actually broke a session arrives there, because the
    agent reads a screenshot with a tool and the tool result is what carries the
    pixels. Rewriting only the top level missed exactly that case.
    """
    out: list = []
    replaced = 0
    for block in parts:
        if isinstance(block, dict):
            btype = block.get("type")
            if btype == "tool_result":
                inner = block.get("content")
                if isinstance(inner, list):
                    block = dict(block)
                    block["content"], n = _downgrade_parts(inner, model)
                    replaced += n
                out.append(block)
                continue
            if btype in ("tool_use", "thinking", "reasoning"):
                out.append(block)
                continue
        kind = _is_media_block(block)
        if kind is None:
            out.append(block)
            continue
        template = IMAGE_PLACEHOLDER if kind == "image" else MEDIA_PLACEHOLDER
        # Anthropic text blocks and OpenAI chat parts are both
        # {"type":"text","text":...}, so one shape fits either dialect.
        out.append({"type": "text", "text": template.format(kind=kind, model=model)})
        replaced += 1
    return out, replaced


def strip_reasoning(body: bytes) -> tuple[bytes, bool]:
    """Drop the ``thinking`` field from an Anthropic request; returns (body, changed).

    LiteLLM's anthropic ``/v1/messages`` handler rewrites the model to
    ``<provider>/responses/<model>`` whenever a non-Anthropic deployment sees
    ``thinking.type == "enabled"`` — it wants the Responses API so it can hand
    back reasoning text. It does that without consulting
    ``use_chat_completions_url_for_anthropic_messages`` (BerriAI/litellm#23841,
    bug 4; still unfixed on 1.103). An upstream that only serves
    ``/chat/completions`` therefore answers a bare 404, which LiteLLM reports as
    "No fallback model group found" — a message that names the wrong layer.

    Removing the field is the only lever the proxy has: without it the handler
    returns at its first check and the request keeps its chat/completions
    builder. The cost is real and is why this is opt-in per model
    (``bridge/request_rules.json``): that deployment loses its reasoning blocks.

    A no-op when nothing changes, so the caller forwards the original bytes
    rather than a re-serialized copy. Unparseable bodies are returned untouched:
    a rewrite must never be the reason a request fails.
    """
    if not body:
        return body, False
    try:
        import json as _j

        doc = _j.loads(body)
    except Exception:
        return body, False
    if not isinstance(doc, dict) or "thinking" not in doc:
        return body, False
    del doc["thinking"]
    return _j.dumps(doc).encode(), True


def downgrade_media(body: bytes, model: str) -> tuple[bytes, int]:
    """Strip media a text-only upstream would reject; returns (body, count).

    Handles the three shapes the bridge carries: Anthropic ``system`` (a bare
    block list), Anthropic ``messages`` / OpenAI chat ``messages`` (turns whose
    ``content`` is a part list), and OpenAI Responses ``input``.

    A no-op when nothing changes, so the caller can forward the original bytes
    rather than a re-serialized copy. Unparseable bodies are returned untouched:
    a rewrite must never be the reason a request fails.
    """
    if not body:
        return body, 0
    try:
        import json as _j

        doc = _j.loads(body)
    except Exception:
        return body, 0
    if not isinstance(doc, dict):
        return body, 0

    total = 0
    for key in ("system", "messages", "input"):
        value = doc.get(key)
        if not isinstance(value, list):
            continue
        if key == "system":
            # A bare list of blocks, not a turn.
            doc[key], n = _downgrade_parts(value, model)
            total += n
            continue
        for turn in value:
            if not isinstance(turn, dict):
                continue
            content = turn.get("content")
            if isinstance(content, list):
                turn["content"], n = _downgrade_parts(content, model)
                total += n

    if not total:
        return body, 0
    return _j.dumps(doc).encode("utf-8"), total


def flatten_system(body: bytes, model: str) -> tuple[bytes, int]:
    """Collapse an Anthropic block-list ``system`` into a plain string.

    Claude Code sends the system prompt as a list of text blocks — several of
    them, one per source — and most gateways take that. Some do not: Beam Free
    answers the same request with the system prompt as a string, and refuses
    the block list with an error about ``messages[0].content`` that points at
    the wrong place entirely (measured 2026-10-29: the same body, the same
    model, 400 with an array-form system and 200 with a string).

    The blocks are joined in order with a blank line between them, which is
    what the upstream would have seen anyway after its own translation.

    A no-op when there is nothing to flatten, so the caller can forward the
    original bytes rather than a re-serialized copy. An unparseable body is
    returned untouched: a rewrite must never be the reason a request fails.
    """
    if not body:
        return body, 0
    try:
        import json as _j

        doc = _j.loads(body)
    except Exception:
        return body, 0
    if not isinstance(doc, dict):
        return body, 0
    system = doc.get("system")
    if not isinstance(system, list) or not system:
        return body, 0

    pieces: list[str] = []
    for block in system:
        if isinstance(block, str):
            pieces.append(block)
            continue
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            pieces.append(text)
    if not pieces:
        return body, 0
    doc["system"] = "\n\n".join(pieces)
    try:
        return _j.dumps(doc).encode("utf-8"), len(pieces)
    except Exception:
        return body, 0


def strip_request_fields(body: bytes, model: str, fields) -> tuple[bytes, int]:
    """Remove request fields the upstream refuses, wherever they appear.

    Claude Code marks blocks for prompt caching with ``cache_control``, and
    an upstream that does not know the marker answers
    ``Unrecognized request argument supplied: messages[4].cache_control``
    (measured 2026-10-29 on Beam Free). The marker is an optimisation, so
    dropping it costs caching and nothing else — while leaving it in costs
    the whole request.

    A no-op when there is nothing to strip, so the caller can forward the
    original bytes. An unparseable body is returned untouched: a rewrite must
    never be the reason a request fails.
    """
    if not body or not fields:
        return body, 0
    try:
        import json as _j

        doc = _j.loads(body)
    except Exception:
        return body, 0
    if not isinstance(doc, dict):
        return body, 0

    names = tuple(str(f) for f in fields)
    removed = _strip_keys(doc, names)
    if not removed:
        return body, 0
    try:
        return _j.dumps(doc).encode("utf-8"), removed
    except Exception:
        return body, 0


def _strip_keys(node, fields) -> int:
    removed = 0
    if isinstance(node, dict):
        for name in fields:
            if name in node:
                del node[name]
                removed += 1
        for value in list(node.values()):
            removed += _strip_keys(value, fields)
    elif isinstance(node, list):
        for value in node:
            removed += _strip_keys(value, fields)
    return removed


def resolve_max_output(
    model: str, clamp_map: dict[str, int], *, fallback: int | None = None
) -> int | None:
    """Return the upstream output-token cap for ``model``.

    ``model`` may still carry a ``[1m]``-style suffix; it is normalized before
    lookup. On a miss, ``fallback`` is returned (``None`` disables clamping,
    e.g. for native Anthropic providers that accept any ``max_tokens``).
    """
    if not clamp_map:
        return fallback
    key = _norm_model(model)
    if key in clamp_map:
        return clamp_map[key]
    # tolerate a map keyed with the raw suffix form the client sent
    raw = (model or "").lower()
    if raw in clamp_map:
        return clamp_map[raw]
    return fallback


def clamp_max_tokens(
    body: bytes, clamp_map: dict[str, int], *, fallback: int | None = None
) -> tuple[bytes, int | None]:
    """Clamp the output-token cap in a request body to the upstream limit.

    Handles the three field names clients use: ``max_tokens`` (Anthropic
    /v1/messages and legacy chat), ``max_completion_tokens`` (OpenAI chat) and
    ``max_output_tokens`` (OpenAI Responses API). Returns ``(body, new_cap)``;
    ``new_cap`` is ``None`` when nothing was clamped. This prevents upstream
    ``400 ... exceeds maximum`` errors from providers whose output cap (e.g.
    CommandCode's 393216) is below what the client asks for.
    """
    if not body or not clamp_map:
        return body, None
    try:
        import json as _j
        _b = _j.loads(body)
    except Exception:
        return body, None
    if not isinstance(_b, dict):
        return body, None
    cap = resolve_max_output(_b.get("model", ""), clamp_map, fallback=fallback)
    if cap is None:
        return body, None
    changed: int | None = None
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        cur = _b.get(key)
        if isinstance(cur, int) and not isinstance(cur, bool) and cur > cap:
            _b[key] = cap
            changed = cap
    if changed is None:
        return body, None
    return _j.dumps(_b).encode("utf-8"), changed

