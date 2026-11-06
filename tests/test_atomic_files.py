import logging
import subprocess
from pathlib import Path

import pytest

from backend import atomic_files


def test_atomic_command_hides_partial_output_and_replaces_only_on_success(monkeypatch, tmp_path):
    target = tmp_path / "take.wav"
    target.write_bytes(b"previous complete take")
    seen = []

    def write_complete(command, **options):
        temporary = Path(command[-1])
        seen.append(temporary)
        assert temporary.parent == target.parent
        assert temporary != target
        assert target.read_bytes() == b"previous complete take"
        temporary.write_bytes(b"new complete take")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(atomic_files.subprocess, "run", write_complete)
    atomic_files.run_atomic_command(["ffmpeg", "-i", "source", str(target)], check=True, capture_output=True)

    assert target.read_bytes() == b"new complete take"
    assert seen and not seen[0].exists()
    assert list(tmp_path.glob("*.part.wav")) == []


def test_atomic_command_failure_keeps_previous_output_and_removes_partial(monkeypatch, tmp_path):
    target = tmp_path / "dub.mp4"
    target.write_bytes(b"previous complete dub")

    def fail_after_partial(command, **options):
        Path(command[-1]).write_bytes(b"partial")
        raise subprocess.CalledProcessError(1, command, stderr="encoder stopped")

    monkeypatch.setattr(atomic_files.subprocess, "run", fail_after_partial)
    with pytest.raises(subprocess.CalledProcessError):
        atomic_files.run_atomic_command(["ffmpeg", "-i", "source", str(target)], check=True)

    assert target.read_bytes() == b"previous complete dub"
    assert list(tmp_path.iterdir()) == [target]


def test_startup_scan_marks_empty_and_invalid_outputs(monkeypatch, tmp_path):
    empty = tmp_path / "empty.mp4"
    truncated = tmp_path / "truncated.wav"
    empty.write_bytes(b"")
    truncated.write_bytes(b"partial wav")

    def invalid_probe(command, **options):
        raise subprocess.CalledProcessError(1, command, stderr="invalid media")

    monkeypatch.setattr(atomic_files.subprocess, "run", invalid_probe)
    marked = atomic_files.mark_invalid_media_for_regeneration(tmp_path, logging.getLogger("test"))

    assert set(marked) == {empty, truncated}
    assert (tmp_path / "empty.mp4.needs_regeneration").is_file()
    assert "invalid media" in (tmp_path / "truncated.wav.needs_regeneration").read_text()


def test_atomic_write_converts_enospc_and_removes_temporary_file(monkeypatch, tmp_path):
    target = tmp_path / "track.wav"

    def full_disk(_fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(atomic_files.os, "fsync", full_disk)
    with pytest.raises(atomic_files.StorageFullError, match="Server storage is full"):
        atomic_files.atomic_write_bytes(target, b"audio")

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
