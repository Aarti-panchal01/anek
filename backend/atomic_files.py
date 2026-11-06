from __future__ import annotations

import os
import errno
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any


class StorageFullError(OSError):
    """Raised when an output could not be completed because storage is full."""

    def __init__(self, message: str = "Server storage is full") -> None:
        super().__init__(errno.ENOSPC, message)
        self.message = message

    def __str__(self) -> str:
        return self.message


def is_storage_full(error: BaseException) -> bool:
    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
        return True
    stderr = getattr(error, "stderr", "") or ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    return "no space left on device" in f"{error} {stderr}".lower()


def _raise_storage_full(error: BaseException) -> None:
    if is_storage_full(error):
        raise StorageFullError() from error


def _temporary_for(target: Path) -> Path:
    return target.with_name(f".{target.stem}.{uuid.uuid4().hex}.part{target.suffix}")


def temporary_path(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    return _temporary_for(target)


def commit_temporary(temporary: Path, target: Path) -> None:
    try:
        _sync_and_replace(temporary, target)
    except OSError as error:
        _raise_storage_full(error)
        raise


def _sync_and_replace(temporary: Path, target: Path) -> None:
    with temporary.open("rb+") as output:
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, target)
    target.with_name(f"{target.name}.needs_regeneration").unlink(missing_ok=True)


def atomic_write_bytes(target: Path, data: bytes) -> None:
    temporary = temporary_path(target)
    try:
        with temporary.open("wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        _sync_and_replace(temporary, target)
    except OSError as error:
        _raise_storage_full(error)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(target: Path, data: str, encoding: str = "utf-8") -> None:
    atomic_write_bytes(target, data.encode(encoding))


def atomic_copyfile(source: Path, target: Path) -> None:
    temporary = temporary_path(target)
    try:
        shutil.copyfile(source, temporary)
        _sync_and_replace(temporary, target)
    except OSError as error:
        _raise_storage_full(error)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def run_atomic_command(arguments: list[str], **options: Any) -> subprocess.CompletedProcess:
    """Run a file-producing command with its final output hidden until durable."""
    target = Path(arguments[-1])
    temporary = temporary_path(target)
    command = [*arguments[:-1], str(temporary)]
    try:
        completed = subprocess.run(command, **options)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError(f"Command did not produce a complete output: {target.name}")
        _sync_and_replace(temporary, target)
        return completed
    except (OSError, subprocess.CalledProcessError) as error:
        _raise_storage_full(error)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def mark_invalid_media_for_regeneration(root: Path, logger) -> list[Path]:
    """Mark empty or ffprobe-invalid generated audio/video in the export tree."""
    marked: list[Path] = []
    if not root.exists():
        return marked
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".mp4", ".wav", ".mp3", ".m4a", ".aac"}:
            continue
        error = "empty output" if path.stat().st_size == 0 else ""
        stderr = ""
        if not error:
            try:
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(path)],
                    check=True, capture_output=True, text=True,
                )
                streams = json.loads(probe.stdout).get("streams", [])
                expected = "video" if path.suffix.lower() == ".mp4" else "audio"
                if not any(row.get("codec_type") == expected for row in streams):
                    error = f"missing {expected} stream"
            except (OSError, ValueError, subprocess.SubprocessError) as failure:
                error = str(failure)
                stderr = getattr(failure, "stderr", "") or ""
        if error:
            marker = path.with_name(f"{path.name}.needs_regeneration")
            atomic_write_text(marker, f"{error}\n{stderr}")
            logger.warning("Generated media marked for regeneration path=%s size=%s reason=%s ffprobe_stderr=%s",
                           path, path.stat().st_size, error, str(stderr)[:1000])
            marked.append(path)
    return marked
