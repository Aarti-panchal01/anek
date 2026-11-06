import asyncio
import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from backend import app as app_module, db
from backend.atomic_files import atomic_copyfile, run_atomic_command
from tests.test_api_destinations import _resolve, _sandbox


def _probe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def test_mock_bilingual_job_survives_interruption_and_exports_playable_media(monkeypatch):
    async def scenario():
        app_module._shutdown_requested.clear()
        with _sandbox(monkeypatch) as (_, data_root):
            monkeypatch.setattr(app_module, "settings", app_module.settings.model_copy(update={"gnani_mock": True, "llm_mock": True}))
            project = _resolve(app_module.create_project(app_module.ProjectCreate(
                name="Resilience smoke", source_lang="en-IN", target_langs=["hi-IN", "kn-IN"],
            )))
            source = data_root / "uploads" / project["id"] / "source.mp4"
            run_atomic_command([
                "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                "color=c=black:s=160x120:r=24:d=2", "-f", "lavfi", "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100", "-t", "2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(source),
            ], check=True, capture_output=True)
            video_duration = _probe_duration(source)
            with db.connect() as connection:
                connection.execute("UPDATE projects SET video_path=?,original_video_path=?,preview_path=?,preview_status='ready',duration_s=?,status='processing' WHERE id=?",
                                   (str(source), str(source), str(source), video_duration, project["id"]))
                connection.execute(
                    "INSERT INTO jobs(id,project_id,kind,content_type,translation_style,voice_mode,stock_voice,state,stage,total) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    ("smoke-job", project["id"], "dub", "explainer", "neutral", "stock", "Shivani", "pending", "queued", 2),
                )

            paid_transcriptions = 0
            resumed_saved_hindi = False

            def make_clip(language: str) -> dict:
                clip_audio = data_root / "exports" / project["id"] / "clips" / language / "clip_001.wav"
                run_atomic_command([
                    "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "anullsrc=channel_layout=mono:sample_rate=16000", "-t", "0.5",
                    "-c:a", "pcm_s16le", str(clip_audio),
                ], check=True, capture_output=True)
                dubbed = data_root / "exports" / project["id"] / f"source_{language}_phase3d_ai-dubbed.mp4"
                atomic_copyfile(source, dubbed)
                return {
                    "clip": 1, "start_s": 0.0, "end_s": 1.0, "source_cleaned": "hello there",
                    "translation": "नमस्ते" if language == "hi-IN" else "ನಮಸ್ಕಾರ",
                    "status": "green", "reasons": [], "audio_path": clip_audio,
                    "final_audio_duration_s": 0.5, "translation_synthesis_attempts": [],
                }

            def fake_quality_pass(video, reference, targets, output_dir, config, *args, resume_state,
                                  checkpoint_callback, continue_check, **kwargs):
                nonlocal paid_transcriptions, resumed_saved_hindi
                assert config.gnani_mock and config.llm_mock
                if not resume_state.get("transcript"):
                    paid_transcriptions += 1
                    resume_state["transcript"] = {"text": "hello there"}
                    checkpoint_callback("transcript", resume_state)

                clip_checkpoints = resume_state.setdefault("clips", {})
                if "hi-IN" not in clip_checkpoints:
                    hindi = make_clip("hi-IN")
                    audio_key = hindi["audio_path"].relative_to(output_dir).as_posix()
                    checkpoint_data = {key: value for key, value in hindi.items() if key != "audio_path"}
                    resume_state.update(current_language="hi-IN", last_clip_index=0)
                    clip_checkpoints.setdefault("hi-IN", {})["0"] = {"audio_key": audio_key, "clip": checkpoint_data}
                    checkpoint_callback("clip", resume_state)
                    # Simulate SIGTERM after Hindi is durable; Kannada remains to resume.
                    app_module._shutdown_requested.set()
                    continue_check()

                saved = clip_checkpoints["hi-IN"]["0"]
                resumed_saved_hindi = (output_dir / saved["audio_key"]).is_file()
                hindi = dict(saved["clip"], audio_path=output_dir / saved["audio_key"])
                kannada = make_clip("kn-IN")
                audio_key = kannada["audio_path"].relative_to(output_dir).as_posix()
                checkpoint_data = {key: value for key, value in kannada.items() if key != "audio_path"}
                resume_state.update(current_language="kn-IN", last_clip_index=0)
                clip_checkpoints.setdefault("kn-IN", {})["0"] = {"audio_key": audio_key, "clip": checkpoint_data}
                checkpoint_callback("clip", resume_state)
                return [
                    {"language": language, "clips": [clip], "term_sheet": {"terms": [], "idioms": []}}
                    for language, clip in (("hi-IN", hindi), ("kn-IN", kannada))
                ]

            monkeypatch.setattr(app_module, "run_quality_pass", fake_quality_pass)
            await app_module.run_job("smoke-job")
            with db.connect() as connection:
                state = connection.execute("SELECT state,checkpoint_json FROM jobs WHERE id='smoke-job'").fetchone()
            assert state["state"] == "interrupted"
            assert json.loads(state["checkpoint_json"])["transcript"]["text"] == "hello there"

            app_module._shutdown_requested.clear()
            await app_module.run_job("smoke-job")
            with db.connect() as connection:
                job = connection.execute("SELECT state FROM jobs WHERE id='smoke-job'").fetchone()
                clips = connection.execute("SELECT start_s,end_s FROM clips WHERE project_id=? ORDER BY idx", (project["id"],)).fetchall()
                language_count = connection.execute("SELECT COUNT(DISTINCT d.lang) FROM dubs d JOIN clips c ON d.clip_id=c.id WHERE c.project_id=?", (project["id"],)).fetchone()[0]
                dubbed_paths = [
                    data_root / "exports" / project["id"] / f"source_{language}_phase3d_ai-dubbed.mp4"
                    for language in ("hi-IN", "kn-IN")
                ]
            assert job["state"] == "complete"
            assert len(clips) > 0 and language_count == 2
            assert paid_transcriptions == 1 and resumed_saved_hindi
            ordered = sorted((float(row["start_s"]), float(row["end_s"])) for row in clips)
            assert all(start >= 0 for start, _ in ordered)
            assert all(left[1] <= right[0] for left, right in zip(ordered, ordered[1:]))
            for path in dubbed_paths:
                assert abs(_probe_duration(path) - video_duration) <= 0.05

            with TestClient(app_module.app) as client:
                for language in ("hi-IN", "kn-IN"):
                    response = client.get(f"/api/projects/{project['id']}/media?lang={language}")
                    assert response.status_code == 200
                    assert response.headers["content-type"].startswith("video/mp4")
                    playable = data_root / "exports" / project["id"] / f"served-{language}.mp4"
                    playable.write_bytes(response.content)
                    assert abs(_probe_duration(playable) - video_duration) <= 0.05
            app_module._shutdown_requested.clear()

    try:
        asyncio.run(scenario())
    finally:
        app_module._shutdown_requested.clear()
