"""Generate a disposable 5-second HEVC phone-video fixture for Round 8."""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    base = args.output.with_name(f"{args.output.stem}.base.mp4")
    command = [
        "ffmpeg", "-y", "-hide_banner", "-v", "warning",
        "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "5", "-vf", "format=yuvj420p", "-c:v", "libx265",
        "-preset", "ultrafast", "-x265-params", "log-level=error",
        "-color_range", "pc", "-tag:v", "hvc1",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(base),
    ]
    print("COMMAND:", subprocess.list2cmdline(command))
    subprocess.run(command, check=True)
    rotate_command = [
        "ffmpeg", "-y", "-hide_banner", "-v", "error", "-display_rotation:v:0", "90",
        "-i", str(base), "-map", "0", "-c", "copy", "-movflags", "+faststart", str(args.output),
    ]
    print("ROTATION COMMAND:", subprocess.list2cmdline(rotate_command))
    subprocess.run(rotate_command, check=True)
    base.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
