"""Guards for `ccp speech` (bridge/ccp_speech.py).

The expensive mistakes here are silent:

* falling back to the pay-as-you-go key moves the charge off the M Plan;
* MiniMax answers some failures as HTTP 200 with ``base_resp.status_code``,
  which looks like success unless it is checked;
* a CN M Plan key sent to the global endpoint returns "invalid api key", not a
  region error, so the hint is the only actionable part;
* the ASR file must travel as multipart, not JSON.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

import ccp_speech as s  # noqa: E402


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
