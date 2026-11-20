#!/usr/bin/env python3
"""`ccp media` — MiniMax multimodal endpoints: speech, music and images.

The M Plan subscription key is the same one the MiniMax text profile uses, but
these endpoints do not travel through LiteLLM: they are MiniMax's own HTTP
APIs. Keeping the paths separate means `ccp speech` / `ccp music` / `ccp image`
still work when the bridge is stopped, and they never pretend a media model is
a Claude Code tier.

`ccp music` is wired but the official Music API has been closed to new users
since 2026-08-20, so a key without legacy access gets a clear 2153 error
instead of a generic failure.

The key is region-specific. A Mainland China key (`sk-cp-...`) is accepted by
`api.minimaxi.com` and `api.minimax.cn`; the global platform is
`api.minimax.io`. The default here is the CN endpoint because that is where this
machine's M Plan key lives. A global key works with:

    ccp speech tts "hello" --base-url https://api.minimax.io

Credentials are read from the environment or from the same proxy.env the bridge
loads; they are never written to the repository or printed.

Usage:
    ccp speech tts "你好，世界" [--voice ...] [--out FILE]
    ccp speech asr meeting.mp3 [--language zh] [--format srt]
    ccp music "calm solo piano" --instrumental
    ccp image "a cat on a windowsill" --aspect-ratio 16:9 --area 2K
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_BASE = "https://api.minimaxi.com"
DEFAULT_TTS_MODEL = "speech-2.8-hd"
DEFAULT_ASR_MODEL = "asr-1.0"
DEFAULT_MUSIC_MODEL = "music-3.0"
DEFAULT_IMAGE_MODEL = "MiniMax-H3-Image"
DEFAULT_VIDEO_MODEL = "MiniMax-H3-Lite"
DEFAULT_VOICE = "Chinese (Mandarin)_Lyrical_Voice"
VIDEO_MODELS = {
    "MiniMax-H3": {"resolutions": ("768P", "2K"), "min_duration": 4,
                    "default_resolution": "768P"},
    "MiniMax-H3-Highspeed": {"resolutions": ("480P", "768P"), "min_duration": 4,
                              "default_resolution": "768P"},
    "MiniMax-H3-Lite": {"resolutions": ("480P", "768P"), "min_duration": 4,
                         "default_resolution": "480P"},
    "MiniMax-H3-Max": {"resolutions": ("480P", "768P"), "min_duration": 5,
                        "default_resolution": "768P"},
}
KEY_ENV = "MINIMAX_PLAN_API_KEY"
MAX_ASR_BYTES = 50 * 1024 * 1024
SUPPORTED_AUDIO = {"wav", "aiff", "flac", "m4a", "mp3", "aac", "opus", "ogg"}

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"


class SpeechError(RuntimeError):
    """A failure that is actionable without a traceback."""


# ── credentials and endpoint ───────────────────────────────────────────────

def _proxy_env_path() -> Path:
    override = os.environ.get("CCP_PROXY_ENV", "")
    return Path(os.path.expanduser(override or "~/.config/litellm/proxy.env"))


def _token_from_file(path: Path, name: str) -> str:
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def resolve_key(explicit_env: str | None = None, env_file: Path | None = None) -> tuple[str, str]:
    """Return (env_name, token). The M Plan key is the default on purpose.

    Falling back to the pay-as-you-go key silently would move the charge from
    the subscription to the account balance; a missing plan key must say so.
    """
    name = explicit_env or KEY_ENV
    token = (os.environ.get(name) or "").strip()
    if not token:
        token = _token_from_file(env_file or _proxy_env_path(), name)
    if not token:
        hint = ""
        if name == KEY_ENV:
            hint = ("\n  The M Plan key lives in ~/.config/litellm/proxy.env as "
                    "MINIMAX_PLAN_API_KEY.\n  For the pay-as-you-go key, pass "
                    "--key-env MINIMAX_API_KEY explicitly.")
        raise SpeechError(f"{name} is not set.{hint}")
    return name, token


def resolve_base_url(explicit: str | None) -> str:
    base = (explicit or os.environ.get("MINIMAX_SPEECH_BASE_URL") or DEFAULT_BASE).rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise SpeechError(f"base URL must start with http:// or https://: {base}")
    return base


# ── HTTP ───────────────────────────────────────────────────────────────────

def _error_hint(code: int | None, http_status: int | None) -> str:
    if code == 2049 or http_status == 401:
        return ("the key is not valid for this endpoint; M Plan keys are "
                "region-specific (CN: api.minimaxi.com / api.minimax.cn, "
                "global: api.minimax.io)")
    if code == 2153 or http_status == 410:
        return ("the official Music API is closed to new users since "
                "2026-08-20; use MiniMax Audio, an existing paying key, or a "
                "third-party host of MiniMax Music 3")
    if code in (1002, 1039, 2056) or http_status == 429:
        return ("rate limit or the current M Plan 5-hour/weekly window is "
                "spent; text, speech, image and video share the same quota")
    if code == 2068:
        return ("this model is not included in the current M Plan tier "
                "(Highspeed image needs Explore or Build)")
    if code in (1008, 2053, 2067) or http_status == 402:
        return ("M Plan quota/credits are exhausted; check the usage page at "
                "platform.minimaxi.com/console/usage")
    if code in (1026, 1027) or http_status == 422:
        return "the prompt or generated media failed MiniMax's content safety check"
    if code in (1004, 1001) or http_status in (401, 403):
        return "authentication failed; check the key and its region"
    if code == 2013 or http_status == 400:
        return ("the request was rejected as invalid; check the prompt, size, "
                "lyrics or audio format and the model id")
    if http_status and http_status >= 500:
        return "MiniMax returned a server error; retry later"
    return ""


def _detail_from_body(body: bytes) -> tuple[int | None, str]:
    text = body[:2000].decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None, text or "(empty response)"
    base = data.get("base_resp") or {}
    if isinstance(base, dict) and base.get("status_code") is not None:
        return int(base.get("status_code")), str(base.get("status_msg") or "")
    err = data.get("error")
    if isinstance(err, dict):
        return None, str(err.get("message") or err.get("type") or "")
    return None, str(data.get("message") or text)[:500]


def _raise_http_error(what: str, status: int | None, body: bytes) -> None:
    code, message = _detail_from_body(body)
    hint = _error_hint(code, status)
    suffix = f" Hint: {hint}." if hint else ""
    raise SpeechError(
        f"{what} failed (HTTP {status or '?'}, code {code if code is not None else '?'}): "
        f"{message or 'no detail'}.{suffix}")


def _request(url: str, key: str, body: bytes, content_type: str,
             headers: dict[str, str] | None = None, timeout: int = 180) -> bytes:
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", content_type)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        _raise_http_error("MiniMax", e.code, e.read())
    except urllib.error.URLError as e:
        raise SpeechError(f"cannot reach {url}: {e.reason}") from e
    except TimeoutError as e:
        raise SpeechError(f"request to {url} timed out after {timeout}s") from e
    raise AssertionError("unreachable")


def _parse_json(body: bytes, what: str) -> dict:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError as e:
        raise SpeechError(f"{what} returned a non-JSON body: {body[:300]!r}") from e
    if not isinstance(data, dict):
        raise SpeechError(f"{what} returned {type(data).__name__}, expected an object")
    return data


def _get_json(url: str, key: str, what: str = "MiniMax",
              timeout: int = 120) -> dict:
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _parse_json(resp.read(), what)
    except urllib.error.HTTPError as e:
        _raise_http_error(what, e.code, e.read())
    except urllib.error.URLError as e:
        raise SpeechError(f"cannot reach {url}: {e.reason}") from e
    except TimeoutError as e:
        raise SpeechError(f"request to {url} timed out after {timeout}s") from e
    raise AssertionError("unreachable")


def _raise_base_resp(data: dict, what: str) -> None:
    """MiniMax reports some failures as HTTP 200 with base_resp.status_code != 0."""
    base = data.get("base_resp")
    if not isinstance(base, dict):
        return
    code = base.get("status_code")
    if code in (0, None):
        return
    try:
        code = int(code)
    except (TypeError, ValueError):
        pass
    hint = _error_hint(code if isinstance(code, int) else None, None)
    suffix = f" Hint: {hint}." if hint else ""
    raise SpeechError(
        f"{what} failed (code {code}): {base.get('status_msg') or 'no detail'}.{suffix}")


# ── text to speech ─────────────────────────────────────────────────────────

def build_tts_payload(text: str, *, model: str, voice: str, audio_format: str,
                      sample_rate: int, bitrate: int, speed: float, vol: float,
                      pitch: int, emotion: str | None,
                      language_boost: str | None) -> dict:
    voice_setting: dict = {
        "voice_id": voice,
        "speed": speed,
        "vol": vol,
        "pitch": pitch,
    }
    if emotion:
        voice_setting["emotion"] = emotion
    payload: dict = {
        "model": model,
        "text": text,
        # `url` keeps a multi-megabyte MP3 out of the JSON response; the audio
        # is downloaded immediately and the signed URL is never persisted.
        "output_format": "url",
        "stream": False,
        "voice_setting": voice_setting,
        "audio_setting": {
            "format": audio_format,
            "sample_rate": sample_rate,
            "bitrate": bitrate,
            "channel": 1,
        },
    }
    if language_boost:
        payload["language_boost"] = language_boost
    return payload


def _default_out(audio_format: str, prefix: str = "speech") -> Path:
    return Path(time.strftime(f"{prefix}-%Y%m%d-%H%M%S") + "." + audio_format)


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


def _write_audio(out: str | None, audio: bytes, audio_format: str,
                 prefix: str = "speech") -> Path | None:
    if out == "-":
        sys.stdout.buffer.write(audio)
        sys.stdout.buffer.flush()
        return None
    path = Path(os.path.expanduser(out)) if out else _default_out(audio_format, prefix)
    if path.is_dir():
        path = path / _default_out(audio_format, prefix)
    _write_bytes(path, audio)
    return path


def cmd_tts(args) -> int:
    text = args.text
    if text is None or text == "-":
        text = sys.stdin.read()
    text = text.strip()
    if not text:
        raise SpeechError("no text to synthesize (pass TEXT or pipe it on stdin)")
    if len(text) > 10_000:
        raise SpeechError(
            f"text is {len(text)} characters; synchronous TTS accepts at most "
            "10,000 (use the async long-TTS API for longer text)")

    key_name, key = resolve_key(args.key_env)
    base = resolve_base_url(args.base_url)
    payload = build_tts_payload(
        text,
        model=args.model,
        voice=args.voice,
        audio_format=args.format,
        sample_rate=args.sample_rate,
        bitrate=args.bitrate,
        speed=args.speed,
        vol=args.vol,
        pitch=args.pitch,
        emotion=args.emotion,
        language_boost=args.language_boost,
    )
    body = _request(base + "/v1/t2a_v2", key, json.dumps(payload).encode(),
                    "application/json")
    data = _parse_json(body, "text-to-speech")
    _raise_base_resp(data, "text-to-speech")

    audio_ref = (data.get("data") or {}).get("audio")
    if not audio_ref:
        raise SpeechError("text-to-speech returned no audio")
    if str(audio_ref).startswith(("http://", "https://")):
        try:
            with urllib.request.urlopen(audio_ref, timeout=120) as resp:
                audio = resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            raise SpeechError(f"cannot download the synthesized audio: {e}") from e
    else:
        try:
            audio = bytes.fromhex(str(audio_ref))
        except ValueError as e:
            raise SpeechError("text-to-speech returned neither a URL nor hex audio") from e
    if not audio:
        raise SpeechError("text-to-speech returned an empty audio file")

    path = _write_audio(args.out, audio, args.format)
    info = data.get("extra_info") or {}
    duration_ms = info.get("audio_length")
    result = {
        "path": str(path) if path else "-",
        "model": args.model,
        "voice": args.voice,
        "format": args.format,
        "bytes": len(audio),
        "duration_ms": duration_ms,
        "usage_characters": info.get("usage_characters"),
        "trace_id": data.get("trace_id"),
        "key_env": key_name,
        "base_url": base,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        where = str(path) if path else "stdout"
        dur = f"{duration_ms / 1000:.1f}s, " if isinstance(duration_ms, (int, float)) else ""
        chars = info.get("usage_characters")
        chars_text = f", {chars} chars" if isinstance(chars, int) else ""
        print(f"{GREEN}✔ {where}{RESET} {DIM}({dur}{len(audio)} bytes{chars_text}){RESET}",
              file=sys.stderr if path is None else sys.stdout)
    return 0


# ── speech to text ─────────────────────────────────────────────────────────

def build_asr_multipart(fields: dict[str, str], filename: str, content: bytes,
                        mime: str) -> tuple[str, bytes]:
    boundary = "----ccp" + uuid.uuid4().hex
    safe_name = Path(filename).name.replace('"', "_")
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    chunks.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{safe_name}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
    )
    chunks.append(content)
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return boundary, b"".join(chunks)


def cmd_asr(args) -> int:
    path = Path(os.path.expanduser(args.file))
    if not path.is_file():
        raise SpeechError(f"audio file not found: {path}")
    size = path.stat().st_size
    if size > MAX_ASR_BYTES:
        raise SpeechError(
            f"audio file is {size / 1_000_000:.1f} MB; MiniMax accepts at most "
            f"50 MB (convert to mono 16 kHz or mp3/opus)")
    ext = path.suffix.lower().lstrip(".")
    if ext not in SUPPORTED_AUDIO:
        raise SpeechError(
            f"unsupported audio extension .{ext}; use one of: "
            + ", ".join(sorted(SUPPORTED_AUDIO)))

    key_name, key = resolve_key(args.key_env)
    base = resolve_base_url(args.base_url)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    fields = {"model": args.model, "response_format": args.format}
    boundary, body = build_asr_multipart(fields, path.name, path.read_bytes(), mime)
    headers = {"language": args.language} if args.language else {}
    raw = _request(base + "/v1/speech_to_text", key, body,
                   f"multipart/form-data; boundary={boundary}", headers=headers,
                   timeout=300)

    if args.format in ("json", "verbose_json"):
        data = _parse_json(raw, "speech-to-text")
        _raise_base_resp(data, "speech-to-text")
        text = str(data.get("text") or "")
        result = {
            "text": text,
            "duration": data.get("duration"),
            "n_speakers": data.get("n_speakers"),
            "segments": data.get("segments"),
            "trace_id": data.get("trace_id"),
            "key_env": key_name,
            "base_url": base,
        }
        if args.out:
            out = Path(os.path.expanduser(args.out))
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            result["path"] = str(out)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        elif args.out:
            print(f"{GREEN}✔ {result['path']}{RESET}")
        else:
            print(text)
    else:
        text = raw.decode("utf-8", "replace")
        if args.out:
            out = Path(os.path.expanduser(args.out))
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            if args.json:
                print(json.dumps({"path": str(out), "format": args.format,
                                  "key_env": key_name, "base_url": base},
                                 ensure_ascii=False))
            else:
                print(f"{GREEN}✔ {out}{RESET}")
        else:
            sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


# ── music generation ──────────────────────────────────────────────────────

def build_music_payload(prompt: str, *, model: str, lyrics: str | None,
                        is_instrumental: bool, lyrics_optimizer: bool,
                        audio_format: str, sample_rate: int,
                        bitrate: int) -> dict:
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "output_format": "url",
        "stream": False,
        "audio_setting": {
            "format": audio_format,
            "sample_rate": sample_rate,
            "bitrate": bitrate,
        },
    }
    if is_instrumental:
        payload["is_instrumental"] = True
    elif lyrics:
        payload["lyrics"] = lyrics
    if lyrics_optimizer:
        payload["lyrics_optimizer"] = True
    return payload


def _music_lyrics(args) -> str | None:
    if args.lyrics and args.lyrics_file:
        raise SpeechError("use either --lyrics or --lyrics-file, not both")
    if args.lyrics:
        return args.lyrics
    if not args.lyrics_file:
        return None
    path = Path(os.path.expanduser(args.lyrics_file))
    if args.lyrics_file == "-":
        return sys.stdin.read()
    if not path.is_file():
        raise SpeechError(f"lyrics file not found: {path}")
    return path.read_text(encoding="utf-8")


def cmd_music(args) -> int:
    prompt = (args.prompt or "").strip()
    if not prompt:
        raise SpeechError("no music prompt (describe style, mood and scenario)")
    if len(prompt) > 2000:
        raise SpeechError(f"prompt is {len(prompt)} characters; the Music API accepts at most 2,000")

    lyrics = _music_lyrics(args)
    if lyrics is not None:
        lyrics = lyrics.strip()
    if not args.instrumental and not lyrics and not args.lyrics_optimizer:
        raise SpeechError(
            "non-instrumental music needs lyrics: pass --lyrics/--lyrics-file, "
            "or --lyrics-optimizer, or --instrumental")
    if lyrics and len(lyrics) > 3500:
        raise SpeechError(f"lyrics are {len(lyrics)} characters; the Music API accepts at most 3,500")

    key_name, key = resolve_key(args.key_env)
    base = resolve_base_url(args.base_url)
    payload = build_music_payload(
        prompt,
        model=args.model,
        lyrics=lyrics,
        is_instrumental=args.instrumental,
        lyrics_optimizer=args.lyrics_optimizer,
        audio_format=args.format,
        sample_rate=args.sample_rate,
        bitrate=args.bitrate,
    )
    body = _request(base + "/v1/music_generation", key,
                    json.dumps(payload).encode(), "application/json", timeout=600)
    data = _parse_json(body, "music generation")
    _raise_base_resp(data, "music generation")

    audio_ref = (data.get("data") or {}).get("audio")
    if not audio_ref:
        raise SpeechError("music generation returned no audio")
    if str(audio_ref).startswith(("http://", "https://")):
        try:
            with urllib.request.urlopen(audio_ref, timeout=180) as resp:
                audio = resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            raise SpeechError(f"cannot download the generated music: {e}") from e
    else:
        try:
            audio = bytes.fromhex(str(audio_ref))
        except ValueError as e:
            raise SpeechError("music generation returned neither a URL nor hex audio") from e
    if not audio:
        raise SpeechError("music generation returned an empty audio file")

    path = _write_audio(args.out, audio, args.format, prefix="music")
    info = data.get("extra_info") or {}
    duration_ms = info.get("music_duration")
    result = {
        "path": str(path) if path else "-",
        "model": args.model,
        "format": args.format,
        "bytes": len(audio),
        "duration_ms": duration_ms,
        "sample_rate": info.get("music_sample_rate"),
        "instrumental": bool(args.instrumental),
        "trace_id": data.get("trace_id"),
        "key_env": key_name,
        "base_url": base,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        where = str(path) if path else "stdout"
        dur = f"{duration_ms / 1000:.1f}s, " if isinstance(duration_ms, (int, float)) else ""
        print(f"{GREEN}✔ {where}{RESET} {DIM}({dur}{len(audio)} bytes){RESET}",
              file=sys.stderr if path is None else sys.stdout)
    return 0


# ── image generation ──────────────────────────────────────────────────────

def build_image_payload(prompt: str, *, model: str, n: int, size: str | None,
                        aspect_ratio: str | None, area: str | None,
                        output_format: str,
                        disable_prompt_enhanced: bool) -> dict:
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "n": n,
        "response_format": "url",
        "output_format": output_format,
    }
    if disable_prompt_enhanced:
        payload["disable_prompt_enhanced"] = True
    if size and size != "auto":
        payload["size"] = size
    else:
        if aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio
        if area:
            payload["area"] = area
    return payload


def _image_data_url(raw: str) -> str:
    """Encode a local image path or pass through an http(s)/data URL."""
    if raw.startswith(("http://", "https://", "data:")):
        return raw
    path = Path(os.path.expanduser(raw))
    if not path.is_file():
        raise SpeechError(f"reference image not found: {raw}")
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _image_targets(out: str | None, n: int, extension: str) -> list[Path]:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if out and out != "-":
        base = Path(os.path.expanduser(out))
        if n == 1 and base.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            return [base]
        base.mkdir(parents=True, exist_ok=True)
    elif out == "-":
        raise SpeechError("--out - is not supported for images; pass a directory or a file")
    else:
        base = Path(".")
    return [base / f"image-{stamp}-{i}.{extension}" for i in range(n)]


def cmd_image(args) -> int:
    prompt = (args.prompt or "").strip()
    if not prompt:
        raise SpeechError("no image prompt")
    if len(prompt) > 32000:
        raise SpeechError(f"prompt is {len(prompt)} characters; the Image API accepts at most 32,000")
    if not 1 <= args.n <= 10:
        raise SpeechError("--n must be between 1 and 10")

    size = (args.size or "auto").strip().lower()
    fixed_size = size != "auto"
    if fixed_size:
        if args.aspect_ratio or args.area:
            raise SpeechError("--size WIDTHxHEIGHT cannot be combined with --aspect-ratio or --area")
        if "x" not in size:
            raise SpeechError("--size must be WIDTHxHEIGHT, e.g. 2720x1536, or auto")
    if args.disable_prompt_enhanced and not fixed_size and not args.aspect_ratio:
        raise SpeechError(
            "--no-enhance requires --size WIDTHxHEIGHT or --aspect-ratio X:Y")

    key_name, key = resolve_key(args.key_env)
    base = resolve_base_url(args.base_url)
    reference_images = [p for p in (getattr(args, "image", None) or []) if p]
    if len(reference_images) > 16:
        raise SpeechError("--image accepts at most 16 reference images")
    if reference_images:
        # Reference-conditioned editing: H3 ignores `subject_reference` on
        # /v1/images/generations, so references must go through /v1/images/edits.
        entries = []
        for raw in reference_images:
            entries.append({"image_url": _image_data_url(raw)})
        payload = {
            "model": args.model,
            "prompt": prompt,
            "images": entries,
            "n": args.n,
            "response_format": "url",
            "output_format": args.format,
        }
        if args.disable_prompt_enhanced:
            payload["disable_prompt_enhanced"] = True
        if size and size != "auto":
            payload["size"] = size
        else:
            if args.aspect_ratio:
                payload["aspect_ratio"] = args.aspect_ratio
            if args.area:
                payload["area"] = args.area
        endpoint = "/v1/images/edits"
    else:
        payload = build_image_payload(
            prompt,
            model=args.model,
            n=args.n,
            size=size,
            aspect_ratio=args.aspect_ratio,
            area=args.area,
            output_format=args.format,
            disable_prompt_enhanced=args.disable_prompt_enhanced,
        )
        endpoint = "/v1/images/generations"
    body = _request(base + endpoint, key,
                    json.dumps(payload).encode(), "application/json", timeout=600)
    data = _parse_json(body, "image generation")
    _raise_base_resp(data, "image generation")

    items = data.get("data") or []
    if not items:
        raise SpeechError("image generation returned no images")
    extension = args.format
    targets = _image_targets(args.out, len(items), extension)
    results: list[dict] = []
    succeeded = 0
    for i, item in enumerate(items):
        if item.get("error"):
            err = item["error"]
            results.append({"index": i, "error": err.get("message") or "failed",
                            "code": err.get("code")})
            continue
        url = item.get("url")
        if not url:
            results.append({"index": i, "error": "no URL in the response"})
            continue
        try:
            with urllib.request.urlopen(url, timeout=180) as resp:
                image = resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            results.append({"index": i, "error": f"download failed: {e}"})
            continue
        path = targets[i]
        _write_bytes(path, image)
        succeeded += 1
        results.append({
            "index": i,
            "path": str(path),
            "size": item.get("size"),
            "bytes": len(image),
            "revised_prompt": item.get("revised_prompt"),
        })

    usage = data.get("usage") or {}
    result = {
        "model": args.model,
        "images": results,
        "generated": succeeded,
        "usage": usage,
        "request_id": data.get("request_id"),
        "key_env": key_name,
        "base_url": base,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        for item in results:
            if item.get("error"):
                print(f"{RED}! image {item['index']}: {item['error']}"
                      + (f" ({item['code']})" if item.get("code") else "")
                      + RESET, file=sys.stderr)
            else:
                size = item.get("size") or ""
                print(f"{GREEN}✔ {item['path']}{RESET} "
                      f"{DIM}({size}, {item.get('bytes', 0)} bytes){RESET}")
        if succeeded:
            print(f"{DIM}{succeeded}/{len(items)} image(s), "
                  f"{usage.get('total_tokens', 0)} tokens{RESET}")
    if succeeded == 0:
        raise SpeechError("all images failed; see the per-image errors above")
    return 0


# ── video generation ──────────────────────────────────────────────────────

def _image_ref(value: str, label: str) -> str:
    if value.startswith(("http://", "https://", "data:")):
        return value
    path = Path(os.path.expanduser(value))
    if not path.is_file():
        raise SpeechError(f"{label} image not found: {path}")
    size = path.stat().st_size
    if size > 30 * 1024 * 1024:
        raise SpeechError(f"{label} image is {size / 1_000_000:.1f} MB; "
                          "the Video API accepts at most 30 MB")
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{encoded}"


def build_video_payload(prompt: str, *, model: str, resolution: str,
                        duration: int, ratio: str, generate_audio: bool,
                        first_frame: str | None = None,
                        last_frame: str | None = None) -> dict:
    content: list[dict] = [{"type": "text", "text": prompt}]
    if first_frame:
        content.append({"type": "image_url",
                        "image_url": {"url": _image_ref(first_frame, "first-frame")},
                        "role": "first_frame"})
    if last_frame:
        content.append({"type": "image_url",
                        "image_url": {"url": _image_ref(last_frame, "last-frame")},
                        "role": "last_frame"})
    return {
        "model": model,
        "content": content,
        "resolution": resolution,
        "duration": duration,
        "ratio": ratio,
        "generate_audio": generate_audio,
    }


def _video_target(out: str | None) -> Path:
    stamp = time.strftime("video-%Y%m%d-%H%M%S")
    if out == "-":
        raise SpeechError("--out - is not supported for video; pass a directory or a file")
    if out:
        base = Path(os.path.expanduser(out))
        if base.suffix.lower() in (".mp4", ".mov"):
            return base
        base.mkdir(parents=True, exist_ok=True)
    else:
        base = Path(".")
    return base / (stamp + ".mp4")


def _poll_video(base: str, key: str, task_id: str, timeout: int,
                interval: int) -> dict:
    deadline = time.time() + timeout
    last_status: str | None = None
    while time.time() < deadline:
        data = _get_json(f"{base}/v2/query/video_generation/{task_id}", key,
                         "video query")
        task = data.get("task") or data
        status = str(task.get("status") or "")
        if status != last_status:
            print(f"{DIM}video {task_id}: {status or 'unknown'}{RESET}",
                  file=sys.stderr)
            last_status = status
        if status == "succeeded":
            return task
        if status in ("failed", "cancelled"):
            err = task.get("error") or {}
            raise SpeechError(
                f"video generation {status}"
                + (f" ({err.get('code')}): {err.get('message')}" if err else ""))
        time.sleep(interval)
    raise SpeechError(
        f"video task {task_id} did not finish within {timeout}s; "
        f"query it later with `ccp video --task {task_id}`")


def cmd_video(args) -> int:
    if args.task and args.prompt:
        raise SpeechError("use either a prompt or --task, not both")
    if not args.task and not (args.prompt or "").strip():
        raise SpeechError("no video prompt (or pass --task to poll an existing task)")

    key_name, key = resolve_key(args.key_env)
    base = resolve_base_url(args.base_url)
    requested_duration = args.duration
    task: dict
    task_id = args.task

    if not task_id:
        prompt = args.prompt.strip()
        if len(prompt) > 7000:
            raise SpeechError(f"prompt is {len(prompt)} characters; "
                              "the Video API accepts at most 7,000")
        spec = VIDEO_MODELS.get(args.model)
        if not spec:
            raise SpeechError(f"unknown video model {args.model!r}; "
                              f"choose one of: {', '.join(VIDEO_MODELS)}")
        resolution = (args.resolution or spec["default_resolution"]).upper()
        if resolution not in spec["resolutions"]:
            raise SpeechError(
                f"{args.model} supports {', '.join(spec['resolutions'])}; "
                f"{resolution} is not available")
        duration = requested_duration or spec["min_duration"]
        if duration < spec["min_duration"] or duration > 15:
            raise SpeechError(
                f"{args.model} duration must be {spec['min_duration']}-15 seconds")
        if args.last_frame and not args.image:
            raise SpeechError("--last-frame requires --image (the first frame)")
        if not args.image and args.ratio == "adaptive":
            raise SpeechError("text-to-video needs a concrete --ratio, not adaptive")

        payload = build_video_payload(
            prompt,
            model=args.model,
            resolution=resolution,
            duration=duration,
            ratio=args.ratio,
            generate_audio=not args.silent,
            first_frame=args.image,
            last_frame=args.last_frame,
        )
        body = _request(base + "/v2/video_generation", key,
                        json.dumps(payload).encode(), "application/json",
                        timeout=120)
        data = _parse_json(body, "video generation")
        _raise_base_resp(data, "video generation")
        task_id = data.get("task_id") or (data.get("task") or {}).get("id")
        if not task_id:
            raise SpeechError("video generation returned no task_id")
        if args.no_wait:
            if args.json:
                print(json.dumps({"task_id": task_id, "status": "queued",
                                  "model": args.model, "key_env": key_name,
                                  "base_url": base}, ensure_ascii=False))
            else:
                print(task_id)
            return 0

    task = _poll_video(base, key, str(task_id), args.timeout, args.poll)
    content = task.get("content") or {}
    url = content.get("url")
    if not url:
        raise SpeechError("video task succeeded but returned no download URL")
    try:
        with urllib.request.urlopen(url, timeout=300) as resp:
            video = resp.read()
    except (urllib.error.URLError, TimeoutError) as e:
        raise SpeechError(f"cannot download the generated video: {e}") from e
    if not video:
        raise SpeechError("video download returned an empty file")

    path = _video_target(args.out)
    _write_bytes(path, video)
    result = {
        "task_id": str(task_id),
        "path": str(path),
        "status": task.get("status"),
        "model": task.get("model") or args.model,
        "resolution": task.get("resolution"),
        "duration": task.get("duration"),
        "ratio": task.get("ratio"),
        "bytes": len(video),
        "usage": task.get("usage"),
        "key_env": key_name,
        "base_url": base,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        size = f", {result['resolution']}" if result.get("resolution") else ""
        dur = f", {result['duration']}s" if result.get("duration") else ""
        print(f"{GREEN}✔ {path}{RESET} {DIM}({len(video)} bytes{size}{dur}){RESET}")
    return 0


# ── CLI ────────────────────────────────────────────────────────────────────

def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--key-env", default=None,
                   help=f"environment variable holding the key (default: {KEY_ENV})")
    p.add_argument("--base-url", default=None,
                   help=f"MiniMax API base (default: {DEFAULT_BASE}; "
                        "global keys use https://api.minimax.io)")
    p.add_argument("--json", action="store_true", help="emit one JSON object")


def build_parser(prog: str = "ccp speech") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="MiniMax multimodal endpoints: speech, music and images.",
        epilog=(
            "The key is read from ~/.config/litellm/proxy.env unless it is in the\n"
            "environment. Speech shares the M Plan 5-hour and weekly quota with text.\n"
            "The official Music API is closed to new users since 2026-08-20.\n\n"
            "Examples:\n"
            '  ccp speech tts "你好，世界"\n'
            '  ccp speech tts "Hello" --voice English_Graceful_Lady --out hello.mp3\n'
            "  ccp speech asr meeting.mp3 --language zh --format verbose_json\n"
            '  ccp music "calm solo piano" --instrumental\n'
            '  ccp image "a cat on a windowsill" --aspect-ratio 16:9 --area 2K\n'
            '  ccp video "a paper boat drifting on a calm pond" --duration 4\n'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    tts = sub.add_parser("tts", help="text to speech")
    tts.add_argument("text", nargs="?", default="-",
                     help="text to speak; '-' or omitted reads stdin")
    tts.add_argument("--model", default=DEFAULT_TTS_MODEL,
                     help=f"speech model (default: {DEFAULT_TTS_MODEL})")
    tts.add_argument("--voice", default=DEFAULT_VOICE,
                     help=f"voice id (default: {DEFAULT_VOICE})")
    tts.add_argument("--out", default=None,
                     help="output file (default: speech-<timestamp>.<format>; '-' = stdout)")
    tts.add_argument("--format", choices=("mp3", "wav", "flac"), default="mp3")
    tts.add_argument("--sample-rate", type=int, default=32000)
    tts.add_argument("--bitrate", type=int, default=128000)
    tts.add_argument("--speed", type=float, default=1.0)
    tts.add_argument("--vol", type=float, default=1.0)
    tts.add_argument("--pitch", type=int, default=0)
    tts.add_argument("--emotion", default=None,
                     choices=("happy", "sad", "angry", "fearful", "disgusted",
                              "surprised", "calm", "fluent", "whisper"))
    tts.add_argument("--language-boost", default=None,
                     help="e.g. Chinese, Chinese,Yue, English, auto")
    _add_common(tts)
    tts.set_defaults(func=cmd_tts)

    asr = sub.add_parser("asr", aliases=["stt"], help="speech to text")
    asr.add_argument("file", help="audio file (wav/aiff/flac/m4a/mp3/aac/opus/ogg)")
    asr.add_argument("--model", default=DEFAULT_ASR_MODEL,
                     help=f"ASR model (default: {DEFAULT_ASR_MODEL})")
    asr.add_argument("--language", default="",
                     help="BCP-47 hint: zh, yue, en, ja, ko, ... (default: auto)")
    asr.add_argument("--format", choices=("json", "verbose_json", "srt", "vtt"),
                     default="json")
    asr.add_argument("--out", default=None,
                     help="write the raw response here instead of stdout")
    _add_common(asr)
    asr.set_defaults(func=cmd_asr)

    music = sub.add_parser(
        "music",
        prog="ccp music",
        help="text to music (official API is closed to new users since 2026-08-20)")
    music.add_argument("prompt", help="style, mood and scenario (1-2000 chars)")
    music.add_argument("--model", default=DEFAULT_MUSIC_MODEL,
                       help=f"music model (default: {DEFAULT_MUSIC_MODEL})")
    music.add_argument("--lyrics", default=None,
                       help="lyrics with [Verse]/[Chorus] structure tags")
    music.add_argument("--lyrics-file", default=None,
                       help="read lyrics from a file; '-' reads stdin")
    music.add_argument("--instrumental", action="store_true",
                       help="no vocals; prompt is required and lyrics are ignored")
    music.add_argument("--lyrics-optimizer", action="store_true",
                       help="let the API write lyrics from the prompt")
    music.add_argument("--out", default=None,
                       help="output file (default: music-<timestamp>.<format>; '-' = stdout)")
    music.add_argument("--format", choices=("mp3", "wav", "pcm"), default="mp3")
    music.add_argument("--sample-rate", type=int, default=44100)
    music.add_argument("--bitrate", type=int, default=256000)
    _add_common(music)
    music.set_defaults(func=cmd_music)

    image = sub.add_parser(
        "image",
        prog="ccp image",
        help="text to image (MiniMax-H3-Image)")
    image.add_argument("prompt", help="image description (up to 32,000 chars)")
    image.add_argument("--model", default=DEFAULT_IMAGE_MODEL,
                       help=f"image model (default: {DEFAULT_IMAGE_MODEL})")
    image.add_argument("--n", type=int, default=1, help="images per request, 1-10")
    image.add_argument("--size", default="auto",
                       help="auto or WIDTHxHEIGHT (e.g. 2720x1536)")
    image.add_argument("--aspect-ratio", default=None,
                       help="e.g. 16:9, 3:4, 21:9 (with --size auto)")
    image.add_argument("--area", choices=("1K", "2K", "4K"), default=None,
                       help="target area when --size is auto (default: server 4K)")
    image.add_argument("--format", choices=("jpeg", "png", "webp"), default="jpeg")
    image.add_argument("--out", default=None,
                       help="directory, or a .png/.jpg/.jpeg/.webp file when --n 1")
    image.add_argument("--image", dest="image", action="append", default=None,
                       metavar="PATH_OR_URL",
                       help="reference image for editing (repeat up to 16; local "
                            "path or URL; routes to /v1/images/edits)")
    image.add_argument("--no-enhance", dest="disable_prompt_enhanced",
                       action="store_true",
                       help="disable prompt enhancement (then set size or aspect ratio)")
    _add_common(image)
    image.set_defaults(func=cmd_image)

    video = sub.add_parser(
        "video",
        prog="ccp video",
        help="text/image to video (async; M Plan Explore/Build)")
    video.add_argument("prompt", nargs="?", default=None,
                       help="video description (up to 7,000 chars)")
    video.add_argument("--task", default=None,
                       help="poll and download an existing task id")
    video.add_argument("--model", default=DEFAULT_VIDEO_MODEL,
                       choices=tuple(VIDEO_MODELS),
                       help=f"video model (default: {DEFAULT_VIDEO_MODEL})")
    video.add_argument("--resolution", choices=("480P", "768P", "2K"),
                       default=None,
                       help="per-model default: Lite 480P, H3 768P")
    video.add_argument("--duration", type=int, default=None,
                       help="seconds; 4-15 (Max: 5-15)")
    video.add_argument("--ratio", default="16:9",
                       choices=("adaptive", "21:9", "16:9", "4:3",
                                "1:1", "3:4", "9:16"))
    video.add_argument("--image", default=None,
                       help="first-frame image: local path, URL or data URI")
    video.add_argument("--last-frame", default=None,
                       help="last-frame image (requires --image)")
    video.add_argument("--silent", action="store_true",
                       help="generate a silent video (default: with audio)")
    video.add_argument("--out", default=None,
                       help="directory, or a .mp4/.mov file (default: video-<timestamp>.mp4)")
    video.add_argument("--no-wait", action="store_true",
                       help="create the task and print its id instead of polling")
    video.add_argument("--timeout", type=int, default=900,
                       help="poll deadline in seconds (default: 900)")
    video.add_argument("--poll", type=int, default=10,
                       help="poll interval in seconds (default: 10)")
    _add_common(video)
    video.set_defaults(func=cmd_video)
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SpeechError as e:
        print(f"{RED}Error:{RESET} {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
