"""Seed a disposable Anek workspace for deterministic README screenshots."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA_ROOT = Path(os.environ.get("ANEK_DATA_DIR", "data")).resolve()
API_ORIGIN = os.environ.get("DEMO_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def ffmpeg(arguments: list[str]) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *arguments], check=True, capture_output=True)


def make_source(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    supplied = os.environ.get("DEMO_VIDEO")
    if supplied:
        source = Path(supplied).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError("DEMO_VIDEO does not point to a readable video file")
        ffmpeg([
            "-i", str(source), "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=20",
            "-map", "0:v:0", "-map", "1:a:0", "-t", "20", "-vf",
            "scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2",
            "-r", "15", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "48k", "-movflags", "+faststart", str(path),
        ])
    else:
        ffmpeg([
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15:duration=20",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=20",
            "-t", "20", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "48k", "-movflags", "+faststart", str(path),
        ])
    if path.stat().st_size > 2 * 1024 * 1024:
        raise RuntimeError("Generated demo video is over 2 MB; lower its encoding bitrate")


def main() -> None:
    if os.environ.get("GNANI_MOCK", "0").lower() not in {"1", "true", "yes"}:
        raise RuntimeError("Set GNANI_MOCK=1 for demo seeding")
    if os.environ.get("LLM_MOCK", "0").lower() not in {"1", "true", "yes"}:
        raise RuntimeError("Set LLM_MOCK=1 for demo seeding")
    if not DATA_ROOT.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise RuntimeError("ANEK_DATA_DIR must point inside the system temporary directory")

    from backend import db

    db.initialize()
    video_path = DATA_ROOT / "uploads" / "demo" / "source.mp4"
    make_source(video_path)

    response = httpx.post(
        f"{API_ORIGIN}/api/projects",
        json={"name": "Demo", "source_lang": "en-IN", "target_langs": ["hi-IN", "kn-IN"]},
        timeout=15,
    )
    response.raise_for_status()
    project_id = response.json()["id"]
    project_video = DATA_ROOT / "uploads" / project_id / "source.mp4"
    project_video.parent.mkdir(parents=True, exist_ok=True)
    video_path.replace(project_video)

    clips = [
        {
            "start": 0.8, "end": 4.0,
            "source": "Welcome to Anek. Speak once and share your story.",
            "translations": {
                "hi-IN": "अनेक में आपका स्वागत है। एक बार बोलिए और अपनी कहानी साझा कीजिए।",
                "kn-IN": "ಅನೆಕ್‌ಗೆ ಸ್ವಾಗತ. ಒಮ್ಮೆ ಮಾತನಾಡಿ, ನಿಮ್ಮ ಕಥೆಯನ್ನು ಹಂಚಿಕೊಳ್ಳಿ.",
            },
            "status": "green", "reason": "", "spoken": 2.7,
        },
        {
            "start": 5.0, "end": 8.5,
            "source": "Today we are talking about clear communication.",
            "translations": {
                "hi-IN": "आज हम स्पष्ट संवाद के बारे में बात कर रहे हैं।",
                "kn-IN": "ಇಂದು ನಾವು ಸ್ಪಷ್ಟ ಸಂವಹನದ ಬಗ್ಗೆ ಮಾತನಾಡುತ್ತಿದ್ದೇವೆ.",
            },
            "status": "yellow", "reason": "Speech recognition transcript variants disagree; review the alternatives.", "spoken": 3.2,
        },
        {
            "start": 10.0, "end": 13.5,
            "source": "Names and numbers should stay accurate in every language.",
            "translations": {
                "hi-IN": "हर भाषा में नाम और संख्याएँ सही रहनी चाहिए।",
                "kn-IN": "ಪ್ರತಿ ಭಾಷೆಯಲ್ಲೂ ಹೆಸರುಗಳು ಮತ್ತು ಸಂಖ್ಯೆಗಳು ನಿಖರವಾಗಿರಬೇಕು.",
            },
            "status": "yellow", "reason": "Check that the emphasis on accuracy is preserved in the translation.", "spoken": 3.1,
        },
        {
            "start": 15.0, "end": 18.5,
            "source": "Review every clip before you export the final video.",
            "translations": {
                "hi-IN": "अंतिम वीडियो निर्यात करने से पहले हर क्लिप की समीक्षा करें।",
                "kn-IN": "ಅಂತಿಮ ವೀಡಿಯೊ ರಫ್ತು ಮಾಡುವ ಮೊದಲು ಪ್ರತಿ ಕ್ಲಿಪ್ ಪರಿಶೀಲಿಸಿ.",
            },
            "status": "red",
            "reason": "Speech exceeds its slot by 0.08s after shorter translation, speed-up, pause trimming, and safe-gap checks; this candidate was withheld to prevent overlap.",
            "spoken": 3.58,
        },
    ]
    exports = DATA_ROOT / "exports" / project_id
    exports.mkdir(parents=True, exist_ok=True)
    with db.connect() as connection:
        connection.execute(
            "UPDATE projects SET video_path=?,duration_s=20,status='complete' WHERE id=?",
            (str(project_video), project_id),
        )
        for language in ("hi-IN", "kn-IN"):
            report_rows = []
            for index, row in enumerate(clips):
                existing_clip = connection.execute(
                    "SELECT id FROM clips WHERE project_id=? AND idx=?", (project_id, index),
                ).fetchone()
                clip_id = existing_clip["id"] if existing_clip else str(uuid.uuid4())
                audio_path = exports / "clips" / language / f"clip_{index + 1:03d}.wav"
                audio_path.parent.mkdir(parents=True, exist_ok=True)
                ffmpeg([
                    "-f", "lavfi", "-i", f"sine=frequency={220 + index * 45}:sample_rate=16000:duration={row['spoken']}",
                    "-ac", "1", "-c:a", "pcm_s16le", str(audio_path),
                ])
                if not existing_clip:
                    connection.execute(
                        "INSERT INTO clips(id,project_id,idx,start_s,end_s,src_text) VALUES(?,?,?,?,?,?)",
                        (clip_id, project_id, index, row["start"], row["end"], row["source"]),
                    )
                connection.execute(
                    "INSERT INTO dubs(id,clip_id,lang,translation,draft_translation,status,reasons_json,audio_path,duration_s) VALUES(?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), clip_id, language, row["translations"][language], row["translations"][language], row["status"], json.dumps([row["reason"]] if row["reason"] else [], ensure_ascii=False), str(audio_path), row["spoken"]),
                )
                report_rows.append({
                    "clip": index + 1, "start_s": row["start"], "end_s": row["end"],
                    "source_raw": row["source"], "source_cleaned": row["source"],
                    "translation": row["translations"][language], "status": row["status"],
                    "reasons": [row["reason"]] if row["reason"] else [],
                    "slot_s": row["end"] - row["start"], "final_audio_duration_s": row["spoken"],
                    "duration_budget_chars": 42, "withheld": row["status"] == "red",
                    "stt_review": {},
                })
            (exports / f"source_{language}_phase3d_ai-dubbed.json").write_text(
                json.dumps({"source_language": "en-IN", "clips": report_rows}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            dubbed = exports / f"source_{language}_phase3d_ai-dubbed.mp4"
            ffmpeg([
                "-i", str(project_video), "-f", "lavfi", "-i",
                f"sine=frequency={330 if language == 'hi-IN' else 440}:sample_rate=44100:duration=20",
                "-map", "0:v:0", "-map", "1:a:0", "-t", "20", "-c:v", "copy", "-c:a", "aac", "-b:a", "64k",
                "-movflags", "+faststart", str(dubbed),
            ])

        connection.execute(
            "INSERT INTO global_terms(id,term,type,treatment,locked,targets_json,note,rule_type,heard_variants_json,spoken_forms_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(term,type) DO UPDATE SET treatment=excluded.treatment,locked=excluded.locked,note=excluded.note,rule_type=excluded.rule_type,heard_variants_json=excluded.heard_variants_json,spoken_forms_json=excluded.spoken_forms_json,updated_at=CURRENT_TIMESTAMP",
            (str(uuid.uuid4()), "Anek", "brand", "keep", 1,
             json.dumps({"hi-IN": "Anek", "kn-IN": "Anek"}, ensure_ascii=False),
             'Heard variants: "a head", "one head", "ek sir". Never translate.',
             "mishearing", json.dumps(["a head", "one head", "ek sir"]),
             json.dumps({"hi-IN": "Anek", "kn-IN": "Anek"})),
        )
    print(json.dumps({"project_id": project_id, "video": str(project_video)}))


if __name__ == "__main__":
    main()
