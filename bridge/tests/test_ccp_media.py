"""Guards for the MiniMax multimodal helper (bridge/ccp_media.py).

The expensive mistakes here are silent:

* falling back to the pay-as-you-go key moves the charge off the M Plan;
* MiniMax answers some failures as HTTP 200 with ``base_resp.status_code``,
  which looks like success unless it is checked;
* a CN M Plan key sent to the global endpoint returns "invalid api key", not a
  region error, so the hint is the only actionable part;
* the ASR file must travel as multipart, not JSON;
* ``--size WIDTHxHEIGHT`` combined with ``--aspect-ratio``/``--area`` is a 400,
  and a partial image failure must not hide the successful images.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

import ccp_media as s  # noqa: E402


# ── key resolution ────────────────────────────────────────────────────────

def test_resolve_key_reads_the_plan_key_from_proxy_env(tmp_path, monkeypatch):
    env_file = tmp_path / "proxy.env"
    env_file.write_text(
        "MINIMAX_PLAN_API_KEY=sk-cp-plan\n"
        "MINIMAX_API_KEY=sk-api-payg\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MINIMAX_PLAN_API_KEY", raising=False)
    name, token = s.resolve_key(env_file=env_file)
    assert name == "MINIMAX_PLAN_API_KEY"
    assert token == "sk-cp-plan"


def test_resolve_key_does_not_silently_use_the_payg_key(tmp_path, monkeypatch):
    """A missing plan key must fail, not bill the account balance."""
    env_file = tmp_path / "proxy.env"
    env_file.write_text("MINIMAX_API_KEY=sk-api-payg\n", encoding="utf-8")
    monkeypatch.delenv("MINIMAX_PLAN_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    with pytest.raises(s.SpeechError) as e:
        s.resolve_key(env_file=env_file)
    assert "MINIMAX_PLAN_API_KEY" in str(e.value)
    assert "MINIMAX_API_KEY" in str(e.value)


def test_resolve_key_allows_an_explicit_payg_key(tmp_path, monkeypatch):
    env_file = tmp_path / "proxy.env"
    env_file.write_text("MINIMAX_API_KEY=sk-api-payg\n", encoding="utf-8")
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    name, token = s.resolve_key("MINIMAX_API_KEY", env_file=env_file)
    assert name == "MINIMAX_API_KEY"
    assert token == "sk-api-payg"


# ── request shapes ─────────────────────────────────────────────────────────

def test_tts_payload_is_url_based_and_carries_voice_settings():
    payload = s.build_tts_payload(
        "你好", model="speech-2.8-hd", voice="Chinese (Mandarin)_Lyrical_Voice",
        audio_format="mp3", sample_rate=32000, bitrate=128000,
        speed=1.0, vol=1.0, pitch=0, emotion=None, language_boost=None,
    )
    assert payload["model"] == "speech-2.8-hd"
    assert payload["text"] == "你好"
    assert payload["output_format"] == "url"
    assert payload["stream"] is False
    assert payload["voice_setting"]["voice_id"] == "Chinese (Mandarin)_Lyrical_Voice"
    assert payload["audio_setting"]["format"] == "mp3"
    assert "emotion" not in payload["voice_setting"]
    assert "language_boost" not in payload


def test_tts_payload_carries_optional_emotion_and_language():
    payload = s.build_tts_payload(
        "hi", model="speech-2.8-hd", voice="v", audio_format="wav",
        sample_rate=16000, bitrate=64000, speed=1.2, vol=2.0, pitch=-1,
        emotion="calm", language_boost="auto",
    )
    assert payload["voice_setting"]["emotion"] == "calm"
    assert payload["language_boost"] == "auto"
    assert payload["audio_setting"]["format"] == "wav"


def test_asr_multipart_contains_model_and_file():
    boundary, body = s.build_asr_multipart(
        {"model": "asr-1.0", "response_format": "json"},
        "meeting.mp3", b"ID3fakeaudio", "audio/mpeg",
    )
    assert boundary.encode() in body
    assert b'name="model"' in body and b"asr-1.0" in body
    assert b'name="file"' in body and b'filename="meeting.mp3"' in body
    assert b"ID3fakeaudio" in body
    assert body.endswith(b"--\r\n")


# ── errors ─────────────────────────────────────────────────────────────────

def test_base_resp_error_is_not_treated_as_success():
    data = {"base_resp": {"status_code": 2049, "status_msg": "invalid api key"}}
    with pytest.raises(s.SpeechError) as e:
        s._raise_base_resp(data, "text-to-speech")
    assert "2049" in str(e.value)
    assert "region-specific" in str(e.value)


def test_base_resp_zero_is_success():
    s._raise_base_resp({"base_resp": {"status_code": 0, "status_msg": "success"}},
                       "text-to-speech")


def test_http_402_names_the_quota():
    body = json.dumps({"error": {"message": "insufficient balance (1008)"}}).encode()
    with pytest.raises(s.SpeechError) as e:
        s._raise_http_error("MiniMax", 402, body)
    assert "quota/credits" in str(e.value)


def test_http_401_names_the_region():
    body = json.dumps({"base_resp": {"status_code": 2049,
                                     "status_msg": "invalid api key"}}).encode()
    with pytest.raises(s.SpeechError) as e:
        s._raise_http_error("MiniMax", 200, body)
    assert "api.minimaxi.com" in str(e.value)


# ── input validation ───────────────────────────────────────────────────────

def test_asr_rejects_an_unsupported_extension(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("not audio", encoding="utf-8")
    args = type("A", (), {"file": str(f)})()
    with pytest.raises(s.SpeechError) as e:
        s.cmd_asr(args)
    assert "unsupported audio extension" in str(e.value)


def test_asr_rejects_a_missing_file(tmp_path):
    args = type("A", (), {"file": str(tmp_path / "nope.mp3")})()
    with pytest.raises(s.SpeechError) as e:
        s.cmd_asr(args)
    assert "not found" in str(e.value)


def test_tts_rejects_text_over_the_synchronous_limit():
    args = type("A", (), {"text": "x" * 10_001})()
    with pytest.raises(s.SpeechError) as e:
        s.cmd_tts(args)
    assert "10,000" in str(e.value)


# ── music ──────────────────────────────────────────────────────────────────

def _music_args(**overrides):
    values = {
        "prompt": "calm solo piano",
        "lyrics": None,
        "lyrics_file": None,
        "instrumental": True,
        "lyrics_optimizer": False,
    }
    values.update(overrides)
    return type("A", (), values)()


def test_music_payload_is_url_based_and_instrumental():
    payload = s.build_music_payload(
        "calm solo piano", model="music-3.0", lyrics=None,
        is_instrumental=True, lyrics_optimizer=False,
        audio_format="mp3", sample_rate=44100, bitrate=256000,
    )
    assert payload["model"] == "music-3.0"
    assert payload["output_format"] == "url"
    assert payload["stream"] is False
    assert payload["is_instrumental"] is True
    assert "lyrics" not in payload
    assert payload["audio_setting"]["sample_rate"] == 44100


def test_music_payload_carries_lyrics_and_optimizer():
    payload = s.build_music_payload(
        "indie folk", model="music-3.0", lyrics="[Verse]\nhello",
        is_instrumental=False, lyrics_optimizer=True,
        audio_format="wav", sample_rate=32000, bitrate=128000,
    )
    assert payload["lyrics"] == "[Verse]\nhello"
    assert payload["lyrics_optimizer"] is True
    assert "is_instrumental" not in payload


def test_music_rejects_an_empty_prompt():
    with pytest.raises(s.SpeechError) as e:
        s.cmd_music(_music_args(prompt="  "))
    assert "no music prompt" in str(e.value)


def test_music_requires_lyrics_or_optimizer_when_not_instrumental():
    with pytest.raises(s.SpeechError) as e:
        s.cmd_music(_music_args(instrumental=False))
    assert "needs lyrics" in str(e.value)


def test_music_rejects_both_lyrics_sources():
    with pytest.raises(s.SpeechError) as e:
        s.cmd_music(_music_args(lyrics="x", lyrics_file="y"))
    assert "either --lyrics or --lyrics-file" in str(e.value)


def test_music_rejects_lyrics_over_the_limit():
    with pytest.raises(s.SpeechError) as e:
        s.cmd_music(_music_args(instrumental=False, lyrics="x" * 3501))
    assert "3,500" in str(e.value)


def test_music_2153_names_the_new_user_closure():
    hint = s._error_hint(2153, 410)
    assert "closed to new users" in hint
    assert "MiniMax Audio" in hint


def test_music_default_filename_is_not_speech_prefixed():
    assert s._default_out("mp3", "music").name.startswith("music-")
    assert s._default_out("mp3").name.startswith("speech-")


# ── image ──────────────────────────────────────────────────────────────────

def _image_args(**overrides):
    values = {
        "prompt": "a cat on a windowsill",
        "n": 1,
        "size": "auto",
        "aspect_ratio": "16:9",
        "area": None,
        "disable_prompt_enhanced": False,
        "key_env": None,
        "base_url": None,
        "model": "MiniMax-H3-Image",
        "format": "jpeg",
        "out": None,
    }
    values.update(overrides)
    return type("A", (), values)()


def test_image_payload_uses_fixed_size_alone():
    payload = s.build_image_payload(
        "a cat", model="MiniMax-H3-Image", n=2, size="2720x1536",
        aspect_ratio=None, area=None, output_format="jpeg",
        disable_prompt_enhanced=False,
    )
    assert payload["size"] == "2720x1536"
    assert "aspect_ratio" not in payload and "area" not in payload
    assert payload["n"] == 2
    assert payload["response_format"] == "url"


def test_image_payload_uses_aspect_and_area_with_auto_size():
    payload = s.build_image_payload(
        "a cat", model="MiniMax-H3-Image", n=1, size="auto",
        aspect_ratio="16:9", area="2K", output_format="png",
        disable_prompt_enhanced=True,
    )
    assert "size" not in payload
    assert payload["aspect_ratio"] == "16:9"
    assert payload["area"] == "2K"
    assert payload["disable_prompt_enhanced"] is True


def test_image_rejects_size_with_aspect_ratio():
    with pytest.raises(s.SpeechError) as e:
        s.cmd_image(_image_args(size="2720x1536", aspect_ratio="16:9"))
    assert "cannot be combined" in str(e.value)


def test_image_no_enhance_needs_a_shape():
    with pytest.raises(s.SpeechError) as e:
        s.cmd_image(_image_args(disable_prompt_enhanced=True, aspect_ratio=None))
    assert "--no-enhance requires" in str(e.value)


def test_image_rejects_out_of_range_n():
    with pytest.raises(s.SpeechError) as e:
        s.cmd_image(_image_args(n=11))
    assert "between 1 and 10" in str(e.value)


def test_image_2068_names_the_plan_tier():
    assert "M Plan tier" in s._error_hint(2068, 200)
    assert "Highspeed" in s._error_hint(2068, 200)


def test_image_targets_accept_a_single_file(tmp_path):
    target = tmp_path / "cat.png"
    assert s._image_targets(str(target), 1, "png") == [target]


def test_image_targets_use_a_numbered_directory(tmp_path):
    targets = s._image_targets(str(tmp_path), 2, "jpeg")
    assert len(targets) == 2
    assert all(p.parent == tmp_path and p.name.startswith("image-") for p in targets)


# ── video ──────────────────────────────────────────────────────────────────

def _video_args(**overrides):
    values = {
        "prompt": "a paper boat drifting on a calm pond",
        "task": None,
        "model": "MiniMax-H3-Lite",
        "resolution": None,
        "duration": None,
        "ratio": "16:9",
        "image": None,
        "last_frame": None,
        "silent": False,
        "out": None,
        "no_wait": False,
        "timeout": 900,
        "poll": 10,
        "json": False,
        "key_env": None,
        "base_url": None,
    }
    values.update(overrides)
    return type("A", (), values)()


def test_video_payload_text_only():
    payload = s.build_video_payload(
        "a paper boat", model="MiniMax-H3-Lite", resolution="480P",
        duration=4, ratio="16:9", generate_audio=False,
    )
    assert payload["model"] == "MiniMax-H3-Lite"
    assert payload["content"] == [{"type": "text", "text": "a paper boat"}]
    assert payload["generate_audio"] is False
    assert payload["duration"] == 4


def test_video_payload_first_and_last_frame(tmp_path):
    first = tmp_path / "first.png"
    last = tmp_path / "last.png"
    first.write_bytes(b"\x89PNG\r\n\x1a\nfirst")
    last.write_bytes(b"\x89PNG\r\n\x1a\nlast")
    payload = s.build_video_payload(
        "animate this", model="MiniMax-H3", resolution="768P",
        duration=5, ratio="adaptive", generate_audio=True,
        first_frame=str(first), last_frame=str(last),
    )
    assert [item["role"] for item in payload["content"][1:]] == [
        "first_frame", "last_frame"]
    assert payload["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_video_requires_prompt_or_task():
    with pytest.raises(s.SpeechError) as e:
        s.cmd_video(_video_args(prompt=None))
    assert "no video prompt" in str(e.value)


def test_video_rejects_2k_on_lite():
    with pytest.raises(s.SpeechError) as e:
        s.cmd_video(_video_args(resolution="2K"))
    assert "480P, 768P" in str(e.value)


def test_video_max_needs_at_least_five_seconds():
    with pytest.raises(s.SpeechError) as e:
        s.cmd_video(_video_args(model="MiniMax-H3-Max", duration=4))
    assert "5-15" in str(e.value)


def test_video_text_to_video_rejects_adaptive_ratio():
    with pytest.raises(s.SpeechError) as e:
        s.cmd_video(_video_args(ratio="adaptive"))
    assert "concrete --ratio" in str(e.value)


def test_video_last_frame_requires_first_frame():
    with pytest.raises(s.SpeechError) as e:
        s.cmd_video(_video_args(last_frame="last.png"))
    assert "--last-frame requires --image" in str(e.value)


def test_video_target_defaults_to_mp4(tmp_path):
    target = s._video_target(str(tmp_path))
    assert target.parent == tmp_path
    assert target.suffix == ".mp4" and target.name.startswith("video-")
