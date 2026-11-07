import subprocess
import wave
from pathlib import Path

import httpx
import pytest

from backend.config import Settings
from backend.pipeline.phase3b import (
    advisory_audio_flag,
    advisory_stt,
    cache_successful_stt,
    merge_tiny_trailing_range,
    source_stt_with_retry,
    validate_stt_chunk,
)


def _wav(path: Path, seconds: float = 0.6, frequency: int = 440) -> Path:
    import math
    import struct

    rate = 16000
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        frames = [int(5000 * math.sin(2 * math.pi * frequency * n / rate)) for n in range(round(rate * seconds))]
        output.writeframes(b"".join(struct.pack("<h", value) for value in frames))
    return path


def _http_error(status: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.example.test/stt/v3")
    response = httpx.Response(status, text=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


@pytest.mark.parametrize("seconds", [0.0, 0.1])
def test_invalid_and_tiny_chunks_never_reach_stt(tmp_path, seconds):
    path = tmp_path / "chunk.wav"
    if seconds:
        _wav(path, seconds)
    else:
        path.write_bytes(b"")
    called = False

    def transcribe(_audio):
        nonlocal called
        called = True
        raise AssertionError("invalid audio reached provider")

    with pytest.raises(ValueError):
        source_stt_with_retry(path, transcribe, lambda: path, "unknown", "saaras:v3", 1)
    assert not called


def test_tiny_trailing_range_is_merged_before_stt(tmp_path):
    assert merge_tiny_trailing_range([(0.0, 2.0), (2.0, 2.1)]) == [(0.0, 2.1)]
    path = _wav(tmp_path / "chunk.wav", 0.1)
    with pytest.raises(ValueError, match="too short"):
        validate_stt_chunk(path)


def test_source_stt_reencodes_once_after_400(tmp_path):
    original = _wav(tmp_path / "original.wav")
    retry = _wav(tmp_path / "retry.wav", frequency=880)
    seen = []

    def transcribe(audio):
        seen.append(audio)
        if len(seen) == 1:
            raise _http_error(400, "invalid audio encoding")
        return {"transcript": "hello"}

    result = source_stt_with_retry(original, transcribe, lambda: retry, "unknown", "saaras:v3", 1)
    assert result["transcript"] == "hello"
    assert len(seen) == 2
    assert seen[0] != seen[1]


def test_advisory_stt_double_400_is_neutral_and_keeps_language_moving(tmp_path, monkeypatch):
    from backend.clients.gnani import LiveGnaniClient

    original = _wav(tmp_path / "original.wav")
    retry = _wav(tmp_path / "retry.wav", frequency=880)
    calls = []
    client = LiveGnaniClient(Settings(gnani_api_key="mock-key"))

    def mocked_post(*_args, **_kwargs):
        calls.append(1)
        request = httpx.Request("POST", "https://api.vachana.ai/stt/v3")
        return httpx.Response(400, text="speech check refused sample", request=request)

    monkeypatch.setattr(httpx, "post", mocked_post)
    result = advisory_stt(
        original,
        lambda audio: client.stt(audio, "hi-IN", "prisma-v3.0", format="transcribe"),
        "hi-IN", "prisma-v3.0", 2, lambda: retry,
    )
    assert result is None
    assert len(calls) == 2
    assert advisory_audio_flag(result, "नमस्ते", "hi-IN", []) == {
        "status": "yellow", "reasons": ["Audio check unavailable"]
    }


def test_failed_stt_is_not_cached_and_user_reason_keeps_body_without_mozilla(tmp_path):
    cache = {}

    def fail():
        raise _http_error(400, "audio payload is not accepted")

    with pytest.raises(httpx.HTTPStatusError):
        cache_successful_stt(cache, "chunk", fail)
    assert cache == {}
    assert cache_successful_stt(cache, "chunk", lambda: {"transcript": "retry ok"})["transcript"] == "retry ok"


def test_no_audio_video_fails_with_plain_message(tmp_path):
    from backend.pipeline.phase3b import run_quality_pass

    video = tmp_path / "silent.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=160x120:r=10:d=0.5",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(video),
    ], check=True)
    with pytest.raises(ValueError, match="This video has no audio track"):
        run_quality_pass(video, None, ["hi-IN"], tmp_path / "out", Settings(gnani_mock=True, llm_mock=True), stock_voice="Shivani")


def test_mock_job_merges_tiny_audio_tail_before_source_transcription(tmp_path, monkeypatch):
    from backend.pipeline import phase3b

    video = tmp_path / "tiny-tail.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=160x120:r=10:d=7.9",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=7.9", "-t", "7.9",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video),
    ], check=True)
    monkeypatch.setattr(phase3b, "targeted_silence_segments", lambda *_args, **_kwargs: [(0.0, 7.8), (7.8, 7.9)])

    class SourceSTT:
        calls = []

        def transcribe(self, audio, language_code, model, **_options):
            self.calls.append((audio, language_code, model))
            return {"transcript": "Hello there.", "detected_language_code": "en-IN"}

    source = SourceSTT()
    monkeypatch.setattr(phase3b, "create_sarvam_stt_client", lambda _settings: source)
    reports = phase3b.run_quality_pass(
        video, None, ["hi-IN"], tmp_path / "out", Settings(gnani_mock=True, llm_mock=True),
        stock_voice="Shivani",
    )
    assert len(source.calls) == 1
    assert len(source.calls[0][0]) > 0
    assert reports and reports[0]["clips"]


def test_user_error_contains_provider_body_and_no_mozilla_link():
    from backend.pipeline.phase3b import log_stt_failure

    error = _http_error(400, "invalid audio language parameter")
    reason = log_stt_failure("sarvam", "saaras:v3", "unknown", 1, Path("missing.wav"), error)
    assert "invalid audio language parameter" in reason
    assert "Mozilla" not in reason
    empty_error = _http_error(400, "")
    empty_reason = log_stt_failure("sarvam", "saaras:v3", "unknown", 1, Path("missing.wav"), empty_error)
    assert "Mozilla" not in empty_reason


def test_provider_body_redacts_key_like_values():
    from backend.pipeline.phase3b import stt_error_body

    error = _http_error(400, 'invalid request, "api_key": "secret-value-123", authorization=Bearer abc.def')
    body = stt_error_body(error)
    assert "secret-value-123" not in body
    assert "abc.def" not in body


def test_sarvam_http_request_uses_source_transcription_endpoint(monkeypatch):
    from backend.clients.sarvam_stt import SarvamSTTClient

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"transcript": "source words", "language_code": "en-IN"}

    def mocked_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(httpx, "post", mocked_post)
    client = SarvamSTTClient(Settings(sarvam_api_key="mock-key"))
    result = client.transcribe(b"wav-data", "unknown", "saaras:v3")
    assert captured["url"].endswith("/speech-to-text")
    assert captured["data"] == {"model": "saaras:v3"}
    assert result["transcript"] == "source words"
