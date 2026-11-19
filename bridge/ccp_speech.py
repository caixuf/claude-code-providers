#!/usr/bin/env python3
"""`ccp speech` — MiniMax M Plan text-to-speech and speech-to-text.

The M Plan subscription key is the same one the MiniMax text profile uses, but
speech does not travel through LiteLLM: these are MiniMax's own HTTP endpoints.
Keeping the two paths separate means `ccp speech` still works when the bridge is
stopped, and it never pretends a speech model is a Claude Code tier.

`ccp music` shares the same key/error layer. Its official API has been closed to
new users since 2026-08-20, so a key without legacy music access gets a clear
2153 error instead of a generic failure.

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
"""

from __future__ import annotations

import argparse
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
DEFAULT_VOICE = "Chinese (Mandarin)_Lyrical_Voice"
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
    if code in (1002, 1039) or http_status == 429:
        return ("rate limit or the current M Plan 5-hour/weekly window is "
                "spent; speech shares the text quota")
    if code == 1008 or http_status == 402:
        return ("M Plan quota/credits are exhausted; check the usage page at "
                "platform.minimaxi.com/console/usage")
    if code in (1004, 1001) or http_status in (401, 403):
        return "authentication failed; check the key and its region"
    if code == 2013 or http_status == 400:
        return "the request was rejected as invalid; check the text/audio format and model id"
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


def _write_audio(out: str | None, audio: bytes, audio_format: str,
                 prefix: str = "speech") -> Path | None:
    if out == "-":
        sys.stdout.buffer.write(audio)
        sys.stdout.buffer.flush()
        return None
    path = Path(os.path.expanduser(out)) if out else _default_out(audio_format, prefix)
    if path.is_dir():
        path = path / _default_out(audio_format, prefix)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(audio)
    tmp.replace(path)
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
        description="MiniMax speech and music endpoints.",
        epilog=(
            "The key is read from ~/.config/litellm/proxy.env unless it is in the\n"
            "environment. Speech shares the M Plan 5-hour and weekly quota with text.\n"
            "The official Music API is closed to new users since 2026-08-20.\n\n"
            "Examples:\n"
            '  ccp speech tts "你好，世界"\n'
            '  ccp speech tts "Hello" --voice English_Graceful_Lady --out hello.mp3\n'
            "  ccp speech asr meeting.mp3 --language zh --format verbose_json\n"
            '  ccp music "calm solo piano" --instrumental\n'
            '  ccp music "indie folk" --lyrics-file lyrics.txt --out song.mp3\n'
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
    return parser


def main(argv: list[str]) -> int:
    prog = "ccp music" if argv and argv[0] == "music" else "ccp speech"
    parser = build_parser(prog)
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
